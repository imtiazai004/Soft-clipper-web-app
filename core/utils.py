"""Shared helpers: time parsing/formatting, filename sanitizing, config persistence."""
import json
import os
import re
import subprocess

CONFIG_FILE = "config.json"


def check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True)
        return True
    except FileNotFoundError:
        return False


def time_to_seconds(t) -> float | None:
    """Accepts 'HH:MM:SS', 'MM:SS', 'SS', or a number. Returns seconds or None."""
    if isinstance(t, (int, float)):
        return float(t)
    t = str(t).strip()
    parts = t.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(t)
    except (ValueError, IndexError):
        return None


def seconds_to_mmss(s: float) -> str:
    s = max(0, s)
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", str(name)).strip().replace(" ", "_") or "clip"


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
