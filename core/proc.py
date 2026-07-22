"""Cancellable subprocess execution.

A job thread registers its job dict as the cancel token; every ffmpeg call in
core/ then goes through run() and dies as soon as that flag is set.

Why not plain subprocess.run(): it blocks until the child exits, so a cancel
would only take effect after the current ffmpeg finished — on a long clip that
is minutes of the user staring at a "cancelling..." spinner.
"""
import subprocess
import threading

_local = threading.local()

POLL = 0.3  # seconds between cancel checks while a child runs


class Cancelled(Exception):
    """Raised inside a job thread once the user cancelled that job."""


def use_token(token: dict | None) -> None:
    """Bind the calling thread to a job dict carrying a 'cancelled' flag."""
    _local.token = token


def cancelled() -> bool:
    token = getattr(_local, "token", None)
    return bool(token and token.get("cancelled"))


def check() -> None:
    """Raise if the job was cancelled — call between long steps."""
    if cancelled():
        raise Cancelled("Cancelled")


def run(cmd, text: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Drop-in subprocess.run(capture_output=True) that honours cancellation."""
    check()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, **kwargs
    )
    while True:
        try:
            out, err = proc.communicate(timeout=POLL)
            break
        except subprocess.TimeoutExpired:
            if cancelled():
                proc.kill()
                proc.communicate()
                raise Cancelled("Cancelled")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
