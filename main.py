#!/usr/bin/env python
# Coding by Easy Tec | easytec.tech
# v5 - mit USB-Audio (MP3) Alarm
#
# Voraussetzung:
# - Lege eine MP3 z.B. sounds/alarm.mp3 im Projektordner ab
# - Installiere mpg123 und alsa-utils auf dem Raspberry Pi:
#     sudo apt update
#     sudo apt install -y mpg123 alsa-utils
#
# Du kannst das Ausgabegerät überschreiben mit der ENV VAR SOUND_DEVICE, z.B.:
#   SOUND_DEVICE=hw:1,0 python3 main.py
#
# Audio-Implementation nutzt audio.py (SoundPlayer). Stelle sicher, dass audio.py
# im selben Verzeichnis liegt.

import os
import signal
import time
import logging
import threading
import queue
import RPi.GPIO as GPIO

from audio import SoundPlayer  # audio.py muss im selben Ordner liegen

# --- Logging ---------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("easytec-alarm")

# --- GPIO pins (BOARD numbering) ------------------------------------------
PIN_output_BUZ = 18  # pin number for your output 1 (buzzer)
PIN_output_LED = 16  # pin number for your output 2 (LED)
PIN_b_alarm = 36     # pin number for your alarm button
PIN_b_reset = 37     # pin number for your reset button

GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)

