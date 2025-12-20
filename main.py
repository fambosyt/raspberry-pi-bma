#!/usr/bin/env python3
# v9 - Realistisches, dunkles & professionelles Design
# - dunkles Industrie-Theme (weniger weiß), realistischer LED-Glow, matte Panels
# - Beibehaltung Funktionalität: AudioWorker, AlarmController, MockGPIO, Config
#
# Hinweise:
# - Keine externen Bilddateien nötig (nur Canvas/Ebenen für Effekte)
# - Optional: später Icons / PNGs hinzufügen für noch realistischere Buttons

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

# Setup pins
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

# --- UI: realistic dark design -------------------------------------------
class RealisticTheme:
    BG = "#0d1114"        # deep near-black slate
    PANEL = "#14171b"     # panel metal
    CARD = "#1a1f23"      # card background
    ACCENT = "#0073b1"    # Siemens-like blue accent
    ACCENT2 = "#3298d1"
    TEXT = "#e6eef6"
    MUTED = "#9aa6b2"
    DANGER = "#ff6b6b"
    SUCCESS = "#2ecc71"
    SHADOW = "#0a0d0f"

    @staticmethod
    def apply(root, style: ttk.Style):
        root.configure(background=RealisticTheme.BG)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("App.TFrame", background=RealisticTheme.BG)
        style.configure("Card.TFrame", background=RealisticTheme.CARD, relief="flat")
        style.configure("Header.TLabel", background=RealisticTheme.BG, foreground=RealisticTheme.ACCENT, font=("Segoe UI", 20, "bold"))
        style.configure("SubHeader.TLabel", background=RealisticTheme.BG, foreground=RealisticTheme.MUTED, font=("Segoe UI", 10))
        style.configure("Accent.TButton", foreground="white", background=RealisticTheme.ACCENT, font=("Segoe UI", 11, "bold"))
        style.map("Accent.TButton", background=[("active", RealisticTheme.ACCENT2)])
        style.configure("History.Treeview", background=RealisticTheme.CARD, fieldbackground=RealisticTheme.CARD, foreground=RealisticTheme.TEXT)
        style.configure("Treeview.Heading", background=RealisticTheme.PANEL, foreground=RealisticTheme.TEXT)

class AlarmGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("EasyTec Alarm · Industrial")
        self.root.attributes("-fullscreen", True)
        try:
            self.root.config(cursor="none")
        except Exception:
            pass

        self.style = ttk.Style()
        RealisticTheme.apply(self.root, self.style)

        # Fonts
        self.title_font = font.Font(family="Segoe UI", size=20, weight="bold")
        self.large_font = font.Font(family="Segoe UI", size=16, weight="bold")
        self.normal_font = font.Font(family="Segoe UI", size=11)
        self.muted_font = font.Font(family="Segoe UI", size=10)

        # Top header (metal strip) built with Canvas to allow subtle gradient/reflection
        self.header = tk.Canvas(root, height=90, highlightthickness=0, bg=RealisticTheme.BG)
        self.header.pack(fill="x")
        self._draw_metal_header()

        # Main area
        main = ttk.Frame(root, style="App.TFrame", padding=16)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, style="App.TFrame")
        left.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(main, width=360, style="App.TFrame")
        right.pack(side="right", fill="y")

        # Big card (status)
        status_card = tk.Frame(left, bg=RealisticTheme.CARD, bd=0, highlightthickness=0)
        status_card.pack(fill="both", expand=True, padx=(0,12), pady=6)

        # Title
        lbl_title = tk.Label(status_card, text="SYSTEM STATUS", bg=RealisticTheme.CARD, fg=RealisticTheme.ACCENT, font=self.title_font)
        lbl_title.pack(anchor="w", padx=18, pady=(12,0))
        lbl_sub = tk.Label(status_card, text="Industrial Alarm Console", bg=RealisticTheme.CARD, fg=RealisticTheme.MUTED, font=self.muted_font)
        lbl_sub.pack(anchor="w", padx=18, pady=(0,10))

        # Status row
        status_row = tk.Frame(status_card, bg=RealisticTheme.CARD)
        status_row.pack(fill="x", padx=18, pady=(6,12))

        # LED assembly: multiple layered ovals for glow + reflection for realism
        self.led_canvas = tk.Canvas(status_row, width=140, height=140, bg=RealisticTheme.CARD, highlightthickness=0)
        self.led_canvas.pack(side="left", padx=(0,20))
        # draw layered ovals (initially green/off)
        self._led_glow_layers = []
        glow_colors = ["#052020", "#06343a", "#075a50"]  # base subtle glows
        for i, color in enumerate(glow_colors):
            oval = self.led_canvas.create_oval(6+i*6, 6+i*6, 134-i*6, 134-i*6, fill=color, outline="")
            self._led_glow_layers.append(oval)
        # inner reflective circle
        self._led_inner = self.led_canvas.create_oval(40, 40, 100, 100, fill="#2ecc71", outline="#1e8f53", width=2)
        # sheen (reflection) - light arc
        self._led_sheen = self.led_canvas.create_arc(40, 20, 100, 70, start=20, extent=120, style="pieslice", fill="#ffffff22", outline="")

        # status texts
        txt_frame = tk.Frame(status_row, bg=RealisticTheme.CARD)
        txt_frame.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="Status: Initializing...")
        tk.Label(txt_frame, textvariable=self.status_var, bg=RealisticTheme.CARD, fg=RealisticTheme.TEXT, font=self.large_font).pack(anchor="w")
        self.detail_var = tk.StringVar(value="Letzte Aktion: —")
        tk.Label(txt_frame, textvariable=self.detail_var, bg=RealisticTheme.CARD, fg=RealisticTheme.MUTED, font=self.normal_font).pack(anchor="w", pady=(6,0))

        # Audio indicator & progress
        self.audio_var = tk.StringVar(value="Audio: Stopped")
        tk.Label(status_card, textvariable=self.audio_var, bg=RealisticTheme.CARD, fg=RealisticTheme.TEXT, font=self.normal_font).pack(anchor="w", padx=18)
        self.progress = ttk.Progressbar(status_card, orient="horizontal", mode="indeterminate")
        self.progress.pack(fill="x", padx=18, pady=(8,12))

        # Action buttons: realistic flat buttons with subtle borders
        actions = tk.Frame(status_card, bg=RealisticTheme.CARD)
        actions.pack(fill="x", padx=18, pady=(6,16))

        self.btn_simulate = tk.Button(actions, text="  🔔  ALARM  ", bg=RealisticTheme.DANGER, fg="white", activebackground="#ff7b7b", bd=0, font=self.large_font, command=self.simulate_alarm)
        self.btn_simulate.pack(side="left", expand=True, fill="x", padx=6, ipadx=6, ipady=12)

        self.btn_mute = tk.Button(actions, text="  🔇  MUTE  ", bg="#bf9b3b", fg="black", bd=0, font=self.normal_font, command=self.gui_mute)
        self.btn_mute.pack(side="left", expand=True, fill="x", padx=6, ipadx=6, ipady=12)

        self.btn_reset = tk.Button(actions, text="  ✔  RESET  ", bg=RealisticTheme.SUCCESS, fg="white", bd=0, font=self.normal_font, command=self.gui_reset)
        self.btn_reset.pack(side="left", expand=True, fill="x", padx=6, ipadx=6, ipady=12)

        # compact settings row
        settings_row = tk.Frame(left, bg=RealisticTheme.CARD)
        settings_row.pack(fill="x", padx=18, pady=(6,12))
        tk.Label(settings_row, text="Alarm MP3", bg=RealisticTheme.CARD, fg=RealisticTheme.MUTED, font=self.normal_font).pack(side="left")
        self.mp3_var = tk.StringVar(value=ALARM_MP3)
        tk.Entry(settings_row, textvariable=self.mp3_var, width=56, bg="#0f1417", fg=RealisticTheme.TEXT, insertbackground=RealisticTheme.TEXT).pack(side="left", padx=8)
        tk.Button(settings_row, text="Browse", command=self.browse_mp3, bg=RealisticTheme.PANEL, fg=RealisticTheme.TEXT, bd=0).pack(side="left", padx=6)

        # Right column: realistic history card
        right_card = tk.Frame(right, bg=RealisticTheme.CARD)
        right_card.pack(fill="both", expand=True, padx=6, pady=6)
        tk.Label(right_card, text="Event History", bg=RealisticTheme.CARD, fg=RealisticTheme.TEXT, font=self.normal_font).pack(anchor="w", padx=8, pady=(8,0))

        # Treeview-like list but styled for dark theme
        self.history_list = tk.Listbox(right_card, bg="#0f1417", fg=RealisticTheme.TEXT, selectbackground="#223344", borderwidth=0)
        self.history_list.pack(fill="both", expand=True, padx=8, pady=8)

        # Quick controls
        quick = tk.Frame(right_card, bg=RealisticTheme.CARD)
        quick.pack(fill="x", padx=8, pady=(0,12))
        tk.Button(quick, text="LED ON", command=self.force_led_on, bg=RealisticTheme.PANEL, fg=RealisticTheme.TEXT, bd=0).pack(side="left", padx=6, ipadx=8)
        tk.Button(quick, text="LED OFF", command=self.force_led_off, bg=RealisticTheme.PANEL, fg=RealisticTheme.TEXT, bd=0).pack(side="left", padx=6, ipadx=8)
        tk.Button(quick, text="Fullscreen", command=self.toggle_fullscreen, bg=RealisticTheme.PANEL, fg=RealisticTheme.TEXT, bd=0).pack(side="left", padx=6, ipadx=8)

        # Keyboard bindings
        root.bind("<Escape>", lambda e: self.toggle_fullscreen())
        root.bind("m", lambda e: self.gui_mute())
        root.bind("r", lambda e: self.gui_reset())
        root.bind("s", lambda e: self.simulate_alarm())

        # LED pulse params
        self._led_phase = 0.0

        # Start updates
        self.update_ui()
        self.root.after(700, self.update_history)

    def _draw_metal_header(self):
        c = self.header
        w = c.winfo_screenwidth()
        h = 90
        # Dark metallic gradient band
        for i in range(0, 90, 3):
            col = self._lerp_color("#0b2230", "#07202a", i/90)
            c.create_rectangle(0, i, w, i+3, fill=col, outline=col)
        # Logo text
        c.create_text(28, h/2, anchor="w", text="EASYTEC", font=("Segoe UI", 26, "bold"), fill="#dff6ff")
        c.create_text(28, h/2 + 26, anchor="w", text="Industrial Alarm Console", font=("Segoe UI", 9), fill="#9fbfcf")

    @staticmethod
    def _lerp_color(a, b, t):
        # linear interpolate hex colors a->b
        a = a.lstrip("#"); b = b.lstrip("#")
        ar = int(a[0:2],16); ag = int(a[2:4],16); ab = int(a[4:6],16)
        br = int(b[0:2],16); bg = int(b[2:4],16); bb = int(b[4:6],16)
        rr = int(ar + (br-ar)*t); rg = int(ag + (bg-ag)*t); rb = int(ab + (bb-ab)*t)
        return f"#{rr:02x}{rg:02x}{rb:02x}"

    def _led_render(self, active):
        # Update layered glow + inner color to simulate a realistic indicator
        cvs = self.led_canvas
        # pulse when active
        if active:
            import math
            self._led_phase += 0.25
            pulse = 0.5 + 0.5 * (0.5 + 0.5 * math.sin(self._led_phase))
            # adjust layers' colors by creating lighter shades
            shades = [
                self._shade("#061a1a", pulse*0.6),
                self._shade("#083232", pulse*0.45),
                self._shade("#0b4a46", pulse*0.35)
            ]
            for item, col in zip(self._led_glow_layers, shades):
                cvs.itemconfig(item, fill=col)
            # inner red for alarm
            cvs.itemconfig(self._led_inner, fill="#ff4d4d", outline="#b32f2f")
            cvs.itemconfig(self._led_sheen, fill="#ffffff22")
        else:
            # quiet green
            for i, item in enumerate(self._led_glow_layers):
                cvs.itemconfig(item, fill=["#061212","#07201a","#0b3a2f"][i])
            cvs.itemconfig(self._led_inner, fill="#2ecc71", outline="#1e8f53")
            cvs.itemconfig(self._led_sheen, fill="#ffffff15")

    @staticmethod
    def _shade(hexcolor, factor):
        # darken or lighten hexcolor by factor in [-1..1], positive -> lighten
        hexcolor = hexcolor.lstrip("#")
        r = int(hexcolor[0:2],16); g = int(hexcolor[2:4],16); b = int(hexcolor[4:6],16)
        def clip(x): return max(0, min(255, int(x)))
        if factor >= 0:
            r = clip(r + (255 - r) * factor)
            g = clip(g + (255 - g) * factor)
            b = clip(b + (255 - b) * factor)
        else:
            r = clip(r * (1 + factor))
            g = clip(g * (1 + factor))
            b = clip(b * (1 + factor))
        return f"#{r:02x}{g:02x}{b:02x}"

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
        self._led_render(active)

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

        if not _stop_event.is_set():
            self.root.after(150, self.update_ui)

    def update_history(self):
        self.history_list.delete(0, tk.END)
        for ts, ev in list(history)[:200]:
            self.history_list.insert(tk.END, f"{ts}  •  {ev}")
        if not _stop_event.is_set():
            self.root.after(1000, self.update_history)

    def browse_mp3(self):
        path = filedialog.askopenfilename(title="Wähle Alarm-MP3", filetypes=[("MP3 Dateien", "*.mp3"), ("All files", "*.*")])
        if path:
            self.mp3_var.set(path)

    def save_settings(self):
        global ALARM_MP3, config
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
