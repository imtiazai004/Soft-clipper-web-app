"""Where state lives, and what identifies the machine, on each OS.

These run on Windows but exercise all three platforms by faking `sys.platform`.
The licence half of this lives in the desktop repo, which is the only place a
per-machine licence means anything.

Originally:
because the macOS build is produced on a machine none of us will be sitting at.
The two things that must not be wrong there: files going somewhere the system
may clear, and a machine id that changes between runs — which would deactivate
a customer's licence every time they opened the app.

    .venv\\Scripts\\python.exe -m pytest tests/test_platform_paths.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import platform_paths as pp  # noqa: E402


def norm(path: str) -> str:
	"""Compare paths by their parts, not their separators — these tests fake a
	Unix platform while running on Windows, so the two get mixed."""
	return path.replace("\\", "/")

IOREG_OUTPUT = """
+-o MacBookPro18,3  <class IOPlatformExpertDevice, id 0x100000268>
    "IOPlatformSerialNumber" = "C02XYZ123456"
    "IOPlatformUUID" = "9C4A1B2D-3E4F-5061-7283-94A5B6C7D8E9"
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
	monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
	monkeypatch.delenv("XDG_DATA_HOME", raising=False)
	monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path / "home")))
	return tmp_path


# ── where files go ───────────────────────────────────────────────────────────


def test_windows_uses_appdata(home, monkeypatch):
	monkeypatch.setattr(sys, "platform", "win32")
	path = pp.app_data_dir()
	assert path.startswith(str(home / "appdata"))
	assert path.endswith("SoftClipper")


def test_macos_uses_application_support(home, monkeypatch):
	"""Not ~/.softclipper and not the app folder — macOS has one right answer
	and putting files elsewhere risks them being cleaned up."""
	monkeypatch.setattr(sys, "platform", "darwin")
	path = pp.app_data_dir()
	assert norm(path).endswith("Library/Application Support/SoftClipper")


def test_linux_uses_the_xdg_data_directory(home, monkeypatch):
	monkeypatch.setattr(sys, "platform", "linux")
	assert norm(pp.app_data_dir()).endswith(".local/share/SoftClipper")


def test_xdg_data_home_is_respected_when_set(home, monkeypatch):
	monkeypatch.setattr(sys, "platform", "linux")
	monkeypatch.setenv("XDG_DATA_HOME", str(home / "custom"))
	assert pp.app_data_dir().startswith(str(home / "custom"))


def test_the_folder_is_created_and_subfolders_nest(home, monkeypatch):
	monkeypatch.setattr(sys, "platform", "darwin")
	models = pp.app_data_dir("models")
	assert os.path.isdir(models)
	assert norm(models).endswith("SoftClipper/models")


# ── identifying the machine ──────────────────────────────────────────────────


def test_macos_reads_the_platform_uuid(monkeypatch):
	monkeypatch.setattr(sys, "platform", "darwin")
	monkeypatch.setattr(
		subprocess,
		"run",
		lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=IOREG_OUTPUT, stderr=""),
	)
	assert pp.machine_id() == "9C4A1B2D-3E4F-5061-7283-94A5B6C7D8E9"


def test_macos_does_not_use_the_serial_number(monkeypatch):
	"""IOPlatformSerialNumber sits two lines above it in the same output and is
	the wrong value — grabbing it would tie a licence to a repairable part."""
	monkeypatch.setattr(sys, "platform", "darwin")
	monkeypatch.setattr(
		subprocess,
		"run",
		lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=IOREG_OUTPUT, stderr=""),
	)
	assert "C02XYZ123456" not in pp.machine_id()


def test_linux_reads_the_machine_id_file(tmp_path, monkeypatch):
	monkeypatch.setattr(sys, "platform", "linux")
	fake = tmp_path / "machine-id"
	fake.write_text("abc123def456\n", encoding="utf-8")
	monkeypatch.setattr(pp, "_linux_machine_id", lambda: fake.read_text().strip())
	assert pp.machine_id() == "abc123def456"


def test_an_unreadable_identifier_falls_back_instead_of_crashing(monkeypatch):
	"""A customer who has to reactivate once is a far better outcome than one
	locked out of software they paid for by a failed system call."""
	monkeypatch.setattr(sys, "platform", "darwin")

	def boom(*a, **k):
		raise OSError("ioreg missing")

	monkeypatch.setattr(subprocess, "run", boom)
	value = pp.machine_id()
	assert value and value == pp.machine_id()


def test_the_id_is_stable_across_calls():
	assert pp.machine_id() == pp.machine_id()


def test_the_id_is_never_empty():
	assert len(pp.machine_id()) > 4
