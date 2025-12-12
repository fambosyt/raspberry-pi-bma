# v6 - mit Touch-GUI für 7" HDMI Touchdisplay
#
# Voraussetzungen:
# - audio.py (SoundPlayer) im selben Verzeichnis
# - mpg123, alsa-utils: sudo apt update && sudo apt install -y mpg123 alsa-utils
# - Tkinter: sudo apt install -y python3-tk
#
# Bedienung:
# - Touch-Buttons: Mute, Reset, Simulate Alarm
# - Hardware-Buttons (wie vorher) funktionieren weiterhin

import os
import signal
import time
import logging
import threading
import queue
import tkinter as tk
from tkinter import font
import RPi.GPIO as GPIO

from audio import SoundPlayer

# --- Logging --------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("easytec-alarm-gui")

# --- GPIO pins (BOARD numbering) ------------------------------------------
PIN_output_BUZ = 18  # Buzzer (Output)
PIN_output_LED = 16  # LED (Output)
PIN_b_alarm = 36     # Alarm button (Input)
PIN_b_reset = 37     # Reset button (Input)

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

alarm_active = False
_state_lock = threading.Lock()

audio_cmd_q = queue.Queue()
_stop_event = threading.Event()

# Small flag to reflect if audio is currently playing (set by AudioWorker)
audio_playing = False
audio_playing_lock = threading.Lock()