GPIO.setup(PIN_output_BUZ, GPIO.OUT)
GPIO.setup(PIN_output_LED, GPIO.OUT)
GPIO.setup(PIN_b_alarm, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(PIN_b_reset, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

GPIO.output(PIN_output_BUZ, GPIO.LOW)
GPIO.output(PIN_output_LED, GPIO.LOW)

# --- Config / Globals ----------------------------------------------------
ALARM_MP3 = os.environ.get("ALARM_MP3", "sounds/alarm.mp3")
SOUND_DEVICE = os.environ.get("SOUND_DEVICE", None)

# Shared state
alarm_active = False   # True while alarm is active (until reset)
_state_lock = threading.Lock()

# Audio command queue for the AudioWorker
audio_cmd_q = queue.Queue()
_stop_event = threading.Event()

# --- Audio worker thread --------------------------------------------------
class AudioWorker(threading.Thread):
    """
    Thread, der SoundPlayer besitzt und Befehle über eine Queue empfängt:
    ('play', filepath), ('stop', None), ('exit', None)
    """
    def __init__(self, cmd_queue: queue.Queue, device: str = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon = True
        self.cmd_queue = cmd_queue
        self.device = device
        self.player = None

    def run(self):
        LOG.info("AudioWorker startet, device=%s", self.device or "<auto>")
        try:
            self.player = SoundPlayer(device=self.device)
        except Exception as e:
            LOG.exception("Fehler beim Initialisieren des SoundPlayers: %s", e)
            # trotzdem weiterlaufen, um keine Blockade zu erzeugen
            self.player = None

        while not _stop_event.is_set():
            try:
                cmd, arg = self.cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if cmd == "play":
                filepath = arg
                if not os.path.exists(filepath):
                    LOG.error("Alarm-Datei nicht gefunden: %s", filepath)
                    continue
                if self.player:
                    try:
                        self.player.play(filepath, interrupt=True)
                        LOG.info("Spiele Alarm: %s", filepath)
                    except Exception:
                        LOG.exception("Fehler beim Abspielen")
                else:
                    LOG.error("Kein funktionierender Player vorhanden.")
            elif cmd == "stop":
                if self.player:
                    try:
                        self.player.stop()
                        LOG.info("Audio gestoppt")
                    except Exception:
                        LOG.exception("Fehler beim Stoppen der Audioausgabe")
            elif cmd == "exit":
                LOG.info("AudioWorker erhält exit")
                break

        # sicherstellen, dass der Player gestoppt ist
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
        LOG.info("AudioWorker beendet")

# --- Alarm / Button handling ----------------------------------------------
def do_mute():
    """Stummschalten: Buzzer aus, Audio stoppen."""
    GPIO.output(PIN_output_BUZ, GPIO.LOW)
    audio_cmd_q.put(("stop", None))
    LOG.info("Action: Alarm mute")

def do_reset():
    """Reset: Alarm beenden, LED aus, System zurücksetzen."""
    global alarm_active
    audio_cmd_q.put(("stop", None))
    GPIO.output(PIN_output_BUZ, GPIO.LOW)
    GPIO.output(PIN_output_LED, GPIO.LOW)
    with _state_lock:
        alarm_active = False
    LOG.info("Action: Alarm reset - System reaktiviert")

def alarm_handler():
    """
    Handler, der bei Auslösen des Alarm-Knopfs in einem eigenen Thread läuft.
    Erwartet zwei Drücke des Reset-Buttons:
      - erster Druck -> Mute (Buzzer aus, Audio stop)
      - zweiter Druck -> Reset (Alarm zurücksetzen)
    """
    global alarm_active
    with _state_lock:
        if alarm_active:
            LOG.debug("Alarm already active, ignoring duplicate trigger")
            return
        alarm_active = True

    # Aktiviere Alarm-Signale
    GPIO.output(PIN_output_BUZ, GPIO.HIGH)
    GPIO.output(PIN_output_LED, GPIO.HIGH)
    LOG.info("Action: Alarm triggered - starte Buzzer + LED + Audio")

    # Starte Audio
    audio_cmd_q.put(("play", ALARM_MP3))

    # Warte auf zwei Bestätigungen am Reset-Knopf
    press_count = 0
    try:
        while True:
            # Polling mit kurzer Verzögerung, entprellen
            if GPIO.input(PIN_b_reset) == GPIO.HIGH:
                # einfache Entprellung: warte bis losgelassen
                time.sleep(0.05)
                if GPIO.input(PIN_b_reset) == GPIO.HIGH:
                    press_count += 1
                    LOG.info("Reset-Knopf gedrückt (%d)", press_count)
                    # Warte auf Loslassen
                    while GPIO.input(PIN_b_reset) == GPIO.HIGH:
                        time.sleep(0.05)
                    # Erster Druck -> mute
                    if press_count == 1:
                        do_mute()
                        LOG.info("Warte auf zweiten Druck zum Reset...")
                    # Zweiter Druck -> reset und beenden des Handlers
                    elif press_count >= 2:
                        do_reset()
                        break
            time.sleep(0.1)
    except Exception:
        LOG.exception("Fehler im Alarm-Handler")
        # im Fehlerfall Alarm zurücksetzen
        do_reset()

# --- Status LED Thread ----------------------------------------------------
def status_light_loop():
    """
    Blink-Status wenn kein Alarm aktiv:
    (verhält sich ähnlich wie das Original: kurz an, dann lange Pause)
    Wenn Alarm aktiv -> LED dauerhaft an.
    """
    while not _stop_event.is_set():
        with _state_lock:
            active = alarm_active
        if active:
            GPIO.output(PIN_output_LED, GPIO.HIGH)
            time.sleep(0.5)
            continue
        # nicht aktiv: kurzes Blinken (wie vorher)
        GPIO.output(PIN_output_LED, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(PIN_output_LED, GPIO.LOW)
        # lange Pause wie im Original
        for _ in range(100):
            if _stop_event.is_set():
                break
            time.sleep(0.1)

# --- Main loop (Button Überwachung) --------------------------------------
def main_loop():
    """
    Überwacht den Alarm-Knopf. Beim Drücken wird ein Alarm-Handler-Thread gestartet.
    """
    LOG.info("System ready...")
    while not _stop_event.is_set():
        try:
            if GPIO.input(PIN_b_alarm) == GPIO.HIGH:
                LOG.info("Alarm-Knopf gedrückt")
                # einfachen Debounce
                time.sleep(0.05)
                if GPIO.input(PIN_b_alarm) == GPIO.HIGH:
                    # Starte Alarm-Handler in eigenem Thread, damit main loop weiterlaufen kann
                    t = threading.Thread(target=alarm_handler, daemon=True)
                    t.start()
                    # Warte, bis der Knopf losgelassen wurde
                    while GPIO.input(PIN_b_alarm) == GPIO.HIGH and not _stop_event.is_set():
                        time.sleep(0.05)
            time.sleep(0.1)
        except Exception:
            LOG.exception("Fehler in main_loop")
            time.sleep(0.5)

# --- Signal handler / Cleanup --------------------------------------------
def signal_handler(sig, frame):
    LOG.info("Signal empfangen, beende sauber...")
    _stop_event.set()
    # sage dem AudioWorker, dass er beenden soll
    audio_cmd_q.put(("exit", None))
    # warte kurz, damit Threads sauber stoppen können
    time.sleep(0.3)
    GPIO.cleanup()
    LOG.info("GPIO cleaned up. Exit.")
    # allow process to exit
    raise SystemExit(0)

# --- Start Threads --------------------------------------------------------
if __name__ == "__main__":
    # Prüfe, ob die MP3 vorhanden ist (nur Warnung)
    if not os.path.exists(ALARM_MP3):
        LOG.warning("Alarm-MP3 nicht gefunden: %s", ALARM_MP3)
        LOG.warning("Lege die Datei im Projektordner an oder setze ALARM_MP3 env var.")

    # Start AudioWorker
    audio_worker = AudioWorker(audio_cmd_q, device=SOUND_DEVICE)
    audio_worker.start()

    # Start status light thread
    thread_status = threading.Thread(target=status_light_loop, daemon=True)
    thread_status.start()

    # Start main loop thread
    thread_main = threading.Thread(target=main_loop, daemon=True)
    thread_main.start()

    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Keep main thread alive to receive signals
    try:
        while True:
            time.sleep(1)
    except SystemExit:
        LOG.info("Programm beendet")
    except KeyboardInterrupt:
        signal_handler(None, None)
