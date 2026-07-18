"""Who is making this request.

Soft Clipper began as a single-user desktop tool, so nothing in it had to ask
who the caller was. On a server it does: ten people share one process and none
of them should see another's videos, clips or settings. Every request therefore
resolves to a user id, and all state hangs off that id.

Accounts come from the APP_USERS env var ("alice:pw1,bob:pw2"), which keeps a
ten-person team database-free. With APP_USERS unset the app stays in its
original single-user mode — that is what the packaged .exe ships as, and it
must keep working.
"""
import os
import secrets

from fastapi import HTTPException, Request

# the identity every request gets when no accounts are configured (desktop build)
LOCAL_USER = "local"


def _parse_users(raw: str) -> dict[str, str]:
    """Read "alice:pw1,bob:pw2" into {name: password}, ignoring malformed pairs."""
    users: dict[str, str] = {}
    for pair in raw.split(","):
        name, sep, password = pair.strip().partition(":")
        if not sep:
            continue
        name, password = name.strip(), password.strip()
        if name and password:
            users[name] = password
    return users


USERS = _parse_users(os.environ.get("APP_USERS", ""))

# one flag decides the whole shape of the app: shared server or personal tool
MULTI_USER = bool(USERS)


def session_secret() -> str:
    """Key that signs the login cookie.

    A generated fallback is fine for the desktop build — it only means the one
    local user is signed out when the app restarts. On a server, set
    SESSION_SECRET, or every deploy logs the whole team out.
    """
    return os.environ.get("SESSION_SECRET") or secrets.token_hex(32)


def check_password(username: str, password: str) -> bool:
    expected = USERS.get(username)
    if expected is None:
        # compare anyway, so a wrong username takes the same time as a wrong
        # password and can't be told apart from one
        secrets.compare_digest(password, password)
        return False
    return secrets.compare_digest(password, expected)


def current_user(request: Request) -> str:
    """FastAPI dependency: the signed-in user, or 401.

    Endpoints depend on this rather than reading the cookie themselves, so an
    endpoint that forgets to ask simply has no user to work with.
    """
    if not MULTI_USER:
        return LOCAL_USER
    user = request.session.get("user")
    if not user or user not in USERS:
        raise HTTPException(401, "Not signed in")
    return user
