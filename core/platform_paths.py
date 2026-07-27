"""Where things live, and what identifies this computer, on each OS.

Two decisions used to be made inline with Windows assumptions baked in, and
both matter too much to get wrong on a second platform:

**Where state goes.** The licence and the downloaded speech models must survive
the app folder being deleted and re-extracted. Every OS has a place for that;
they are just different places, and using the wrong one on macOS puts files
somewhere the system may clear.

**What identifies the machine.** A licence is bound to one computer, so this
value has to be stable across hardware changes and reinstalls but different on
a different machine. Each OS publishes exactly one such value, and none of them
is a serial number we should be storing raw — the caller hashes it.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import uuid

APP_FOLDER = "SoftClipper"


def is_windows() -> bool:
	return sys.platform.startswith("win")


def is_mac() -> bool:
	return sys.platform == "darwin"


def app_data_dir(*parts: str) -> str:
	"""The per-user application-support folder, created if missing.

	Windows   %APPDATA%\\SoftClipper
	macOS     ~/Library/Application Support/SoftClipper
	Linux     ~/.local/share/SoftClipper  (XDG_DATA_HOME when set)
	"""
	if is_windows():
		base = os.environ.get("APPDATA") or os.path.expanduser("~")
	elif is_mac():
		base = os.path.expanduser("~/Library/Application Support")
	else:
		base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")

	path = os.path.join(base, APP_FOLDER, *parts)
	os.makedirs(path, exist_ok=True)
	return path


def _windows_machine_id() -> str:
	import winreg  # noqa: PLC0415 - Windows only, imported lazily

	with winreg.OpenKey(
		winreg.HKEY_LOCAL_MACHINE,
		r"SOFTWARE\Microsoft\Cryptography",
		0,
		winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
	) as k:
		return str(winreg.QueryValueEx(k, "MachineGuid")[0])


def _mac_machine_id() -> str:
	"""IOPlatformUUID — macOS's equivalent of MachineGuid.

	It is tied to the logic board, so it survives OS reinstalls and disk
	changes, which is exactly the property a licence needs. `ioreg` is part of
	the system, so there is nothing to bundle.
	"""
	out = subprocess.run(
		["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
		capture_output=True,
		text=True,
		timeout=10,
	).stdout
	for line in out.splitlines():
		if "IOPlatformUUID" in line:
			# "IOPlatformUUID" = "1234ABCD-..."
			return line.split("=")[-1].strip().strip('"')
	raise RuntimeError("IOPlatformUUID not found")


def _linux_machine_id() -> str:
	for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
		try:
			with open(path, encoding="utf-8") as f:
				value = f.read().strip()
			if value:
				return value
		except OSError:
			continue
	raise RuntimeError("no machine-id file")


def machine_id() -> str:
	"""A stable, per-computer identifier — raw, for the caller to hash.

	If the platform's own identifier cannot be read we fall back to hostname
	plus MAC address. That is weaker, and it changes if someone swaps a network
	card, but a customer who can still use the software they paid for and has
	to reactivate once is a far better outcome than one locked out by a failed
	registry read.
	"""
	try:
		if is_windows():
			return _windows_machine_id()
		if is_mac():
			return _mac_machine_id()
		return _linux_machine_id()
	except Exception:
		return f"{platform.node()}:{uuid.getnode()}"
