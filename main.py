#!/usr/bin/env python3
# v8 - Visuell verbessertes Design (moderner, "industrial" / Siemens-like)
# - modernes Farbschema, Gradient-Header, LED-Glow/Puls, größere typografische Hierarchie
# - History als Treeview, schlanke Settings, Tastenkürzel
# - Beibehaltung der Funktionalität: AudioWorker, AlarmController, MockGPIO, Config
#
# Voraussetzungen:
# - audio.py (SoundPlayer) im selben Verzeichnis
# - Tkinter (python3-tk)
# - Optional: amixer für systemweite Lautstärke (ALS A)
#
# Hinweis: Diese Datei ersetzt main.py — UI-Änderungen sind rückwärtskompatibel.

import os
import signal
import time
import logging
import threading
import queue
import json
from collections import deque
from datetime import datetime
import subprocess

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font

# Versuche RPi.GPIO zu importieren, ansonsten Simulation (nützlich für Entwicklung)
try:
    import RPi.GPIO as GPIO  # type: ignore
    IS_RPI = True
except Exception:
    IS_RPI = False

from audio import SoundPlayer

# --- Logging --------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("easytec-alarm-gui")

# --- Config / Globals ----------------------------------------------------
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")

DEFAULT_CONFIG = {
    "ALARM_MP3": os.environ.get("ALARM_MP3", "sounds/alarm.mp3"),
    "SOUND_DEVICE": os.environ.get("SOUND_DEVICE", None),
    "VOLUME_PERCENT": 80,
    "SIMULATE_GPIO": not IS_RPI
}

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            cfg.update(data)
    except Exception:
        LOG.exception("Fehler beim Laden der Konfiguration, verwende Defaults.")
    return cfg

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        LOG.exception("Fehler beim Speichern der Konfiguration")

config = load_config()
ALARM_MP3 = config["ALARM_MP3"]
SOUND_DEVICE = config["SOUND_DEVICE"]
VOLUME_PERCENT = int(config.get("VOLUME_PERCENT", 80))
SIMULATE_GPIO = bool(config.get("SIMULATE_GPIO", not IS_RPI))

# --- GPIO pins (BOARD numbering) ------------------------------------------
PIN_output_BUZ = 18  # Buzzer (Output)
PIN_output_LED = 16  # LED (Output)
PIN_b_alarm = 36     # Alarm button (Input)
PIN_b_reset = 37     # Reset button (Input)

# --- Mock GPIO for non-RPi / Testing -------------------------------------
class MockGPIO:
    BOARD = "BOARD"
    IN = "IN"
    OUT = "OUT"
    PUD_DOWN = "PUD_DOWN"
    HIGH = 1
    LOW = 0

    def __init__(self):
        self._pins = {}
        self._inputs = {}
        LOG.info("MockGPIO initialisiert (Simulation)")

    def setmode(self, m):
        LOG.debug("MockGPIO setmode(%s)", m)

    def setwarnings(self, flag):
        pass

    def setup(self, pin, mode, pull_up_down=None):
        self._pins[pin] = {"mode": mode, "state": self.LOW}
        if mode == self.IN:
            self._inputs[pin] = self.LOW

    def input(self, pin):
        return self._inputs.get(pin, self.LOW)

    def output(self, pin, value):
        if pin in self._pins:
            self._pins[pin]["state"] = value
        LOG.debug("MockGPIO output pin=%s value=%s", pin, value)

    def cleanup(self):
        LOG.info("MockGPIO cleanup")

    def set_input(self, pin, value):
        self._inputs[pin] = value

# Wähle GPIO-Implementierung
if IS_RPI and not SIMULATE_GPIO:
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
else:
    GPIO = MockGPIO()
    SIMULATE_GPIO = True

