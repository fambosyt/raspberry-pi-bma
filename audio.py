import os
import re
import shlex
import subprocess
import threading
import logging
from typing import Optional

LOG = logging.getLogger(__name__)
LOG.addHandler(logging.NullHandler())

def detect_usb_alsa_device() -> Optional[str]:
    """
    Versucht, einen angeschlossenen USB-Audio-Card-Index über 'aplay -l' zu finden.
    Gibt z.B. 'hw:1,0' zurück oder None wenn nichts passendes gefunden wurde.
    """
    try:
        proc = subprocess.run(["aplay", "-l"], capture_output=True, text=True, check=False)
        out = proc.stdout + proc.stderr
    except FileNotFoundError:
        LOG.debug("aplay nicht gefunden; kann ALSA-Karten nicht ermitteln.")
        return None

    # Suche nach Zeilen mit "card N:" und Hinweis auf USB
    card_matches = []
    for line in out.splitlines():
        m = re.search(r"card\s+(\d+):\s*(.+)", line)
        if not m:
            continue
        card = int(m.group(1))
        rest = m.group(2)
        if "USB" in line or "usb" in line or "USB" in rest or "Headset" in rest or "Audio" in rest:
            card_matches.append(card)

    if card_matches:
        chosen = card_matches[0]
        dev = f"hw:{chosen},0"
        LOG.debug("USB-Audio-Karte erkannt: %s -> %s", chosen, dev)
        return dev

    LOG.debug("Keine USB-Audio-Karte eindeutig erkannt (aplay -l-Ausgabe durchsucht).")
    return None


class SoundPlayer:
    """
    Einfacher MP3-Player für Raspberry Pi, erkennt optional USB-ALSA-Gerät.
    Verwendet 'mpg123' zum Abspielen von MP3-Dateien (headless geeignet).

    - player_cmd: Befehl zum Abspielen (default 'mpg123')
    - device: optionaler ALSA-Gerätename (z.B. 'hw:1,0'). Wenn None -> automatische Erkennung.
    """

    def __init__(self, player_cmd: str = "mpg123", device: Optional[str] = None):
        self.player_cmd = player_cmd
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._device = device or detect_usb_alsa_device()
        if self._device:
            LOG.info("SoundPlayer konfiguriert: device=%s player=%s", self._device, self.player_cmd)
        else:
            LOG.info("SoundPlayer konfiguriert: kein spezifisches ALSA-Device, Standardausgabe wird genutzt.")

    @property
    def device(self) -> Optional[str]:
        return self._device

    def play(self, filepath: str, interrupt: bool = True):
        """
        Spielt eine MP3 non-blocking. Wenn interrupt=True, wird laufender Sound beendet.
        """
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Sound file not found: {filepath}")

        with self._lock:
            if self._proc and self._proc.poll() is None:
                if interrupt:
                    LOG.debug("Beende aktuell laufenden Player-Prozess, um neuen Start zu erlauben.")
                    try:
                        self._proc.terminate()
                    except Exception:
                        LOG.exception("Fehler beim Terminieren des Players")
                else:
                    LOG.debug("Ignoriere play() weil bereits etwas läuft und interrupt=False.")
                    return

            cmd = [self.player_cmd, "-q"]
            if self._device:
                # mpg123: -a <device> leitet ALSA-Ausgabe auf das Gerät
                cmd += ["-a", self._device]
            cmd.append(filepath)

            LOG.debug("Starte Player: %s", " ".join(shlex.quote(x) for x in cmd))
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError as e:
                raise RuntimeError(f"Player '{self.player_cmd}' nicht gefunden. Bitte mpg123 installieren.") from e

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    LOG.debug("Player-Prozess terminiert.")
                except Exception:
                    LOG.exception("Fehler beim Terminieren des Players")

    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
