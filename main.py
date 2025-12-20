#!/usr/bin/env python3
# v10-fix2 - Fix: header draw + robustere Audio-Play-Fehlerbehandlung
# (Beinhaltet vorheriges v10-Design + Fehlerbehebungen)
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
import sys

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font

# Versuche RPi.GPIO zu importieren, ansonsten Simulation
try:
    import RPi.GPIO as GPIO  # type: ignore
    IS_RPI = True
except Exception:
    IS_RPI = False

# Lokaler Audio-Player (vom Projekt)
try:
    from audio import SoundPlayer
except Exception:
    SoundPlayer = None  # defensive fallback

# --- Logging --------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("easytec-alarm")

# --- Configuration -------------------------------------------------------
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
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
    except Exception:
        LOG.exception("Fehler beim Laden der Konfiguration - verwende Defaults")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        LOG.exception("Fehler beim Speichern der Konfiguration")


config = load_config()
ALARM_MP3 = config.get("ALARM_MP3", DEFAULT_CONFIG["ALARM_MP3"])
SOUND_DEVICE = config.get("SOUND_DEVICE", DEFAULT_CONFIG["SOUND_DEVICE"])
VOLUME_PERCENT = int(config.get("VOLUME_PERCENT", DEFAULT_CONFIG["VOLUME_PERCENT"]))
SIMULATE_GPIO = bool(config.get("SIMULATE_GPIO", DEFAULT_CONFIG["SIMULATE_GPIO"]))

# --- GPIO pins (BOARD numbering) ------------------------------------------
PIN_OUTPUT_BUZ = 18
PIN_OUTPUT_LED = 16
PIN_BTN_ALARM = 36
PIN_BTN_RESET = 37

# --- Mock GPIO ------------------------------------------------------------
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
        LOG.info("MockGPIO aktiviert (Simulationsmodus)")

    def setmode(self, mode):
        LOG.debug("MockGPIO.setmode(%s)", mode)

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
        LOG.debug("MockGPIO.output pin=%s value=%s", pin, value)

    def cleanup(self):
        LOG.info("MockGPIO.cleanup")

    # Hilfsmethode für Tests
    def set_input(self, pin, value):
        self._inputs[pin] = value

# Wähle GPIO-Implementierung
if IS_RPI and not SIMULATE_GPIO:
    try:
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
    except Exception:
        LOG.exception("GPIO-Init fehlgeschlagen - wechsle zu MockGPIO")
        GPIO = MockGPIO()
        SIMULATE_GPIO = True
else:
    GPIO = MockGPIO()
    SIMULATE_GPIO = True