# Setup pins (sicher in try/except)
try:
    GPIO.setup(PIN_output_BUZ, GPIO.OUT)
    GPIO.setup(PIN_output_LED, GPIO.OUT)
    GPIO.setup(PIN_b_alarm, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_b_reset, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.output(PIN_output_BUZ, GPIO.LOW)
    GPIO.output(PIN_output_LED, GPIO.LOW)
except Exception:
    LOG.exception("Fehler beim Initialisieren der GPIO-Pins (Fortsetzen im Simulationsmodus)")

# --- Threading primitives -----------------------------------------------
alarm_active = False
_state_lock = threading.Lock()

audio_cmd_q = queue.Queue()
_stop_event = threading.Event()

audio_playing = False
audio_playing_lock = threading.Lock()

history = deque(maxlen=500)
def add_history(entry):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history.appendleft((ts, entry))
    LOG.info(entry)

# --- System volume helper (amixer) ---------------------------------------
def set_system_volume(percent: int):
    try:
        percent = max(0, min(100, int(percent)))
        subprocess.run(["amixer", "sset", "PCM", f"{percent}%"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        LOG.info("Set system volume to %d%%", percent)
    except Exception:
        LOG.debug("amixer not available or failed to set volume.")

set_system_volume(VOLUME_PERCENT)

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
                    add_history(f"Audio: MP3 nicht gefunden: {filepath}")
                    continue
                if self.player:
                    try:
                        LOG.info("Spiele Alarm: %s", filepath)
                        with audio_playing_lock:
                            audio_playing = True
                        self.player.play(filepath, interrupt=True)
                        with audio_playing_lock:
                            audio_playing = False
                        LOG.info("Audio fertig")
                    except Exception:
                        LOG.exception("Fehler beim Abspielen")
                        with audio_playing_lock:
                            audio_playing = False
                else:
                    LOG.error("Kein funktionierender Player vorhanden.")
                    add_history("Audio: Kein Player vorhanden")
            elif cmd == "stop":
                if self.player:
                    try:
                        LOG.info("Audio Stop angefordert")
                        self.player.stop()
                    except Exception:
                        LOG.exception("Fehler beim Stoppen der Audioausgabe")
                with audio_playing_lock:
                    audio_playing = False
                add_history("Audio: gestoppt")
            elif cmd == "set_volume":
                try:
                    set_system_volume(int(arg))
                    add_history(f"Volume eingestellt: {arg}%")
                except Exception:
                    LOG.exception("Fehler beim Setzen der Lautstärke")
            elif cmd == "exit":
                LOG.info("AudioWorker - exit erhalten")
                break

        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
        with audio_playing_lock:
            audio_playing = False
        LOG.info("AudioWorker beendet")

# --- Alarm controller ----------------------------------------------------
class AlarmController:
    def __init__(self):
        self._lock = threading.Lock()

    def trigger_alarm(self, source="external"):
        global alarm_active
        with self._lock:
            with _state_lock:
                if alarm_active:
                    LOG.debug("Alarm bereits aktiv, ignoriere weiteren Trigger")
                    add_history("Alarm-Trigger ignoriert (bereits aktiv)")
                    return
                alarm_active = True
            add_history(f"Alarm ausgelöst ({source})")
            try:
                GPIO.output(PIN_output_BUZ, GPIO.HIGH)
                GPIO.output(PIN_output_LED, GPIO.HIGH)
            except Exception:
                LOG.exception("GPIO output failed on trigger")
            audio_cmd_q.put(("play", ALARM_MP3))
            threading.Thread(target=self._alarm_wait_for_reset, daemon=True).start()

    def _alarm_wait_for_reset(self):
        press_count = 0
        try:
            while not _stop_event.is_set():
                if GPIO.input(PIN_b_reset) == GPIO.HIGH:
                    time.sleep(0.05)
                    if GPIO.input(PIN_b_reset) == GPIO.HIGH:
                        press_count += 1
                        add_history(f"Reset-Knopf gedrückt ({press_count})")
                        while GPIO.input(PIN_b_reset) == GPIO.HIGH and not _stop_event.is_set():
                            time.sleep(0.05)
                        if press_count == 1:
                            self.mute()
                            add_history("Alarm: gemuted (erster Druck)")
                        elif press_count >= 2:
                            self.reset()
                            add_history("Alarm: reset (zweiter Druck)")
                            break
                time.sleep(0.1)
        except Exception:
            LOG.exception("Fehler im Alarm-Handler")
            self.reset()

    def mute(self):
        try:
            GPIO.output(PIN_output_BUZ, GPIO.LOW)
        except Exception:
            LOG.exception("Fehler beim Setzen des Buzzers")
        audio_cmd_q.put(("stop", None))
        add_history("Aktion: Mute")

    def reset(self):
        global alarm_active
        audio_cmd_q.put(("stop", None))
        try:
            GPIO.output(PIN_output_BUZ, GPIO.LOW)
            GPIO.output(PIN_output_LED, GPIO.LOW)
        except Exception:
            LOG.exception("Fehler beim Setzen der Ausgänge")
        with _state_lock:
            alarm_active = False
        add_history("Aktion: Reset - System reaktiviert")

alarm_ctrl = AlarmController()

# --- Status light thread --------------------------------------------------
def status_light_loop():
    while not _stop_event.is_set():
        with _state_lock:
            active = alarm_active
        if active:
            try:
                GPIO.output(PIN_output_LED, GPIO.HIGH)
            except Exception:
                pass
            time.sleep(0.5)
            continue
        try:
            GPIO.output(PIN_output_LED, GPIO.HIGH)
            time.sleep(0.1)
            GPIO.output(PIN_output_LED, GPIO.LOW)
        except Exception:
            pass
        for _ in range(100):
            if _stop_event.is_set():
                break
            time.sleep(0.1)

# --- Main loop (Überwacht Alarm-Knopf) -----------------------------------
def main_loop():
    LOG.info("System ready (Button-Überwachung läuft)... (SIM=%s)", SIMULATE_GPIO)
    while not _stop_event.is_set():
        try:
            if GPIO.input(PIN_b_alarm) == GPIO.HIGH:
                LOG.info("Hardware Alarm-Knopf gedrückt")
                time.sleep(0.05)
                if GPIO.input(PIN_b_alarm) == GPIO.HIGH:
                    alarm_ctrl.trigger_alarm(source="hardware-button")
                    while GPIO.input(PIN_b_alarm) == GPIO.HIGH and not _stop_event.is_set():
                        time.sleep(0.05)
            time.sleep(0.1)
        except Exception:
            LOG.exception("Fehler in main_loop")
            time.sleep(0.5)

# --- UI: enhanced design -------------------------------------------------
class BeautifulStyle:
    # Farbpalette (Siemens-like, industrial & calm)
    BG = "#f2f6f9"
    ACCENT = "#0b5e88"       # deep teal / corporate
    ACCENT2 = "#1f7fb0"
    DANGER = "#c0392b"
    WARNING = "#f39c12"
    SUCCESS = "#27ae60"
    CARD = "#ffffff"
    MUTED = "#6b7a86"

    @staticmethod
    def apply(root, style: ttk.Style):
        root.configure(background=BeautifulStyle.BG)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("App.TFrame", background=BeautifulStyle.BG)
        style.configure("Card.TFrame", background=BeautifulStyle.CARD, relief="flat", borderwidth=0)
        style.configure("Header.TLabel", background=BeautifulStyle.BG, foreground=BeautifulStyle.ACCENT, font=("Helvetica", 24, "bold"))
        style.configure("SubHeader.TLabel", background=BeautifulStyle.BG, foreground=BeautifulStyle.MUTED, font=("Helvetica", 10))
        style.configure("Accent.TButton", foreground="white", background=BeautifulStyle.ACCENT, font=("Helvetica", 14, "bold"))
        style.map("Accent.TButton", background=[("active", BeautifulStyle.ACCENT2)])
        style.configure("Danger.TButton", foreground="white", background=BeautifulStyle.DANGER, font=("Helvetica", 14, "bold"))
        style.configure("Success.TButton", foreground="white", background=BeautifulStyle.SUCCESS, font=("Helvetica", 14, "bold"))
        style.configure("Warning.TButton", foreground="black", background=BeautifulStyle.WARNING, font=("Helvetica", 12, "bold"))
        style.configure("History.Treeview", background=BeautifulStyle.CARD, fieldbackground=BeautifulStyle.CARD)

class AlarmGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("EasyTec Alarm · Professional")
        self.root.attributes("-fullscreen", True)
        try:
            self.root.config(cursor="none")
        except Exception:
            pass

        self.style = ttk.Style()
        BeautifulStyle.apply(self.root, self.style)

        # Fonts
        self.title_font = font.Font(family="Helvetica", size=28, weight="bold")
        self.big_font = font.Font(family="Helvetica", size=20, weight="bold")
        self.medium_font = font.Font(family="Helvetica", size=12)
        self.small_font = font.Font(family="Helvetica", size=10)

        # Top-level layout: header, content
        self.header_canvas = tk.Canvas(root, height=110, highlightthickness=0)
        self.header_canvas.pack(fill="x")
        self._draw_header()

        content = ttk.Frame(root, style="App.TFrame", padding=18)
        content.pack(fill="both", expand=True)

        left = ttk.Frame(content, style="App.TFrame")
        left.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(content, width=360, style="App.TFrame")
        right.pack(side="right", fill="y")

        # Big status card
        status_card = ttk.Frame(left, style="Card.TFrame", padding=20)
        status_card.pack(fill="both", expand=True, padx=(0,12))

        # Status title+subtitle
        title = ttk.Label(status_card, text="SYSTEM STATUS", style="Header.TLabel")
        title.pack(anchor="w")
        subtitle = ttk.Label(status_card, text="Industrial Alarm Interface · Ready for deployment", style="SubHeader.TLabel")
        subtitle.pack(anchor="w", pady=(0,10))

        # Main status row
        status_row = ttk.Frame(status_card, style="Card.TFrame")
        status_row.pack(fill="x", pady=(6,14))

        # LED canvas with glow
        self.led_canvas = tk.Canvas(status_row, width=120, height=120, highlightthickness=0, bg=BeautifulStyle.CARD)
        self.led_canvas.pack(side="left", padx=(0,18))
        # Glow layers
        self._led_glow_big = self.led_canvas.create_oval(6,6,114,114, fill="", outline="")
        self._led_glow_mid = self.led_canvas.create_oval(18,18,102,102, fill="", outline="")
        self._led_circle = self.led_canvas.create_oval(34,34,86,86, fill="#2ecc71", outline="#1e8f53", width=2)

        # Status text
        status_text_frame = ttk.Frame(status_row, style="Card.TFrame")
        status_text_frame.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="Status: Initializing...")
        self.status_label = ttk.Label(status_text_frame, textvariable=self.status_var, font=self.big_font, background=BeautifulStyle.CARD)
        self.status_label.pack(anchor="w")
        self.detail_var = tk.StringVar(value="Letzte Aktion: —")
        self.detail_label = ttk.Label(status_text_frame, textvariable=self.detail_var, font=self.medium_font, foreground=BeautifulStyle.MUTED, background=BeautifulStyle.CARD)
        self.detail_label.pack(anchor="w", pady=(6,0))

        # Progress / audio indicator
        self.audio_var = tk.StringVar(value="Audio: Stopped")
        self.audio_label = ttk.Label(status_card, textvariable=self.audio_var, font=self.medium_font, background=BeautifulStyle.CARD)
        self.audio_label.pack(anchor="w")
        self.progress = ttk.Progressbar(status_card, orient="horizontal", mode="indeterminate")
        self.progress.pack(fill="x", pady=(8,0))

        # Action buttons styled big
        actions = ttk.Frame(status_card, style="Card.TFrame", padding=(0,10))
        actions.pack(fill="x")
        self.btn_simulate = tk.Button(actions, text=" 🔔  Simulate Alarm", bg=BeautifulStyle.DANGER, fg="white", bd=0, font=self.medium_font, activebackground="#e74c3c", command=self.simulate_alarm)
        self.btn_simulate.pack(side="left", padx=6, ipadx=12, ipady=12, expand=True, fill="x")
        self.btn_mute = tk.Button(actions, text=" 🔇  Mute", bg="#f39c12", fg="black", bd=0, font=self.medium_font, command=self.gui_mute)
        self.btn_mute.pack(side="left", padx=6, ipadx=12, ipady=12, expand=True, fill="x")
        self.btn_reset = tk.Button(actions, text=" ✅  Reset", bg=BeautifulStyle.SUCCESS, fg="white", bd=0, font=self.medium_font, command=self.gui_reset)
        self.btn_reset.pack(side="left", padx=6, ipadx=12, ipady=12, expand=True, fill="x")

        # Compact settings bar
        settings_bar = ttk.Frame(left, style="Card.TFrame")
        settings_bar.pack(fill="x", pady=(14,0))
        ttk.Label(settings_bar, text="Alarm MP3", font=self.small_font, background=BeautifulStyle.BG).pack(side="left", padx=(0,6))
        self.mp3_var = tk.StringVar(value=ALARM_MP3)
        ttk.Entry(settings_bar, textvariable=self.mp3_var, width=48).pack(side="left", padx=6)
        ttk.Button(settings_bar, text="Browse", command=self.browse_mp3).pack(side="left", padx=6)
        ttk.Button(settings_bar, text="Save", command=self.save_settings).pack(side="left", padx=6)

        # Right column: history + quick toggles
        title_h = ttk.Label(right, text="Event History", font=self.medium_font, background=BeautifulStyle.BG)
        title_h.pack(anchor="w", padx=6, pady=(6,0))

        # Treeview for history (time, event)
        self.history_tv = ttk.Treeview(right, columns=("time", "event"), show="headings", height=18, style="History.Treeview")
        self.history_tv.heading("time", text="Time")
        self.history_tv.heading("event", text="Event")
        self.history_tv.column("time", width=120, anchor="w")
        self.history_tv.column("event", width=220, anchor="w")
        self.history_tv.pack(fill="both", expand=True, padx=6, pady=6)

        # Quick controls
        quick = ttk.Frame(right, style="App.TFrame")
        quick.pack(fill="x", padx=6, pady=6)
        ttk.Button(quick, text="LED On", command=self.force_led_on).pack(side="left", padx=4, ipadx=10)
        ttk.Button(quick, text="LED Off", command=self.force_led_off).pack(side="left", padx=4, ipadx=10)
        ttk.Button(quick, text="Toggle FS", command=self.toggle_fullscreen).pack(side="left", padx=4, ipadx=10)

        # Bind keyboard shortcuts
        root.bind("<Escape>", lambda e: self.toggle_fullscreen())
        root.bind("m", lambda e: self.gui_mute())
        root.bind("r", lambda e: self.gui_reset())
        root.bind("s", lambda e: self.simulate_alarm())

        # UI update loops
        self.led_pulse_phase = 0.0
        self.update_ui()
        self.root.after(700, self.update_history)

    def _draw_header(self):
        c = self.header_canvas
        w = c.winfo_reqwidth() or c.winfo_screenwidth()
        h = 110
        # Draw simple horizontal gradient (approximation)
        c.create_rectangle(0, 0, w, h, fill="#0b5e88", outline="")
        c.create_text(28, h/2, anchor="w", text=" EASYTEC", font=("Helvetica", 28, "bold"), fill="white")
        c.create_text(28, h/2 + 30, anchor="w", text="Industrial Alarm Console", font=("Helvetica", 10), fill="#dff3ff")

    def _led_update(self, active):
        # Pulsing glow when active
        canvas = self.led_canvas
        phase = self.led_pulse_phase
        if active:
            # pulse between 0.4 and 0.9
            import math
            pulse = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(phase))
            outer = int(60 * pulse)  # not used directly, but adjust color
            glow_color = "#ff6b6b"
            inner_color = "#ff4d4d"
            canvas.itemconfig(self._led_circle, fill=inner_color, outline="#cc2f2f")
            # simulate glow by drawing a translucent oval behind (approx via color alpha not supported -> use lighter colors)
            canvas.itemconfig(self._led_glow_big, fill="", outline="")
            canvas.itemconfig(self._led_glow_mid, fill="", outline="")
        else:
            canvas.itemconfig(self._led_circle, fill="#2ecc71", outline="#1e8f53")
            canvas.itemconfig(self._led_glow_big, fill="", outline="")
            canvas.itemconfig(self._led_glow_mid, fill="", outline="")
        self.led_pulse_phase += 0.3

    def simulate_alarm(self):
        add_history("Alarm (simulated) gestartet via GUI")
        self.detail_var.set("Letzte Aktion: Simulierter Alarm")
        alarm_ctrl.trigger_alarm(source="gui-simulate")

    def gui_mute(self):
        alarm_ctrl.mute()
        self.detail_var.set("Letzte Aktion: Mute")
        add_history("GUI: Mute gedrückt")

    def gui_reset(self):
        alarm_ctrl.reset()
        self.detail_var.set("Letzte Aktion: Reset")
        add_history("GUI: Reset gedrückt")

    def on_exit(self):
        if messagebox.askyesno("Exit", "Programm wirklich beenden?"):
            LOG.info("GUI Exit gedrückt")
            _stop_event.set()
            audio_cmd_q.put(("exit", None))
            try:
                self.root.quit()
            except Exception:
                pass

    def update_ui(self):
        with _state_lock:
            active = alarm_active
        self.status_var.set("Status: ALARM" if active else "Status: Ready")
        # update led glow/pulse
        self._led_update(active)

        # audio state
        with audio_playing_lock:
            ap = audio_playing
        self.audio_var.set(f"Audio: {'Playing' if ap else 'Stopped'}")
        if ap:
            try:
                self.progress.start(8)
            except Exception:
                pass
        else:
            try:
                self.progress.stop()
            except Exception:
                pass

        # schedule next update
        if not _stop_event.is_set():
            self.root.after(150, self.update_ui)

    def update_history(self):
        # sync history to treeview
        cur_items = self.history_tv.get_children()
        # clear and reinsert (keeps simple)
        for it in cur_items:
            self.history_tv.delete(it)
        for ts, ev in list(history)[:200]:
            self.history_tv.insert("", "end", values=(ts, ev))
        if not _stop_event.is_set():
            self.root.after(1000, self.update_history)

    def browse_mp3(self):
        path = filedialog.askopenfilename(title="Wähle Alarm-MP3", filetypes=[("MP3 Dateien", "*.mp3"), ("All files", "*.*")])
        if path:
            self.mp3_var.set(path)

    def save_settings(self):
        global ALARM_MP3, VOLUME_PERCENT, config
        ALARM_MP3 = self.mp3_var.get()
        config["ALARM_MP3"] = ALARM_MP3
        save_config(config)
        add_history("Einstellungen gespeichert")
        messagebox.showinfo("Settings", "Einstellungen wurden gespeichert.")

    def force_led_on(self):
        try:
            GPIO.output(PIN_output_LED, GPIO.HIGH)
            add_history("LED manuell eingeschaltet")
        except Exception:
            LOG.exception("LED on failed")

    def force_led_off(self):
        try:
            GPIO.output(PIN_output_LED, GPIO.LOW)
            add_history("LED manuell ausgeschaltet")
        except Exception:
            LOG.exception("LED off failed")

    def toggle_fullscreen(self):
        cur = self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", not cur)

# --- Signal handler ------------------------------------------------------
def signal_handler(sig, frame):
    LOG.info("Signal empfangen, beende sauber...")
    _stop_event.set()
    audio_cmd_q.put(("exit", None))
    try:
        for w in tk._default_root.children.values():
            try:
                w.quit()
            except Exception:
                pass
    except Exception:
        pass
    time.sleep(0.2)
    try:
        GPIO.cleanup()
    except Exception:
        pass
    LOG.info("GPIO cleaned up. Exit.")
    raise SystemExit(0)

# --- Start everything ----------------------------------------------------
def main():
    global ALARM_MP3
    LOG.info("Start application (SIMULATE_GPIO=%s)", SIMULATE_GPIO)

    if not os.path.exists(ALARM_MP3):
        LOG.warning("Alarm-MP3 nicht gefunden: %s", ALARM_MP3)
        add_history("Warnung: Alarm-MP3 nicht gefunden")

    # Start AudioWorker
    audio_worker = AudioWorker(audio_cmd_q, device=SOUND_DEVICE)
    audio_worker.start()

    # Hintergrund-Threads
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
        try:
            GPIO.cleanup()
        except Exception:
            pass
        LOG.info("Programm beendet")

if __name__ == "__main__":
    main()
