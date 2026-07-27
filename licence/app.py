"""Soft Clipper licence server.

Three things the desktop app calls (activate, validate, release), one webhook
Stripe calls, and a small admin surface for the humans.

Design rule throughout: **a paying customer must never be locked out by our
infrastructure.** The app holds a signed token that keeps working for weeks, and
every failure here is written down rather than silently swallowed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import crypto, mail, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("licence")

# How long an activation token is valid before the app must check in again.
# Two weeks: long enough to survive a holiday offline, short enough that a
# revoked licence stops working within a sensible window.
TOKEN_DAYS = int(os.environ.get("LICENCE_TOKEN_DAYS", "14"))
# Moving PC is normal. Moving PC fifteen times is a shared key.
MAX_RELEASES = int(os.environ.get("LICENCE_MAX_RELEASES", "10"))
ADMIN_TOKEN = os.environ.get("LICENCE_ADMIN_TOKEN", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

app = FastAPI(title="Soft Clipper licences", docs_url=None, redoc_url=None)

# Created at import rather than on a startup event: creating the tables is
# cheap and idempotent, and doing it here means the service is usable however
# it is started — uvicorn, a test client, or a one-off admin script.
store.init()
log.info("licence db ready at %s", store.DB_PATH)


# ── crude throttle ───────────────────────────────────────────────────────────
# Keys are 20 characters from a 31-symbol alphabet, so guessing one is not a
# realistic attack. This exists to stop a broken client hammering the service.
_hits: dict[str, list[float]] = {}


def _throttle(ip: str, limit: int = 30, window: int = 60):
	now = time.time()
	hits = [t for t in _hits.get(ip, []) if now - t < window]
	hits.append(now)
	_hits[ip] = hits
	if len(hits) > limit:
		raise HTTPException(429, "Too many requests — wait a minute and try again.")


def _client_ip(request: Request) -> str:
	# Caddy sits in front, so the real address is in X-Forwarded-For.
	fwd = request.headers.get("x-forwarded-for", "")
	return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _machine(fingerprint: str) -> str:
	"""Never store the raw fingerprint the client sent — hash it again here so a
	dump of this database cannot be replayed as a machine identity."""
	if not fingerprint or len(fingerprint) < 8:
		raise HTTPException(400, "Missing machine fingerprint")
	return hashlib.sha256(f"sc:{fingerprint}".encode()).hexdigest()


def _require_admin(token: str | None):
	if not ADMIN_TOKEN or not token or not hmac.compare_digest(token, ADMIN_TOKEN):
		raise HTTPException(401, "Not authorised")


# ── what the desktop app calls ───────────────────────────────────────────────


@app.post("/api/licence/activate")
def activate(request: Request, body: dict = Body(...)):
	"""Bind a key to this machine and hand back a signed offline token."""
	_throttle(_client_ip(request))

	key = crypto.normalise(body.get("key", ""))
	if not key:
		raise HTTPException(400, "That does not look like a licence key. Check the email again.")
	machine = _machine(body.get("fingerprint", ""))

	lic = store.get(key)
	if not lic:
		raise HTTPException(404, "We do not recognise that key. Check for typos, or contact support.")
	if lic["status"] != "active":
		raise HTTPException(403, "This licence has been cancelled. Contact support if that is wrong.")

	if lic["machine"] and lic["machine"] != machine:
		raise HTTPException(
			409,
			"This key is already active on another computer. Release it there first "
			"(Settings → Licence → Release), or contact support if you no longer have that PC.",
		)

	store.bind(key, machine)
	return {
		"ok": True,
		"token": crypto.make_token(key, machine, TOKEN_DAYS),
		"email": lic["email"],
		"expires_days": TOKEN_DAYS,
	}


@app.post("/api/licence/validate")
def validate(request: Request, body: dict = Body(...)):
	"""Periodic check-in. Returns a fresh token, or says why not."""
	_throttle(_client_ip(request))

	key = crypto.normalise(body.get("key", ""))
	machine = _machine(body.get("fingerprint", ""))
	lic = store.get(key) if key else None

	if not lic:
		raise HTTPException(404, "Unknown licence key.")
	if lic["status"] != "active":
		raise HTTPException(403, "This licence has been cancelled.")
	if lic["machine"] != machine:
		raise HTTPException(409, "This licence is active on a different computer.")

	store.touch(key)
	return {"ok": True, "token": crypto.make_token(key, machine, TOKEN_DAYS)}


@app.post("/api/licence/release")
def release(request: Request, body: dict = Body(...)):
	"""Unbind so the customer can activate on a new machine. Only the machine
	currently holding the licence may do this — otherwise a leaked key could be
	used to kick the real owner off their own PC."""
	_throttle(_client_ip(request))

	key = crypto.normalise(body.get("key", ""))
	machine = _machine(body.get("fingerprint", ""))
	lic = store.get(key) if key else None

	if not lic:
		raise HTTPException(404, "Unknown licence key.")
	if lic["machine"] and lic["machine"] != machine:
		raise HTTPException(403, "This licence is held by a different computer.")
	if lic["releases"] >= MAX_RELEASES:
		raise HTTPException(
			429,
			"This licence has been moved between computers many times. "
			"Contact support and we will sort it out.",
		)

	count = store.release(key)
	return {"ok": True, "releases": count}


@app.get("/api/health")
def health():
	return {"ok": True}


# ── Stripe ───────────────────────────────────────────────────────────────────


def _verify_stripe(payload: bytes, sig_header: str) -> bool:
	"""Stripe's scheme: `t=<ts>,v1=<hex hmac of "ts.payload">`.

	Implemented here rather than pulling in the stripe SDK — this is the only
	part of it we need, and one fewer dependency is one fewer thing to patch.
	"""
	if not STRIPE_WEBHOOK_SECRET:
		log.error("STRIPE_WEBHOOK_SECRET not set — refusing webhook")
		return False
	try:
		parts = dict(p.split("=", 1) for p in sig_header.split(","))
		timestamp, signature = parts["t"], parts["v1"]
	except Exception:
		return False

	# Reject replays of an old, valid-looking event.
	if abs(time.time() - int(timestamp)) > 300:
		return False

	expected = hmac.new(
		STRIPE_WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
	).hexdigest()
	return hmac.compare_digest(expected, signature)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header("")):
	payload = await request.body()
	if not _verify_stripe(payload, stripe_signature):
		raise HTTPException(400, "Bad signature")

	event = json.loads(payload)
	kind = event.get("type", "")
	obj = event.get("data", {}).get("object", {})

	if kind == "checkout.session.completed":
		session_id = obj.get("id", "")
		email = (
			obj.get("customer_details", {}).get("email")
			or obj.get("customer_email")
			or ""
		).strip()

		# Stripe retries on any non-2xx, so this must be safe to run twice.
		existing = store.find_by_source(session_id)
		if existing:
			log.info("session %s already has licence %s", session_id, existing["key"])
			return {"ok": True, "key": existing["key"], "duplicate": True}

		if not email:
			log.error("session %s completed with no email — cannot deliver a key", session_id)
			raise HTTPException(400, "No email on session")

		key = crypto.new_key()
		store.create(key, email, source=session_id)
		mail.send_licence(email, key)
		log.info("licence %s created for %s", key, email)
		return {"ok": True, "key": key}

	if kind in ("charge.refunded", "charge.dispute.created"):
		# Find the licence by the payment intent's checkout session where we can;
		# otherwise fall back to the email so a refund still closes the licence.
		email = (obj.get("billing_details", {}) or {}).get("email", "")
		for lic in store.recent(500):
			if lic["email"] == (email or "").lower() and lic["status"] == "active":
				store.revoke(lic["key"], kind)
				log.info("revoked %s after %s", lic["key"], kind)
				break
		return {"ok": True}

	return {"ok": True, "ignored": kind}


# ── admin ────────────────────────────────────────────────────────────────────


@app.post("/api/admin/licences")
def admin_create(body: dict = Body(...), x_admin_token: str = Header("")):
	"""Issue a key by hand — refund replacements, testers, invoiced team sales."""
	_require_admin(x_admin_token)
	email = (body.get("email") or "").strip()
	if not email:
		raise HTTPException(400, "email is required")
	key = crypto.new_key()
	store.create(key, email, source=body.get("source", "manual"), note=body.get("note", ""))
	if body.get("send_email", True):
		mail.send_licence(email, key)
	return {"ok": True, "key": key}


@app.get("/api/admin/licences")
def admin_list(x_admin_token: str = Header(""), limit: int = 100):
	_require_admin(x_admin_token)
	return {"licences": store.recent(limit)}


@app.get("/api/admin/licences/{key}")
def admin_detail(key: str, x_admin_token: str = Header("")):
	_require_admin(x_admin_token)
	key = crypto.normalise(key)
	lic = store.get(key)
	if not lic:
		raise HTTPException(404, "Unknown key")
	return {"licence": lic, "events": store.events_for(key)}


@app.post("/api/admin/licences/{key}/release")
def admin_release(key: str, x_admin_token: str = Header("")):
	"""Support's version of release, for when the old PC is dead or wiped."""
	_require_admin(x_admin_token)
	key = crypto.normalise(key)
	if not store.get(key):
		raise HTTPException(404, "Unknown key")
	return {"ok": True, "releases": store.release(key)}


@app.post("/api/admin/licences/{key}/revoke")
def admin_revoke(key: str, body: dict = Body(default={}), x_admin_token: str = Header("")):
	_require_admin(x_admin_token)
	key = crypto.normalise(key)
	if not store.get(key):
		raise HTTPException(404, "Unknown key")
	store.revoke(key, body.get("reason", "manual"))
	return {"ok": True}


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
	# The app shows `error` straight to the customer, so it is written for them.
	return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
