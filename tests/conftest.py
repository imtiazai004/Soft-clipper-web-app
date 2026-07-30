"""Test-wide safety net for the real config.json.

`core.utils.CONFIG_FILE` is the relative path "config.json", so anything that
calls `save_config` writes into whatever the current directory happens to be.
Chdir-ing into a tmp_path inside a test is not enough, because **importing
`backend.main` calls `os.chdir(DATA_ROOT)` at import time** — a test that chdirs
in a fixture and then imports the backend lands back in the repository root and
overwrites the real file.

On the desktop that cost a working Gemini API key twice. In desktop mode this
repo runs the same code path, so it gets the same guard rather than waiting to
learn the lesson locally.

Every test gets `CONFIG_FILE` pointed at an absolute path inside its own tmp dir,
which no chdir can redirect. Autouse, and in conftest so it covers tests nobody
has written yet.
"""
from __future__ import annotations

import os

import pytest

from core import utils

# Resolved once, before any test can move the working directory out from under us.
_REAL_CONFIG = os.path.abspath(utils.CONFIG_FILE)


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
	monkeypatch.setattr(utils, "CONFIG_FILE", str(tmp_path / "config.json"))
	yield


@pytest.fixture(autouse=True)
def isolate_app_data(tmp_path, monkeypatch):
	"""Send per-user state somewhere disposable, on every platform.

	One variable per platform, because the code reads a different one on each:
	APPDATA on Windows, HOME (via expanduser) on macOS, XDG_DATA_HOME or HOME on
	Linux. Missing any of them puts the leak back on that platform only, which is
	exactly how this sort of thing goes unnoticed.
	"""
	root = tmp_path / "appdata"
	root.mkdir(exist_ok=True)
	monkeypatch.setenv("APPDATA", str(root))
	monkeypatch.setenv("HOME", str(root))
	monkeypatch.setenv("USERPROFILE", str(root))
	monkeypatch.setenv("XDG_DATA_HOME", str(root))
	yield


@pytest.fixture(autouse=True)
def _real_config_untouched():
	"""Fail loudly if the real file changes anyway.

	Belt and braces: the fixture above should make this unreachable, but the
	failure it guards against is silent, destructive and only noticed days later.
	A test that trips this has found a code path writing config through something
	other than `utils.CONFIG_FILE`, which is worth knowing about.
	"""
	before = _snapshot()
	yield
	assert _snapshot() == before, (
		f"A test modified the real {_REAL_CONFIG}. Config must never be written "
		"outside tmp_path — see the note at the top of tests/conftest.py."
	)


def _snapshot() -> tuple:
	try:
		with open(_REAL_CONFIG, "rb") as f:
			return (True, f.read())
	except OSError:
		return (False, b"")
