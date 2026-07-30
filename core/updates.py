"""Is the bundled yt-dlp behind the released one?

The desktop build checks two things — itself and yt-dlp. A server needs only the
second, and needs it more. yt-dlp is the part that rots: YouTube changes
something, downloads start failing, and on a server that happens to *everyone at
once* with nobody in front of a screen to notice. The app's own version is not
interesting here, because the deploy replaces it.

There is deliberately no upgrade path in this module. On the desktop, pip can
replace yt-dlp in place; here the container is immutable and the right fix is a
redeploy with a newer pin. Offering a button that appears to work and is undone
by the next deploy would be worse than reporting the facts.

Cheap, cached for a day, and quiet on failure: an update check that breaks the
app it is checking is worse than no update check.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request

from . import utils

YTDLP_PYPI = "https://pypi.org/pypi/yt-dlp/json"
YTDLP_LATEST = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

# PyPI and GitHub both rate-limit by IP, and on a shared host that IP is not
# ours alone. Once a day is plenty for a project that releases weekly.
CHECK_INTERVAL_S = 24 * 60 * 60


def _state_path() -> str:
	"""Somewhere writable that surviving a restart is a bonus, not a requirement.

	The container filesystem is ephemeral, so this is a cache and nothing more —
	losing it costs one extra HTTP request.
	"""
	return os.path.join(tempfile.gettempdir(), "softclipper-update-check.json")


def _read_state() -> dict:
	try:
		with open(_state_path(), encoding="utf-8") as f:
			return json.load(f)
	except (OSError, json.JSONDecodeError):
		return {}


def _write_state(state: dict) -> None:
	try:
		with open(_state_path(), "w", encoding="utf-8") as f:
			json.dump(state, f)
	except OSError:
		pass


def _opener():
	proxy = str(utils.load_config().get("proxy", "")).strip()
	if proxy:
		return urllib.request.build_opener(
			urllib.request.ProxyHandler({"http": proxy, "https": proxy})
		)
	return urllib.request.build_opener()


def _get_json(url: str, timeout: int = 12) -> dict:
	req = urllib.request.Request(url, headers={"User-Agent": "SoftClipper"})
	with _opener().open(req, timeout=timeout) as r:
		return json.loads(r.read().decode("utf-8", "replace"))


def parse_version(text: str) -> tuple:
	""""2026.1.10" -> (2026, 1, 10). Compares numerically, so .10 beats .9.

	String comparison is the bug this avoids: "2026.1.10" < "2026.1.9" is true for
	strings, which would report "up to date" forever.
	"""
	nums = re.findall(r"\d+", str(text or ""))
	return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
	return parse_version(candidate) > parse_version(current)


def ytdlp_version() -> str:
	try:
		import yt_dlp  # noqa: PLC0415

		return str(getattr(yt_dlp, "__version__", "")).strip()
	except Exception:
		return ""


def ytdlp_check(force: bool = False) -> dict:
	"""PyPI first — that version is what a redeploy would actually install.

	yt-dlp tags nightlies on GitHub that pip will not give you, so GitHub is only
	the fallback for when PyPI is the thing being blocked.
	"""
	current = ytdlp_version()
	result = {
		"current": current, "latest": "", "update": False,
		# Always false here: the container is immutable. See the module docstring.
		"can_upgrade": False,
		"how": "Redeploy with a newer yt-dlp pin in requirements.txt.",
		"checked": False, "error": "",
	}
	state = _read_state()
	if not force and time.time() - float(state.get("ytdlp_checked_at", 0)) < CHECK_INTERVAL_S:
		cached = state.get("ytdlp_latest", "")
		result["latest"] = cached
		result["update"] = bool(cached) and bool(current) and is_newer(cached, current)
		return result

	latest = ""
	try:
		latest = str(_get_json(YTDLP_PYPI).get("info", {}).get("version") or "")
	except Exception:
		try:
			latest = str(_get_json(YTDLP_LATEST).get("tag_name") or "")
		except Exception as e:
			result["error"] = str(e)
			return result

	result["checked"] = True
	result["latest"] = latest
	result["update"] = bool(latest) and bool(current) and is_newer(latest, current)
	state.update(ytdlp_checked_at=time.time(), ytdlp_latest=latest)
	_write_state(state)
	return result