# Setup Pins sicher
try:
    GPIO.setup(PIN_OUTPUT_BUZ, GPIO.OUT)
    GPIO.setup(PIN_OUTPUT_LED, GPIO.OUT)
    GPIO.setup(PIN_BTN_ALARM, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_BTN_RESET, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    try:
        GPIO.output(PIN_OUTPUT_BUZ, GPIO.LOW)
        GPIO.output(PIN_OUTPUT_LED, GPIO.LOW)
    except Exception:
        pass
except Exception:
    LOG.exception("Fehler beim Konfigurieren der GPIO-Pins")

# --- Threading & State ---------------------------------------------------
_state_lock = threading.Lock()
alarm_active = False

audio_cmd_q = queue.Queue()
_stop_event = threading.Event()

audio_playing = False
audio_playing_lock = threading.Lock()

history = deque(maxlen=500)


def add_history(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history.appendleft((ts, msg))
    LOG.info(msg)

# --- Volume helper -------------------------------------------------------
def set_system_volume(percent: int):
    try:
        percent = max(0, min(100, int(percent)))
        # Versuche ALSA amixer
        subprocess.run(["amixer", "sset", "PCM", f"{percent}%"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        LOG.info("System volume gesetzt: %d%%", percent)
    except Exception:
        LOG.debug("amixer nicht verfügbar oder Fehler beim Setzen der Lautstärke")


set_system_volume(VOLUME_PERCENT)

# --- AudioWorker ---------------------------------------------------------
class AudioWorker(threading.Thread):
    def __init__(self, cmd_queue: queue.Queue, device: str = None):
        super().__init__(daemon=True)
        self.cmd_queue = cmd_queue
        self.device = device
        self.player = None

    def _safe_play(self, filepath):
        """
        Versucht player.play mit verschiedenen Signaturen, loggt Fehler, beendet sauber.
        """
        try:
            # erster Versuch: gängige Signatur mit interrupt
            self.player.play(filepath, interrupt=True)
        except TypeError:
            # play existiert, aber Signatur unterscheidet sich -> versuch ohne interrupt
            try:
                self.player.play(filepath)
            except Exception:
                LOG.exception("Fehler beim Abspielen (fallback ohne interrupt)")
        except AttributeError:
            LOG.exception("Player hat keine 'play' Methode")
        except Exception:
            LOG.exception("Fehler beim Abspielen der Datei")

    def run(self):
        global audio_playing
        LOG.info("AudioWorker startet (device=%s)", self.device or "<auto>")
        if SoundPlayer is not None:
            try:
                self.player = SoundPlayer(device=self.device)
            except Exception:
                LOG.exception("SoundPlayer-Initialisierung fehlgeschlagen")
                self.player = None
        else:
            LOG.warning("Kein SoundPlayer verfügbar - Audio-Funktionen deaktiviert")
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
                        with audio_playing_lock:
                            audio_playing = True
                        LOG.info("Spiele: %s", filepath)
                        self._safe_play(filepath)
                    finally:
                        with audio_playing_lock:
                            audio_playing = False
                else:
                    LOG.error("Kein Player vorhanden")
                    add_history("Audio: Kein Player vorhanden")
            elif cmd == "stop":
                if self.player:
                    try:
                        # einige Player haben stop(), andere nicht
                        stop_fn = getattr(self.player, "stop", None)
                        if callable(stop_fn):
                            stop_fn()
                            LOG.info("Audio gestoppt")
                    except Exception:
                        LOG.exception("Fehler beim Stoppen")
                with audio_playing_lock:
                    audio_playing = False
                add_history("Audio: gestoppt")
            elif cmd == "set_volume":
                try:
                    set_system_volume(int(arg))
                    add_history(f"Lautstärke gesetzt: {arg}%")
                except Exception:
                    LOG.exception("Fehler beim Setzen der Lautstärke")
            elif cmd == "exit":
                LOG.info("AudioWorker: exit")
                break

        # Cleanup
        if self.player:
            try:
                stop_fn = getattr(self.player, "stop", None)
                if callable(stop_fn):
                    stop_fn()
            except Exception:
                pass
        with audio_playing_lock:
            audio_playing = False
        LOG.info("AudioWorker beendet")

# --- AlarmController -----------------------------------------------------
class AlarmController:
    def __init__(self):
        self._lock = threading.Lock()

    def trigger(self, source="external"):
        global alarm_active
        with self._lock:
            with _state_lock:
                if alarm_active:
                    LOG.debug("Alarm bereits aktiv - Ignoriere Trigger")
                    add_history("Alarm-Trigger ignoriert (bereits aktiv)")
                    return
                alarm_active = True
            add_history(f"Alarm ausgelöst ({source})")
            try:
                GPIO.output(PIN_OUTPUT_BUZ, GPIO.HIGH)
                GPIO.output(PIN_OUTPUT_LED, GPIO.HIGH)
            except Exception:
                LOG.exception("GPIO write failed on trigger")
            audio_cmd_q.put(("play", ALARM_MP3))
            threading.Thread(target=self._wait_for_reset, daemon=True).start()

    def _wait_for_reset(self):
        press_count = 0
        try:
            while not _stop_event.is_set():
                if GPIO.input(PIN_BTN_RESET) == GPIO.HIGH:
                    time.sleep(0.05)
                    if GPIO.input(PIN_BTN_RESET) == GPIO.HIGH:
                        press_count += 1
                        add_history(f"Reset-Taste gedrückt ({press_count})")
                        # warte bis losgelassen
                        while GPIO.input(PIN_BTN_RESET) == GPIO.HIGH and not _stop_event.is_set():
                            time.sleep(0.05)
                        if press_count == 1:
                            self.mute()
                            add_history("Aktion: Mute (1. Druck)")
                        elif press_count >= 2:
                            self.reset()
                            add_history("Aktion: Reset (2. Druck)")
                            break
                time.sleep(0.1)
        except Exception:
            LOG.exception("Fehler im Alarm-Wait-Handler")
            self.reset()

    def mute(self):
        try:
            GPIO.output(PIN_OUTPUT_BUZ, GPIO.LOW)
        except Exception:
            LOG.exception("Fehler beim Deaktivieren des Buzzers")
        audio_cmd_q.put(("stop", None))
        add_history("Aktion: Mute (GUI/Hardware)")

    def reset(self):
        global alarm_active
        audio_cmd_q.put(("stop", None))
        try:
            GPIO.output(PIN_OUTPUT_BUZ, GPIO.LOW)
            GPIO.output(PIN_OUTPUT_LED, GPIO.LOW)
        except Exception:
            LOG.exception("Fehler beim Setzen der Ausgänge")
        with _state_lock:
            alarm_active = False
        add_history("Aktion: Reset - System reaktiviert")


alarm_ctrl = AlarmController()

# --- Background threads --------------------------------------------------
def status_led_loop():
    while not _stop_event.is_set():
        with _state_lock:
            active = alarm_active
        if active:
            try:
                GPIO.output(PIN_OUTPUT_LED, GPIO.HIGH)
            except Exception:
                pass
            time.sleep(0.5)
            continue
        # Blinken im Idle
        try:
            GPIO.output(PIN_OUTPUT_LED, GPIO.HIGH)
            time.sleep(0.12)
            GPIO.output(PIN_OUTPUT_LED, GPIO.LOW)
        except Exception:
            pass
        for _ in range(30):
            if _stop_event.is_set():
                break
            time.sleep(0.1)


def monitor_alarm_button():
    while not _stop_event.is_set():
        try:
            if GPIO.input(PIN_BTN_ALARM) == GPIO.HIGH:
                time.sleep(0.05)
                if GPIO.input(PIN_BTN_ALARM) == GPIO.HIGH:
                    add_history("Hardware-Alarm-Taste gedrückt")
                    alarm_ctrl.trigger(source="hardware-button")
                    # wait until released
                    while GPIO.input(PIN_BTN_ALARM) == GPIO.HIGH and not _stop_event.is_set():
                        time.sleep(0.05)
            time.sleep(0.1)
        except Exception:
            LOG.exception("Fehler in monitor_alarm_button")
            time.sleep(0.5)

# --- UI / Theme -----------------------------------------------------------
class DarkIndustrial:
    BG = "#0e1113"
    PANEL = "#151719"
    CARD = "#1b1f22"
    ACCENT = "#0078a8"
    DANGER = "#d94d4d"
    SUCCESS = "#2fb36b"
    TEXT = "#e6eef3"
    MUTED = "#94a3ad"

    @staticmethod
    def apply(root: tk.Tk, style: ttk.Style):
        root.configure(bg=DarkIndustrial.BG)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=DarkIndustrial.BG)
        style.configure("Card.TFrame", background=DarkIndustrial.CARD, relief="flat")
        style.configure("TLabel", background=DarkIndustrial.BG, foreground=DarkIndustrial.TEXT)
        style.configure("TButton", background=DarkIndustrial.PANEL, foreground=DarkIndustrial.TEXT)

# --- Main GUI ------------------------------------------------------------
class AlarmApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EasyTec Alarm Console")
        # Try fullscreen, but handle platforms that forbid it
        try:
            self.root.attributes("-fullscreen", True)
        except Exception:
            pass
        try:
            self.root.config(cursor="none")
        except Exception:
            pass

        self.style = ttk.Style()
        DarkIndustrial.apply(self.root, self.style)

        # fonts
        self.h1 = font.Font(family="Segoe UI", size=22, weight="bold")
        self.h2 = font.Font(family="Segoe UI", size=14, weight="bold")
        self.normal = font.Font(family="Segoe UI", size=11)

        # layout
        header = tk.Frame(self.root, bg=DarkIndustrial.PANEL, height=80)
        header.pack(fill="x")
        # robust header drawing
        try:
            self._draw_header(header)
        except Exception:
            LOG.exception("Header-Drawing fehlgeschlagen (fortfahren ohne Header)")

        body = tk.Frame(self.root, bg=DarkIndustrial.BG, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=DarkIndustrial.BG)
        left.pack(side="left", fill="both", expand=True)

        right = tk.Frame(body, bg=DarkIndustrial.BG, width=360)
        right.pack(side="right", fill="y")

        # main status card
        card = tk.Frame(left, bg=DarkIndustrial.CARD, bd=0)
        card.pack(fill="both", expand=True, padx=(0,12), pady=6)

        # top labels
        tk.Label(card, text="SYSTEM STATUS", font=self.h1, fg=DarkIndustrial.ACCENT, bg=DarkIndustrial.CARD).pack(anchor="w", padx=18, pady=(18,0))
        tk.Label(card, text="Industrial Alarm Console · Ready", font=self.normal, fg=DarkIndustrial.MUTED, bg=DarkIndustrial.CARD).pack(anchor="w", padx=18, pady=(2,12))

        # status area
        status_row = tk.Frame(card, bg=DarkIndustrial.CARD)
        status_row.pack(fill="x", padx=18, pady=(6,12))

        # realistic LED
        self.led_canvas = tk.Canvas(status_row, width=160, height=160, bg=DarkIndustrial.CARD, highlightthickness=0)
        self.led_canvas.pack(side="left", padx=(0,18))
        self._create_led_art()

        # text block
        txt = tk.Frame(status_row, bg=DarkIndustrial.CARD)
        txt.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="Status: Initializing...")
        tk.Label(txt, textvariable=self.status_var, font=self.h2, fg=DarkIndustrial.TEXT, bg=DarkIndustrial.CARD).pack(anchor="w")
        self.detail_var = tk.StringVar(value="Letzte Aktion: —")
        tk.Label(txt, textvariable=self.detail_var, font=self.normal, fg=DarkIndustrial.MUTED, bg=DarkIndustrial.CARD).pack(anchor="w", pady=(6,0))

        # audio info
        self.audio_var = tk.StringVar(value="Audio: Stopped")
        tk.Label(card, textvariable=self.audio_var, bg=DarkIndustrial.CARD, fg=DarkIndustrial.TEXT, font=self.normal).pack(anchor="w", padx=18)
        self.progress = ttk.Progressbar(card, orient="horizontal", mode="indeterminate")
        self.progress.pack(fill="x", padx=18, pady=(8,12))

        # big action buttons
        btn_row = tk.Frame(card, bg=DarkIndustrial.CARD)
        btn_row.pack(fill="x", padx=18, pady=(6,18))
        self.btn_alarm = tk.Button(btn_row, text="  🔔  ALARM  ", bg=DarkIndustrial.DANGER, fg="white", bd=0, font=self.h2, command=self.on_simulate)
        self.btn_alarm.pack(side="left", expand=True, fill="x", padx=6, ipadx=4, ipady=12)
        self.btn_mute = tk.Button(btn_row, text="  🔇  MUTE  ", bg="#b87e2a", fg="black", bd=0, font=self.normal, command=self.on_mute)
        self.btn_mute.pack(side="left", expand=True, fill="x", padx=6, ipadx=4, ipady=12)
        self.btn_reset = tk.Button(btn_row, text="  ✔  RESET  ", bg=DarkIndustrial.SUCCESS, fg="white", bd=0, font=self.normal, command=self.on_reset)
        self.btn_reset.pack(side="left", expand=True, fill="x", padx=6, ipadx=4, ipady=12)

        # settings row
        settings = tk.Frame(left, bg=DarkIndustrial.CARD)
        settings.pack(fill="x", padx=18, pady=(6,12))
        tk.Label(settings, text="Alarm MP3", bg=DarkIndustrial.CARD, fg=DarkIndustrial.MUTED, font=self.normal).pack(side="left")
        self.mp3_var = tk.StringVar(value=ALARM_MP3)
        ent = tk.Entry(settings, textvariable=self.mp3_var, width=56, bg="#0f1417", fg=DarkIndustrial.TEXT, insertbackground=DarkIndustrial.TEXT)
        ent.pack(side="left", padx=8)
        tk.Button(settings, text="Browse", command=self.browse_mp3, bg=DarkIndustrial.PANEL, fg=DarkIndustrial.TEXT, bd=0).pack(side="left", padx=6)
        tk.Button(settings, text="Save", command=self.save_settings, bg=DarkIndustrial.PANEL, fg=DarkIndustrial.TEXT, bd=0).pack(side="left", padx=6)

        # right column: History & quick
        tk.Label(right, text="Event History", bg=DarkIndustrial.BG, fg=DarkIndustrial.MUTED, font=self.normal).pack(anchor="w", padx=8, pady=(6,0))
        self.history_lv = tk.Listbox(right, bg="#0f1417", fg=DarkIndustrial.TEXT, borderwidth=0)
        self.history_lv.pack(fill="both", expand=True, padx=8, pady=8)

        quick = tk.Frame(right, bg=DarkIndustrial.BG)
        quick.pack(fill="x", padx=8, pady=(0,12))
        tk.Button(quick, text="LED ON", command=self.force_led_on, bg=DarkIndustrial.PANEL, fg=DarkIndustrial.TEXT, bd=0).pack(side="left", padx=6)
        tk.Button(quick, text="LED OFF", command=self.force_led_off, bg=DarkIndustrial.PANEL, fg=DarkIndustrial.TEXT, bd=0).pack(side="left", padx=6)
        tk.Button(quick, text="Exit", command=self.on_exit, bg=DarkIndustrial.PANEL, fg=DarkIndustrial.TEXT, bd=0).pack(side="right", padx=6)

        # keyboard shortcuts
        self.root.bind("<Escape>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Key-s>", lambda e: self.on_simulate())
        self.root.bind("<Key-m>", lambda e: self.on_mute())
        self.root.bind("<Key-r>", lambda e: self.on_reset())

        # animation state
        self._led_phase = 0.0

        # start ui loops
        self.update_ui()
        self.root.after(800, self.update_history)

    # Header drawing (robust)
    def _draw_header(self, parent):
        try:
            c = tk.Canvas(parent, height=90, bg=DarkIndustrial.PANEL, highlightthickness=0)
            c.pack(fill="both", expand=True)
            w = c.winfo_screenwidth() or 1024
            h = 90
            # simple horizontal gradient approximation
            for i in range(0, h, 3):
                t = i / max(1, h)
                col = self._lerp_color("#0b2230", "#07202a", t)
                c.create_rectangle(0, i, w, i + 3, fill=col, outline=col)
            c.create_text(28, h / 2, anchor="w", text="EASYTEC", font=("Segoe UI", 26, "bold"), fill="#dff6ff")
            c.create_text(28, h / 2 + 26, anchor="w", text="Industrial Alarm Console", font=("Segoe UI", 9), fill="#9fbfcf")
        except Exception:
            LOG.exception("Fehler in _draw_header")

    @staticmethod
    def _lerp_color(a, b, t):
        a = a.lstrip("#"); b = b.lstrip("#")
        ar = int(a[0:2], 16); ag = int(a[2:4], 16); ab = int(a[4:6], 16)
        br = int(b[0:2], 16); bg = int(b[2:4], 16); bb = int(b[4:6], 16)
        rr = int(ar + (br - ar) * t); rg = int(ag + (bg - ag) * t); rb = int(ab + (bb - ab) * t)
        return f"#{rr:02x}{rg:02x}{rb:02x}"

    def _create_led_art(self):
        c = self.led_canvas
        # base metallic ring
        c.create_oval(6, 6, 154, 154, fill="#0f1213", outline="#2b2b2b", width=6)
        c.create_oval(18, 18, 138, 138, fill="#0b1010", outline="#202426", width=2)
        # glow layers
        self._glow_items = [
            c.create_oval(28, 28, 128, 128, fill="#081212", outline=""),
            c.create_oval(36, 36, 120, 120, fill="#093030", outline=""),
        ]
        # center light
        self._center = c.create_oval(46, 46, 110, 110, fill="#2ecc71", outline="#1e8f53", width=2)
        self._sheen = c.create_arc(46, 30, 110, 86, start=20, extent=130, style="pieslice", fill="#ffffff15", outline="")

    def _led_render(self, active: bool):
        c = self.led_canvas
        if active:
            import math
            self._led_phase += 0.28
            pulse = 0.6 + 0.35 * (0.5 + 0.5 * math.sin(self._led_phase))
            glow1 = self._shade("#0b2b2b", pulse * 0.6)
            glow2 = self._shade("#0f3a36", pulse * 0.45)
            c.itemconfig(self._glow_items[0], fill=glow1)
            c.itemconfig(self._glow_items[1], fill=glow2)
            c.itemconfig(self._center, fill="#ff4d4d", outline="#b32f2f")
            c.itemconfig(self._sheen, fill="#ffffff22")
        else:
            c.itemconfig(self._glow_items[0], fill="#081212")
            c.itemconfig(self._glow_items[1], fill="#093030")
            c.itemconfig(self._center, fill="#2ecc71", outline="#1e8f53")
            c.itemconfig(self._sheen, fill="#ffffff10")

    @staticmethod
    def _shade(hexcolor, factor):
        hexcolor = hexcolor.lstrip("#")
        r = int(hexcolor[0:2], 16); g = int(hexcolor[2:4], 16); b = int(hexcolor[4:6], 16)
        def clip(x): return max(0, min(255, int(x)))
        if factor >= 0:
            r = clip(r + (255 - r) * factor)
            g = clip(g + (255 - g) * factor)
            b = clip(b + (255 - b) * factor)
        else:
            r = clip(r * (1 + factor)); g = clip(g * (1 + factor)); b = clip(b * (1 + factor))
        return f"#{r:02x}{g:02x}{b:02x}"

    # UI actions
    def on_simulate(self):
        add_history("Alarm simuliert (GUI)")
        self.detail_var.set("Letzte Aktion: Simulierter Alarm")
        alarm_ctrl.trigger(source="gui-simulate")

    def on_mute(self):
        alarm_ctrl.mute()
        add_history("Mute über GUI")
        self.detail_var.set("Letzte Aktion: Mute")

    def on_reset(self):
        alarm_ctrl.reset()
        add_history("Reset über GUI")
        self.detail_var.set("Letzte Aktion: Reset")

    def browse_mp3(self):
        p = filedialog.askopenfilename(title="Wähle Alarm-MP3", filetypes=[("MP3 Dateien", "*.mp3"), ("Alle Dateien", "*.*")])
        if p:
            self.mp3_var.set(p)

    def save_settings(self):
        global ALARM_MP3, VOLUME_PERCENT, config
        ALARM_MP3 = self.mp3_var.get()
        config["ALARM_MP3"] = ALARM_MP3
        save_config(config)
        add_history("Einstellungen gespeichert")
        messagebox.showinfo("Settings", "Einstellungen gespeichert")

    def force_led_on(self):
        try:
            GPIO.output(PIN_OUTPUT_LED, GPIO.HIGH)
            add_history("LED manuell an")
        except Exception:
            LOG.exception("LED ON failed")

    def force_led_off(self):
        try:
            GPIO.output(PIN_OUTPUT_LED, GPIO.LOW)
            add_history("LED manuell aus")
        except Exception:
            LOG.exception("LED OFF failed")

    def on_exit(self):
        if messagebox.askokcancel("Beenden", "Programm wirklich beenden?"):
            LOG.info("Beende via GUI")
            _stop_event.set()
            audio_cmd_q.put(("exit", None))
            try:
                self.root.quit()
            except Exception:
                pass

    def toggle_fullscreen(self):
        try:
            cur = self.root.attributes("-fullscreen")
            self.root.attributes("-fullscreen", not cur)
        except Exception:
            pass

    def update_ui(self):
        with _state_lock:
            active = alarm_active
        self.status_var.set("Status: ALARM" if active else "Status: Ready")
        self._led_render(active)

        with audio_playing_lock:
            ap = audio_playing
        self.audio_var.set("Audio: Playing" if ap else "Audio: Stopped")
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

        # update history entry preview
        if history:
            ts, msg = history[0]
            self.detail_var.set(f"Letzte Aktion: {msg} ({ts})")

        if not _stop_event.is_set():
            self.root.after(150, self.update_ui)

    def update_history(self):
        self.history_lv.delete(0, tk.END)
        for ts, msg in list(history)[:200]:
            self.history_lv.insert(tk.END, f"{ts} • {msg}")
        if not _stop_event.is_set():
            self.root.after(1000, self.update_history)

# --- Signal handling -----------------------------------------------------
def _safe_quit_tk():
    try:
        root = getattr(tk, "_default_root", None)
        if root:
            try:
                root.quit()
            except Exception:
                pass
    except Exception:
        pass


def signal_handler(sig, frame):
    LOG.info("Signal empfangen: beende sauber...")
    _stop_event.set()
    audio_cmd_q.put(("exit", None))
    _safe_quit_tk()
    time.sleep(0.2)
    try:
        GPIO.cleanup()
    except Exception:
        pass
    LOG.info("Cleanup done - exit")
    try:
        sys.exit(0)
    except SystemExit:
        raise

# --- Main ----------------------------------------------------------------
def main():
    global ALARM_MP3
    LOG.info("Starte Anwendung (SIMULATE_GPIO=%s)", SIMULATE_GPIO)
    if not os.path.exists(ALARM_MP3):
        LOG.warning("Alarm-MP3 nicht gefunden: %s", ALARM_MP3)
        add_history("Warnung: Alarm-MP3 nicht gefunden")

    # Audio Worker starten
    audio_worker = AudioWorker(audio_cmd_q, device=SOUND_DEVICE)
    audio_worker.start()

    # Hintergrundthreads
    t1 = threading.Thread(target=status_led_loop, daemon=True)
    t1.start()
    t2 = threading.Thread(target=monitor_alarm_button, daemon=True)
    t2.start()

    # Signals
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Tk root
    try:
        root = tk.Tk()
    except Exception:
        LOG.exception("Tkinter Root konnte nicht erstellt werden")
        raise

    app = AlarmApp(root)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt im mainloop")
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