# --- Audio worker thread --------------------------------------------------
class AudioWorker(threading.Thread):
    def __init__(self, cmd_queue: queue.Queue, device: str = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon = True
        self.cmd_queue = cmd_queue
        self.device = device
        self.player = None

    def run(self):
        global audio_playing
        LOG.info("AudioWorker startet, device=%s", self.device or "<auto>")
        try:
            self.player = SoundPlayer(device=self.device)
        except Exception:
            LOG.exception("Fehler beim Initialisieren des SoundPlayers")
            self.player = None

        while not _stop_event.is_set():
            try:
                cmd, arg = self.cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if cmd == "play":
                filepath = arg
                if not os.path.exists(filepath):
                    LOG.error("Alarm-MP3 nicht gefunden: %s", filepath)
                    continue
                if self.player:
                    try:
                        self.player.play(filepath, interrupt=True)
                        with audio_playing_lock:
                            audio_playing = True
                        LOG.info("Spiele Alarm: %s", filepath)
                    except Exception:
                        LOG.exception("Fehler beim Abspielen")
                else:
                    LOG.error("Kein funktionierender Player vorhanden.")
            elif cmd == "stop":
                if self.player:
                    try:
                        self.player.stop()
                    except Exception:
                        LOG.exception("Fehler beim Stoppen der Audioausgabe")
                with audio_playing_lock:
                    audio_playing = False
                LOG.info("Audio gestoppt")
            elif cmd == "exit":
                LOG.info("AudioWorker - exit erhalten")
                break

        # Cleanup
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
        with audio_playing_lock:
            audio_playing = False
        LOG.info("AudioWorker beendet")

# --- Alarm logic (wie zuvor) ----------------------------------------------
def do_mute():
    GPIO.output(PIN_output_BUZ, GPIO.LOW)
    audio_cmd_q.put(("stop", None))
    LOG.info("Action: Alarm mute")

def do_reset():
    global alarm_active
    audio_cmd_q.put(("stop", None))
    GPIO.output(PIN_output_BUZ, GPIO.LOW)
    GPIO.output(PIN_output_LED, GPIO.LOW)
    with _state_lock:
        alarm_active = False
    LOG.info("Action: Alarm reset - System reaktiviert")

def alarm_handler():
    """
    Wird beim Auslösen gestartet. Wartet auf zwei Reset-Tastendrücke:
    1. -> Mute
    2. -> Reset
    """
    global alarm_active
    with _state_lock:
        if alarm_active:
            LOG.debug("Alarm bereits aktiv, ignoriere weiteren Trigger")
            return
        alarm_active = True

    GPIO.output(PIN_output_BUZ, GPIO.HIGH)
    GPIO.output(PIN_output_LED, GPIO.HIGH)
    LOG.info("Action: Alarm triggered - starte Buzzer + LED + Audio")
    audio_cmd_q.put(("play", ALARM_MP3))

    press_count = 0
    try:
        while True:
            if GPIO.input(PIN_b_reset) == GPIO.HIGH:
                time.sleep(0.05)
                if GPIO.input(PIN_b_reset) == GPIO.HIGH:
                    press_count += 1
                    LOG.info("Reset-Knopf gedrückt (%d)", press_count)
                    while GPIO.input(PIN_b_reset) == GPIO.HIGH:
                        time.sleep(0.05)
                    if press_count == 1:
                        do_mute()
                        LOG.info("Warte auf zweiten Druck zum Reset...")
                    elif press_count >= 2:
                        do_reset()
                        break
            time.sleep(0.1)
    except Exception:
        LOG.exception("Fehler im Alarm-Handler")
        do_reset()

# --- Status light thread --------------------------------------------------
def status_light_loop():
    while not _stop_event.is_set():
        with _state_lock:
            active = alarm_active
        if active:
            GPIO.output(PIN_output_LED, GPIO.HIGH)
            time.sleep(0.5)
            continue
        # Nicht aktiv: kurzes Blinken (Originalverhalten)
        GPIO.output(PIN_output_LED, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(PIN_output_LED, GPIO.LOW)
        for _ in range(100):
            if _stop_event.is_set():
                break
            time.sleep(0.1)

# --- Main loop (Überwacht Alarm-Knopf) -----------------------------------
def main_loop():
    LOG.info("System ready (Button-Überwachung läuft)...")
    while not _stop_event.is_set():
        try:
            if GPIO.input(PIN_b_alarm) == GPIO.HIGH:
                LOG.info("Hardware Alarm-Knopf gedrückt")
                time.sleep(0.05)
                if GPIO.input(PIN_b_alarm) == GPIO.HIGH:
                    threading.Thread(target=alarm_handler, daemon=True).start()
                    while GPIO.input(PIN_b_alarm) == GPIO.HIGH and not _stop_event.is_set():
                        time.sleep(0.05)
            time.sleep(0.1)
        except Exception:
            LOG.exception("Fehler in main_loop")
            time.sleep(0.5)

# --- GUI (Tkinter) -------------------------------------------------------
class AlarmGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("EasyTec Alarm")
        # Vollbild für Touchdisplay
        self.root.attributes("-fullscreen", True)
        # optional: Cursor verbergen
        try:
            self.root.config(cursor="none")
        except Exception:
            pass

        # große Schriftarten
        self.title_font = font.Font(size=36, weight="bold")
        self.big_font = font.Font(size=28)
        self.small_font = font.Font(size=18)

        # Layout: Status oben, Buttons in der Mitte, Footer unten
        self.status_var = tk.StringVar(value="Status: Unbekannt")
        self.led_var = tk.StringVar(value="LED: Off")
        self.buzz_var = tk.StringVar(value="Buzzer: Off")
        self.audio_var = tk.StringVar(value="Audio: Stopped")

        status_frame = tk.Frame(root, pady=20)
        status_frame.pack(fill="x")
        tk.Label(status_frame, text="EasyTec Alarm", font=self.title_font).pack()
        tk.Label(status_frame, textvariable=self.status_var, font=self.big_font).pack()
        tk.Label(status_frame, textvariable=self.led_var, font=self.small_font).pack()
        tk.Label(status_frame, textvariable=self.buzz_var, font=self.small_font).pack()
        tk.Label(status_frame, textvariable=self.audio_var, font=self.small_font).pack()

        # Buttons
        btn_frame = tk.Frame(root, pady=40)
        btn_frame.pack(expand=True)

        btn_play = tk.Button(btn_frame, text="Simulate Alarm", font=self.big_font, bg="#e74c3c", fg="white",
                             width=18, height=2, command=self.simulate_alarm)
        btn_play.grid(row=0, column=0, padx=10, pady=10)

        btn_mute = tk.Button(btn_frame, text="Mute", font=self.big_font, bg="#f39c12", fg="white",
                             width=18, height=2, command=self.gui_mute)
        btn_mute.grid(row=0, column=1, padx=10, pady=10)

        btn_reset = tk.Button(btn_frame, text="Reset", font=self.big_font, bg="#27ae60", fg="white",
                              width=18, height=2, command=self.gui_reset)
        btn_reset.grid(row=0, column=2, padx=10, pady=10)

        # Footer: Exit button (klein), nützlich für Debugging
        footer = tk.Frame(root)
        footer.pack(side="bottom", pady=10)
        tk.Button(footer, text="Exit", font=self.small_font, command=self.on_exit).pack()

        # Start UI-Update-Loop
        self.update_ui()

    def simulate_alarm(self):
        # Startet einen Alarm-Handler (wie Hardware-Trigger) in separatem Thread
        threading.Thread(target=alarm_handler, daemon=True).start()

    def gui_mute(self):
        do_mute()

    def gui_reset(self):
        do_reset()

    def on_exit(self):
        LOG.info("GUI Exit gedrückt")
        _stop_event.set()
        audio_cmd_q.put(("exit", None))
        self.root.quit()

    def update_ui(self):
        # Aktualisiere Status-Labels basierend auf globalen Variablen
        with _state_lock:
            active = alarm_active
        if active:
            self.status_var.set("Status: ALARM")
        else:
            self.status_var.set("Status: Ready")

        # LED / Buzzer state aus GPIO auslesen
        try:
            led_state = GPIO.input(PIN_output_LED)
            buzz_state = GPIO.input(PIN_output_BUZ)
        except Exception:
            led_state = 0
            buzz_state = 0
        self.led_var.set(f"LED: {'On' if led_state else 'Off'}")
        self.buzz_var.set(f"Buzzer: {'On' if buzz_state else 'Off'}")

        with audio_playing_lock:
            ap = audio_playing
        self.audio_var.set(f"Audio: {'Playing' if ap else 'Stopped'}")

        # Wiederhole Update
        if not _stop_event.is_set():
            self.root.after(300, self.update_ui)

# --- Signal handler ------------------------------------------------------
def signal_handler(sig, frame):
    LOG.info("Signal empfangen, beende sauber...")
    _stop_event.set()
    audio_cmd_q.put(("exit", None))
    # allow GUI mainloop to exit
    try:
        # If Tk mainloop running, quit it
        for w in tk._default_root.children.values():
            try:
                w.quit()
            except Exception:
                pass
    except Exception:
        pass
    # cleanup GPIO
    time.sleep(0.2)
    GPIO.cleanup()
    LOG.info("GPIO cleaned up. Exit.")
    raise SystemExit(0)

# --- Start everything ----------------------------------------------------
def main():
    # Warnung, wenn MP3 fehlt
    if not os.path.exists(ALARM_MP3):
        LOG.warning("Alarm-MP3 nicht gefunden: %s", ALARM_MP3)

    # Starte AudioWorker
    audio_worker = AudioWorker(audio_cmd_q, device=SOUND_DEVICE)
    audio_worker.start()

    # Starte Hintergrund-Threads
    thread_status = threading.Thread(target=status_light_loop, daemon=True)
    thread_status.start()
    thread_main = threading.Thread(target=main_loop, daemon=True)
    thread_main.start()

    # Setup signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Tkinter GUI im Hauptthread
    root = tk.Tk()
    app = AlarmGUI(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt in Tk mainloop")
    finally:
        _stop_event.set()
        audio_cmd_q.put(("exit", None))
        time.sleep(0.2)
        GPIO.cleanup()
        LOG.info("Programm beendet")

if __name__ == "__main__":
    main()
