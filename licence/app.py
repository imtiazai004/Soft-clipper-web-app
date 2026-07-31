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
import re
import secrets
import time
import urllib.parse

import pathlib

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import bank_payments, crypto, mail, settings, store, stripe_api

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

# Where "publish the site" points. A Cloudflare deploy hook, a GitHub
# repository_dispatch, anything that rebuilds the marketing site when POSTed to.
# Unset means the admin page says publishing is not wired up rather than
# offering a button that silently does nothing.
PUBLISH_HOOK_URL = os.environ.get("PUBLISH_HOOK_URL", "")

# ── the affiliate self-sign-up surface ───────────────────────────────────────
#
# The marketing site is static and lives on another origin, so its sign-up form
# posts here across origins. That is the only cross-origin call this service
# accepts, and the list below is exactly who may make it — a wildcard would let
# any page on the internet drive these endpoints on a visitor's behalf.
SITE_ORIGINS = [
	o.strip() for o in os.environ.get(
		"SITE_ORIGIN", "https://softclipper.pro,https://www.softclipper.pro"
	).split(",") if o.strip()
]
# Where the affiliate's own dashboard lives, for the links inside emails. Served
# by this service, on this host — an affiliate signing in has to reach a server,
# and the static site is not one.
#
# `/partner`, not `/affiliate`, and every endpoint below has the same pair of
# names. The reason is not taste: ad and tracker blockers match request URLs
# against keyword lists, and "affiliate" is on them — the sign-up form's fetch
# was cancelled by the browser before it left, which reaches JavaScript as a
# bare "Failed to fetch" with nothing in it to diagnose. Nothing is wrong with
# the server in that state, which is what makes it expensive to find. The old
# paths still answer, so a link already sent to somebody keeps working.
#
# On `api.` rather than `app.`, and that is the more important half. `app.` is
# this server's bare IP, and a bare IP is what gets blocked wholesale in a
# number of countries — the marketing site loads fine, because it is behind
# Cloudflare, and then every request the page makes to `app.` dies. `api.` is
# the same server reached through Cloudflare, which nobody blocks.
PORTAL_URL = os.environ.get("AFFILIATE_PORTAL_URL", "https://api.softclipper.pro/partner")
ADMIN_URL = os.environ.get("ADMIN_URL", "https://api.softclipper.pro/admin")
# Where an affiliate's link points. The shop, not this API.
SHOP_URL = os.environ.get("SHOP_URL", SITE_ORIGINS[0] if SITE_ORIGINS else "https://softclipper.pro")

# A sign-in link is good for half an hour; the session it grants lasts a month.
# Short for the thing that arrives by email and may sit in an inbox, long for the
# thing that lives in the browser of the person who already proved who they are.
LOGIN_LINK_MINUTES = int(os.environ.get("AFFILIATE_LINK_MINUTES", "30"))
VERIFY_LINK_MINUTES = int(os.environ.get("AFFILIATE_VERIFY_MINUTES", str(24 * 60)))
SESSION_DAYS = int(os.environ.get("AFFILIATE_SESSION_DAYS", "30"))
SESSION_COOKIE = "sc_aff"
# Off only for local testing over plain HTTP. In production the cookie is the
# affiliate's whole identity and must never travel in the clear.
COOKIE_SECURE = os.environ.get("AFFILIATE_COOKIE_SECURE", "1") != "0"

app = FastAPI(title="Soft Clipper licences", docs_url=None, redoc_url=None)

# Credentials are off, and that is not an oversight: the portal session cookie is
# only ever used same-origin, from the page this service serves itself. Nothing
# the site posts here needs to carry it, so nothing here has to trust a cookie
# that arrived from another origin.
app.add_middleware(
	CORSMiddleware,
	allow_origins=SITE_ORIGINS,
	allow_credentials=False,
	allow_methods=["GET", "POST"],
	allow_headers=["Content-Type"],
	max_age=3600,
)

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

	# Nothing used to remove an address from this dict, which was survivable while
	# every caller was a licensed copy of the app. The affiliate sign-up and click
	# endpoints are reachable by anyone, so an idle entry per address that has
	# ever touched the service is now a leak with the whole internet feeding it.
	if len(_hits) > 5000:
		for key in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
			del _hits[key]

	if len(hits) > limit:
		raise HTTPException(429, "Too many requests — wait a minute and try again.")


def _client_ip(request: Request) -> str:
	# Caddy sits in front, so the real address is in X-Forwarded-For.
	fwd = request.headers.get("x-forwarded-for", "")
	return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _clean_fingerprint(fingerprint: str) -> str:
	"""The client's fingerprint, checked but otherwise untouched.

	This is what goes into the signed token, because the token is read by the
	client, and the client can only compare it against the one thing it knows:
	its own fingerprint. Putting anything else in there — a hash, a truncation,
	an id of our own — means the comparison can never succeed and every
	activation silently fails. It did, for exactly that reason.
	"""
	if not fingerprint or len(fingerprint) < 8:
		raise HTTPException(400, "Missing machine fingerprint")
	return fingerprint


def _stored_machine(fingerprint: str) -> str:
	"""What the database holds — hashed, so a dump of it cannot be replayed as a
	machine identity. Never put this in a token."""
	return hashlib.sha256(f"sc:{_clean_fingerprint(fingerprint)}".encode()).hexdigest()


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
	fingerprint = _clean_fingerprint(body.get("fingerprint", ""))
	machine = _stored_machine(fingerprint)

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
		# The raw fingerprint, not the stored hash: see _clean_fingerprint.
		"token": crypto.make_token(key, fingerprint, TOKEN_DAYS),
		"email": lic["email"],
		"expires_days": TOKEN_DAYS,
	}


@app.post("/api/licence/validate")
def validate(request: Request, body: dict = Body(...)):
	"""Periodic check-in. Returns a fresh token, or says why not."""
	_throttle(_client_ip(request))

	key = crypto.normalise(body.get("key", ""))
	fingerprint = _clean_fingerprint(body.get("fingerprint", ""))
	machine = _stored_machine(fingerprint)
	lic = store.get(key) if key else None

	if not lic:
		raise HTTPException(404, "Unknown licence key.")
	if lic["status"] != "active":
		raise HTTPException(403, "This licence has been cancelled.")
	if lic["machine"] != machine:
		raise HTTPException(409, "This licence is active on a different computer.")

	store.touch(key)
	return {"ok": True, "token": crypto.make_token(key, fingerprint, TOKEN_DAYS)}


@app.post("/api/licence/release")
def release(request: Request, body: dict = Body(...)):
	"""Unbind so the customer can activate on a new machine. Only the machine
	currently holding the licence may do this — otherwise a leaked key could be
	used to kick the real owner off their own PC."""
	_throttle(_client_ip(request))

	key = crypto.normalise(body.get("key", ""))
	machine = _stored_machine(body.get("fingerprint", ""))
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


def _credit_affiliate(ref: str, key: str, session_id: str, session: dict, buyer_email: str = ""):
	"""Record what an affiliate earned on one sale.

	The commission is worked out from `amount_total` — what Stripe actually
	collected — rather than from the list price. A discount code, a currency
	conversion or a price change would otherwise pay a percentage of a number
	nobody was charged.
	"""
	config = settings.get()["affiliates"]
	if not config["enabled"]:
		# The programme is closed. Existing commission is untouched and still
		# owed; this only stops new sales earning.
		log.info("affiliate programme is off — not crediting %s", ref)
		return

	affiliate = store.get_affiliate(ref)
	if not affiliate:
		# Someone shared a link with a code that does not exist, or one that was
		# deleted. The sale is real and stands; there is simply nobody to pay.
		log.warning("session %s carried unknown ref %r", session_id, ref)
		return
	if affiliate["status"] != "active":
		# Covers a disabled code and every stage of an unfinished sign-up —
		# pending, under review, rejected. All of them are "there is a row, but it
		# is not earning", and the sale itself stands regardless.
		log.warning("session %s carried ref %r with status %s", session_id, ref, affiliate["status"])
		return

	# The one rule the affiliate terms name as grounds for closing an account, now
	# enforced rather than only published. Buying through your own link is a 30%
	# discount that costs us a commission and shows up as a sale, and the honest
	# version of it — "can I have it cheaper" — is a question we would say yes to.
	if buyer_email and buyer_email.lower().strip() == (affiliate["email"] or "").lower().strip():
		log.warning("session %s is a self-referral by %s — not crediting", session_id, ref)
		return

	gross = int(session.get("amount_total") or 0)
	if gross <= 0:
		log.warning("session %s has no amount_total — not crediting %s", session_id, ref)
		return

	row = store.record_referral(
		code=ref,
		licence_key=key,
		session=session_id,
		gross=gross,
		currency=session.get("currency") or "usd",
		rate_pct=int(affiliate["rate_pct"]),
		hold_days=int(config["holdDays"]),
	)
	if row is None:
		log.info("session %s already credited to %s", session_id, ref)
	else:
		log.info("credited %s: %s of %s to %s", session_id, row["commission"], gross, ref)


def _fulfil_stripe_checkout(obj: dict) -> dict:
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

	# The affiliate tag, if the buyer arrived through a referral link. Stripe
	# carries it from the Payment Link URL to here untouched, which is why the
	# site does not need a server of its own to attribute a sale.
	ref = store.normalise_code(obj.get("client_reference_id") or "")

	key = crypto.new_key()
	store.create(key, email, source=session_id, ref=ref)
	mail.send_licence(email, key)
	log.info("licence %s created for %s", key, email)

	# Attribution is deliberately after the key exists and the email has gone.
	# A customer who paid must get their licence even if the commission
	# bookkeeping fails, so nothing in here is allowed to fail the webhook —
	# an unpaid affiliate is a conversation, an undelivered licence is a refund.
	if ref:
		try:
			_credit_affiliate(ref, key, session_id, obj, buyer_email=email)
		except Exception:
			log.exception("could not credit affiliate %s for session %s", ref, session_id)

	return {"ok": True, "key": key}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header("")):
	payload = await request.body()
	if not _verify_stripe(payload, stripe_signature):
		raise HTTPException(400, "Bad signature")

	event = json.loads(payload)
	kind = event.get("type", "")
	obj = event.get("data", {}).get("object", {})

	if kind == "checkout.session.completed":
		# Card sessions are paid at completion. Delayed methods also emit this
		# event, but at that point they only mean "instructions were shown". A key
		# here would give the product away before a transfer arrived.
		if obj.get("payment_status") != "paid":
			log.info("session %s completed but is not paid yet", obj.get("id", ""))
			return {"ok": True, "pending": True}
		return _fulfil_stripe_checkout(obj)

	if kind == "checkout.session.async_payment_succeeded":
		return _fulfil_stripe_checkout(obj)

	if kind == "checkout.session.async_payment_failed":
		log.warning("delayed Stripe payment failed for session %s", obj.get("id", ""))
		return {"ok": True, "failed": True}

	if kind in ("charge.refunded", "charge.dispute.created"):
		# Find the licence by the payment intent's checkout session where we can;
		# otherwise fall back to the email so a refund still closes the licence.
		email = (obj.get("billing_details", {}) or {}).get("email", "")
		for lic in store.recent(500):
			if lic["email"] == (email or "").lower() and lic["status"] == "active":
				store.revoke(lic["key"], kind)
				log.info("revoked %s after %s", lic["key"], kind)
				# A sale that came back earns no commission. Only unpaid commission
				# can be taken back this way; anything already sent stays sent and
				# shows in the admin list as paid against a revoked licence.
				if store.void_referral(lic["key"], kind):
					log.info("voided commission on %s after %s", lic["key"], kind)
				break
		return {"ok": True}

	if kind == "account.updated":
		# A Connect event: the affiliate has moved through Stripe's onboarding.
		# This is how "ready to be paid" becomes true without anyone watching —
		# they finish at midnight on a Sunday and the payout button works on
		# Monday without an admin having to think to press Refresh.
		affiliate = store.affiliate_by_stripe_account(obj.get("id", ""))
		if affiliate:
			ready = stripe_api.payouts_enabled(obj)
			if bool(affiliate["stripe_ready"]) != ready:
				store.set_affiliate_stripe(affiliate["code"], obj["id"], ready)
				log.info("affiliate %s payouts_enabled=%s", affiliate["code"], ready)
		return {"ok": True}

	return {"ok": True, "ignored": kind}


# ── Pakistani bank checkout ─────────────────────────────────────────────────


def _public_bank_order(order: dict) -> dict:
	"""The customer/admin-safe fields. Token hashes and server paths never leave."""
	return {
		"reference": order["reference"],
		"email": order["email"],
		"status": order["status"],
		"method": order["method"],
		"usd_cents": order["usd_cents"],
		"fx_rate": order["fx_rate"],
		"rate_date": order["rate_date"],
		"rate_stale": bool(order["rate_stale"]),
		"pkr_amount": order["pkr_amount"],
		"transaction_id": order.get("transaction_id") or "",
		"affiliate_code": order.get("affiliate_code") or "",
		"has_proof": bool(order.get("proof_path")),
		"created_at": order["created_at"],
		"expires_at": order["expires_at"],
		"submitted_at": order.get("submitted_at"),
		"decided_at": order.get("decided_at"),
		"decided_note": order.get("decided_note") or "",
		"licence_key": order.get("licence_key") or "",
	}


@app.post("/api/checkout/bank/orders")
def create_bank_order(request: Request, body: dict = Body(...)):
	"""Create and lock a USD-to-PKR quote before showing payment details."""
	_throttle(f"bank-create:{_client_ip(request)}", limit=8, window=600)
	if not bank_payments.BANK_ENABLED:
		raise HTTPException(503, "Pakistani bank payments are temporarily unavailable.")

	email = (body.get("email") or "").strip().lower()
	if not _looks_like_email(email):
		raise HTTPException(400, "Enter the email address where your licence should be sent.")
	method = (body.get("method") or "bank").strip().lower()
	if method not in ("bank", "jazzcash"):
		raise HTTPException(400, "Choose Bank Al-Habib or JazzCash.")

	price = settings.get()["price"]
	if str(price.get("currency", "USD")).upper() != "USD":
		raise HTTPException(503, "Pakistani checkout currently requires the product price in USD.")
	try:
		quoted = bank_payments.quote(int(price["amount"]) * 100)
	except bank_payments.BankPaymentError as exc:
		log.warning("bank quote unavailable: %s", exc)
		raise HTTPException(503, str(exc)) from exc

	reference = "SC-" + secrets.token_hex(4).upper()
	token = secrets.token_urlsafe(32)
	order = store.create_bank_order(
		reference=reference,
		token=token,
		email=email,
		method=method,
		usd_cents=quoted["usd_cents"],
		fx_rate=quoted["rate"],
		rate_date=quoted["rate_date"],
		rate_stale=quoted["rate_stale"],
		pkr_amount=quoted["pkr_amount"],
		affiliate_code=body.get("ref", ""),
		expires_at=int(time.time()) + bank_payments.ORDER_QUOTE_SECONDS,
	)
	return {
		"ok": True,
		"order": _public_bank_order(order),
		"token": token,
		"payment_details": bank_payments.public_details(),
	}


@app.post("/api/checkout/bank/orders/{reference}/submit")
async def submit_bank_order(reference: str, request: Request):
	"""Attach the transfer reference and optional receipt to a pending order."""
	_throttle(f"bank-submit:{_client_ip(request)}", limit=12, window=600)
	reference = (reference or "").upper()
	if not re.fullmatch(r"SC-[A-F0-9]{8}", reference):
		raise HTTPException(404, "That bank-payment order was not found.")

	length = int(request.headers.get("content-length") or 0)
	if length > 6 * 1024 * 1024:
		raise HTTPException(413, "The payment screenshot must be smaller than 4 MB.")
	raw = await request.body()
	if len(raw) > 6 * 1024 * 1024:
		raise HTTPException(413, "The payment screenshot must be smaller than 4 MB.")
	try:
		body = json.loads(raw or b"{}")
	except (json.JSONDecodeError, UnicodeDecodeError) as exc:
		raise HTTPException(400, "The payment submission could not be read.") from exc

	token = str(body.get("token") or "")
	transaction_id = str(body.get("transaction_id") or "").strip()
	if not 4 <= len(transaction_id) <= 100 or any(ord(c) < 32 for c in transaction_id):
		raise HTTPException(400, "Enter the transaction ID shown by your bank or JazzCash.")
	current = store.bank_order(reference, token)
	if not current:
		raise HTTPException(404, "That bank-payment order was not found.")
	if current["status"] == "submitted" and (current["transaction_id"] or "") == transaction_id:
		return {"ok": True, "order": _public_bank_order(current), "duplicate": True}
	if current["status"] != "awaiting_payment":
		raise HTTPException(409, f"This order is already {current['status'].replace('_', ' ')}.")

	proof_path = proof_sha = ""
	try:
		proof_path, proof_sha = bank_payments.save_proof(
			reference, str(body.get("proof_data") or "")
		)
		order = store.submit_bank_order(
			reference, token, transaction_id, proof_path=proof_path, proof_sha256=proof_sha
		)
	except (bank_payments.BankPaymentError, store.DuplicatePayment, store.BankOrderState) as exc:
		bank_payments.remove_proof(proof_path)
		raise HTTPException(409 if isinstance(exc, store.DuplicatePayment) else 400, str(exc)) from exc

	mail.send_bank_payment_submitted(order["email"], reference, int(order["pkr_amount"]))
	mail.notify_bank_payment(
		reference,
		order["email"],
		int(order["pkr_amount"]),
		order["method"],
		transaction_id,
	)
	return {"ok": True, "order": _public_bank_order(order)}


@app.get("/api/admin/bank-orders")
def admin_bank_orders(
	status: str = "", limit: int = 200, x_admin_token: str = Header("")
):
	_require_admin(x_admin_token)
	if status and status not in ("awaiting_payment", "submitted", "paid", "rejected"):
		raise HTTPException(400, "Unknown bank-order status.")
	return {"orders": [_public_bank_order(o) for o in store.bank_orders(status, limit)]}


@app.get("/api/admin/bank-orders/{reference}/proof")
def admin_bank_order_proof(reference: str, x_admin_token: str = Header("")):
	_require_admin(x_admin_token)
	order = store.bank_order(reference)
	if not order or not order.get("proof_path"):
		raise HTTPException(404, "This order has no payment screenshot.")
	path = pathlib.Path(order["proof_path"]).resolve()
	root = bank_payments.PROOF_DIR.resolve()
	if root not in path.parents or not path.is_file():
		raise HTTPException(404, "The payment screenshot is missing from private storage.")
	media = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
		path.suffix.lower(), "application/octet-stream"
	)
	return FileResponse(path, media_type=media, filename=f"{order['reference']}-payment{path.suffix}")


@app.post("/api/admin/bank-orders/{reference}/approve")
def admin_approve_bank_order(
	reference: str, body: dict = Body(default={}), x_admin_token: str = Header("")
):
	_require_admin(x_admin_token)
	key = crypto.new_key()
	try:
		order, duplicate = store.fulfil_bank_order(
			reference, key, (body.get("note") or "").strip()[:500]
		)
	except store.BankOrderState as exc:
		raise HTTPException(400, str(exc)) from exc
	if duplicate:
		return {"ok": True, "order": _public_bank_order(order), "duplicate": True}

	mail.send_licence(order["email"], order["licence_key"])
	ref = order.get("affiliate_code") or ""
	if ref:
		try:
			_credit_affiliate(
				ref,
				order["licence_key"],
				f"bank:{order['reference']}",
				# The affiliate programme is denominated in USD. This is the exact
				# configured USD price from which the customer's locked PKR quote was
				# calculated, so adding a Pakistani sale does not mix currencies in
				# affiliate balances or make a Stripe payout impossible.
				{"amount_total": int(order["usd_cents"]), "currency": "usd"},
				buyer_email=order["email"],
			)
		except Exception:
			log.exception("could not credit affiliate %s for bank order %s", ref, order["reference"])
	return {"ok": True, "order": _public_bank_order(order)}


@app.post("/api/admin/bank-orders/{reference}/reject")
def admin_reject_bank_order(
	reference: str, body: dict = Body(default={}), x_admin_token: str = Header("")
):
	_require_admin(x_admin_token)
	reason = (body.get("reason") or "").strip()[:500]
	try:
		order = store.reject_bank_order(reference, reason)
	except store.BankOrderState as exc:
		raise HTTPException(400, str(exc)) from exc
	mail.send_bank_payment_rejected(order["email"], order["reference"], reason)
	return {"ok": True, "order": _public_bank_order(order)}


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


# ── admin: affiliates ────────────────────────────────────────────────────────


@app.get("/api/admin/affiliates")
def admin_affiliates(x_admin_token: str = Header("")):
	"""Everyone who sells for us, with what they are owed right now."""
	_require_admin(x_admin_token)
	config = settings.get()["affiliates"]
	return {
		"affiliates": store.affiliate_summary(),
		"rate_pct": config["ratePct"],
		"hold_days": config["holdDays"],
		"programme_open": config["enabled"],
		# The page hides the automatic-payout controls when this is false rather
		# than offering a button that can only fail.
		"stripe_payouts": stripe_api.configured(),
		# So the page can say why nobody is signing up, when the reason is that
		# the form is switched off.
		"self_signup": bool(config["enabled"]) and bool(config.get("selfSignup", True)),
		"auto_approve": bool(config.get("autoApprove", True)),
	}


@app.post("/api/admin/affiliates")
def admin_add_affiliate(body: dict = Body(...), x_admin_token: str = Header("")):
	_require_admin(x_admin_token)
	code = store.normalise_code(body.get("code", ""))
	if len(code) < 3:
		raise HTTPException(400, "Code must be at least 3 letters or digits")
	if store.get_affiliate(code):
		raise HTTPException(409, f"'{code}' is already taken")
	if not (body.get("name") or "").strip():
		raise HTTPException(400, "name is required")
	if not (body.get("email") or "").strip():
		raise HTTPException(400, "email is required — it is where the money and the questions go")

	rate = int(body.get("rate_pct") or settings.get()["affiliates"]["ratePct"])
	# A rate over 100 would pay out more than the sale brought in; 0 is a partner
	# who gets a tracking link but no commission, which is a real arrangement.
	if not 0 <= rate <= 100:
		raise HTTPException(400, "rate_pct must be between 0 and 100")

	method = body.get("payout_method", "manual")
	if method not in ("manual", "stripe"):
		raise HTTPException(400, "payout_method must be 'manual' or 'stripe'")

	return {
		"ok": True,
		"affiliate": store.add_affiliate(
			code=code,
			name=body["name"],
			email=body["email"],
			rate_pct=rate,
			payout_method=method,
			payout_to=body.get("payout_to", ""),
			note=body.get("note", ""),
		),
	}


@app.post("/api/admin/affiliates/{code}/stripe")
def admin_affiliate_stripe(code: str, body: dict = Body(default={}), x_admin_token: str = Header("")):
	"""Start — or resume — Stripe onboarding for one affiliate.

	Returns a link to send them. They follow it, Stripe collects their identity
	and bank details and does the compliance, and `account.updated` tells us when
	they can actually be paid. Calling this again for an affiliate who already
	has an account issues a fresh link rather than a second account: two accounts
	for one person is a mess only Stripe support can untangle.
	"""
	_require_admin(x_admin_token)
	if not stripe_api.configured():
		raise HTTPException(
			503,
			"Stripe payouts are not switched on. Set STRIPE_SECRET_KEY on the server, "
			"or pay this affiliate by hand.",
		)

	affiliate = store.get_affiliate(code)
	if not affiliate:
		raise HTTPException(404, "Unknown affiliate")

	try:
		# The same helper the affiliate's own dashboard calls, so neither path can
		# create a second Connect account for somebody who already has one.
		return _stripe_onboarding(affiliate, (body.get("country") or "").strip())
	except stripe_api.StripeError as exc:
		# Stripe's own message is the useful one — "country not supported" is
		# exactly what an admin needs to read, and rewording it would hide it.
		raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/affiliates/{code}/refresh")
def admin_affiliate_refresh(code: str, x_admin_token: str = Header("")):
	"""Ask Stripe whether this affiliate can be paid yet."""
	_require_admin(x_admin_token)
	affiliate = store.get_affiliate(code)
	if not affiliate or not affiliate["stripe_account"]:
		raise HTTPException(404, "That affiliate has no Stripe account")
	try:
		acct = stripe_api.account(affiliate["stripe_account"])
	except stripe_api.StripeError as exc:
		raise HTTPException(400, str(exc)) from exc
	ready = stripe_api.payouts_enabled(acct)
	store.set_affiliate_stripe(code, affiliate["stripe_account"], ready)
	return {
		"ok": True,
		"ready": ready,
		# What Stripe is still waiting for, so an admin can tell the affiliate
		# what to go and finish instead of guessing.
		"needs": (acct.get("requirements") or {}).get("currently_due", []),
	}


@app.post("/api/admin/affiliates/{code}/pay")
def admin_pay_affiliate(code: str, x_admin_token: str = Header("")):
	"""Send everything owed to a Stripe-connected affiliate, in one transfer.

	Only for `payout_method = 'stripe'`. Manual affiliates go through
	`/referrals/paid`, which records a payment made elsewhere rather than making
	one — the money for those leaves Wise or PayPal, not this endpoint.
	"""
	_require_admin(x_admin_token)
	affiliate = store.get_affiliate(code)
	if not affiliate:
		raise HTTPException(404, "Unknown affiliate")
	if affiliate["payout_method"] != "stripe" or not affiliate["stripe_account"]:
		raise HTTPException(400, "This affiliate is paid by hand — send the money, then mark it paid.")
	if not affiliate["stripe_ready"]:
		raise HTTPException(400, "Stripe has not finished verifying this affiliate yet.")

	rows = store.payable(code)
	if not rows:
		raise HTTPException(400, "Nothing is due — commission is held until the refund window closes.")

	currencies = {r["currency"] for r in rows}
	if len(currencies) > 1:
		raise HTTPException(400, f"Mixed currencies ({', '.join(sorted(currencies))}) — pay these by hand.")

	amount = sum(r["commission"] for r in rows)
	ids = [r["id"] for r in rows]
	# The key is derived from exactly what is being paid. A retry after a timeout
	# sends the identical request and Stripe returns the original transfer
	# instead of making a second one; a later payout covers different rows, so it
	# gets a different key and goes through.
	idem = f"sc-payout-{code}-{min(ids)}-{max(ids)}-{amount}"

	try:
		transfer = stripe_api.create_transfer(
			affiliate["stripe_account"],
			amount,
			currencies.pop(),
			idempotency_key=idem,
			description=f"Soft Clipper commission — {len(ids)} sale(s)",
		)
	except stripe_api.StripeError as exc:
		# Nothing is marked paid. The rows stay payable and the admin can retry,
		# which is the right way round: a row wrongly left open is a second
		# payment attempt, a row wrongly closed is an affiliate who never gets
		# paid and has no way of knowing.
		raise HTTPException(400, str(exc)) from exc

	marked = store.mark_referrals_paid(ids, how="stripe", transfer_id=transfer.get("id", ""))
	log.info("paid %s %s to %s via %s", amount, transfer.get("currency"), code, transfer.get("id"))
	return {"ok": True, "amount": amount, "rows": marked, "transfer": transfer.get("id")}


@app.post("/api/admin/affiliates/{code}/status")
def admin_affiliate_status(code: str, body: dict = Body(default={}), x_admin_token: str = Header("")):
	"""Disable stops future sales earning. It never touches commission already
	recorded — someone can behave badly today without losing what they earned
	honestly last month."""
	_require_admin(x_admin_token)
	status = body.get("status", "disabled")
	if status not in ("active", "disabled"):
		raise HTTPException(400, "status must be 'active' or 'disabled'")
	if not store.get_affiliate(code):
		raise HTTPException(404, "Unknown affiliate")
	store.set_affiliate_status(code, status, body.get("reason", ""))
	return {"ok": True}


@app.post("/api/admin/affiliates/{code}/decide")
def admin_decide_affiliate(code: str, body: dict = Body(...), x_admin_token: str = Header("")):
	"""Approve or reject an application that came in through the public form.

	Only reachable for someone actually waiting — `review`. Approving an active
	affiliate is a no-op that would send them a second welcome email, and
	rejecting one whose sales are already recorded is a different decision
	entirely, which is what Disable is for.

	Either way they are told. An applicant who hears nothing assumes the form is
	broken and applies again from another address, and one rejection quietly
	becomes three accounts.
	"""
	_require_admin(x_admin_token)
	affiliate = store.get_affiliate(code)
	if not affiliate:
		raise HTTPException(404, "Unknown affiliate")
	if affiliate["status"] != "review":
		raise HTTPException(
			400,
			f"'{code}' is {affiliate['status']}, not waiting for a decision. "
			"Use Enable or Disable instead.",
		)

	approve = bool(body.get("approve"))
	reason = (body.get("reason") or "").strip()
	store.set_affiliate_status(code, "active" if approve else "rejected", reason)

	if approve:
		mail.send_affiliate_welcome(
			affiliate["email"], affiliate["name"], code,
			_ref_link(code), PORTAL_URL, int(affiliate["rate_pct"]),
		)
	else:
		mail.send_affiliate_decision(
			affiliate["email"], affiliate["name"], False, reason, _ref_link(code), PORTAL_URL
		)
	log.info("affiliate %s %s by admin", code, "approved" if approve else "rejected")
	return {"ok": True, "status": "active" if approve else "rejected"}


@app.get("/api/admin/referrals")
def admin_referrals(code: str = "", x_admin_token: str = Header(""), limit: int = 200):
	_require_admin(x_admin_token)
	return {"referrals": store.referrals(code, limit)}


@app.post("/api/admin/referrals/paid")
def admin_mark_paid(body: dict = Body(...), x_admin_token: str = Header("")):
	"""Mark commission as sent, after you have actually sent it.

	This is a record of a payment made elsewhere, not an instruction to pay. The
	money moves in Wise or PayPal by hand and this closes the row afterwards —
	deliberately, because an automated transfer that goes to the wrong person or
	goes twice cannot be undone by fixing the code.
	"""
	_require_admin(x_admin_token)
	ids = body.get("ids") or []
	if not isinstance(ids, list) or not ids:
		raise HTTPException(400, "ids is required")
	marked = store.mark_referrals_paid(
		ids, how=body.get("how", "manual"), detail=body.get("detail", "")
	)
	return {"ok": True, "marked": marked}


# ── affiliates: signing themselves up ────────────────────────────────────────
#
# Everything from here to the next heading is reachable without an admin token,
# which makes it the only part of this service a stranger can drive. The rules
# it works to:
#
#   · Nothing earns until an email address has been proved. That is the entire
#     gate on an open form — a code, a name and a country are free to type, and
#     only the mailbox is not.
#   · Nothing here says whether an address or a code belongs to somebody.
#     Sign-in answers identically for an account that exists and one that does
#     not, because the alternative is a way to ask us who our affiliates are.
#   · Nothing here can move money. An affiliate can change where their payout
#     goes; sending it is still the owner pressing a button on the admin page.
#
# The commission rate is taken from settings, never from the request. It arrives
# from a form on a page we do not control, and a rate that came in with it would
# be a rate the applicant chose.


def _affiliate_settings() -> dict:
	return settings.get()["affiliates"]


def _ref_link(code: str) -> str:
	return f"{SHOP_URL}/?ref={code}"


def _sign_in(code: str, to: str = "") -> RedirectResponse:
	"""Land the affiliate on their dashboard, signed in.

	A redirect rather than a JSON token, because this is the end of a link
	clicked in an email client — whatever opens it has to be handed a page, and
	the session goes in an HttpOnly cookie the page itself cannot read or leak.
	"""
	store.touch_affiliate(code)
	res = RedirectResponse(to or PORTAL_URL, status_code=303)
	res.set_cookie(
		SESSION_COOKIE,
		crypto.make_scoped("aff-session", code, SESSION_DAYS * 24 * 60),
		max_age=SESSION_DAYS * 86400,
		httponly=True,
		secure=COOKIE_SECURE,
		samesite="lax",
		path="/",
	)
	return res


def _looks_like_email(value: str) -> bool:
	"""Enough to catch a typo, not an attempt to decide what is deliverable.

	Whether an address works is answered by the confirmation email either
	arriving or not, and every regex that tries to answer it in advance rejects
	somebody's real address.
	"""
	value = (value or "").strip()
	return "@" in value[1:-1] and "." in value.rsplit("@", 1)[-1] and len(value) <= 200


def _register_affiliate(fields: dict, ip: str) -> dict:
	"""Take one application, whatever carried it here.

	The rules live here, once, because there are two front doors: a plain HTML
	form that submits itself, and a JSON endpoint. They must accept and refuse
	exactly the same things — a code allowed at one door and refused at the other
	is somebody who has already put their link in a video description.

	Creates the affiliate straight away, but as `pending`: the row is real, the
	code is reserved so nobody else takes it while they read their email, and
	`_credit_affiliate` will not pay a penny to anything that is not `active`.
	"""
	# Its own bucket, and a much tighter one than the licence endpoints: five
	# sign-ups from one address in ten minutes is either a mistake or a script.
	_throttle(f"apply:{ip}", limit=5, window=600)

	config = _affiliate_settings()
	if not config["enabled"] or not config.get("selfSignup", True):
		raise HTTPException(
			403,
			"Sign-ups are closed at the moment. Email us and we will sort you out by hand.",
		)

	# A field a person never sees and never fills in. A bot fills in everything.
	# Answered with success rather than an error, because an error is feedback a
	# script can use to work out what it got wrong.
	if (fields.get("website") or "").strip():
		log.info("affiliate sign-up honeypot tripped from %s", ip)
		return {"ok": True, "status": "pending", "code": "", "email": ""}

	name = (fields.get("name") or "").strip()[:120]
	email = (fields.get("email") or "").strip().lower()[:200]
	code = store.normalise_code(fields.get("code", ""))
	country = (fields.get("country") or "").strip().upper()[:2]
	promo = (fields.get("promo") or "").strip()[:500]

	if not name:
		raise HTTPException(400, "Your name is required.")
	if not _looks_like_email(email):
		raise HTTPException(400, "That does not look like an email address.")
	problem = store.code_problem(code)
	if problem:
		raise HTTPException(400, problem)
	if store.get_affiliate(code):
		raise HTTPException(409, f"'{code}' is taken. Try adding your channel name or a number.")
	if store.affiliate_by_email(email):
		# Not "you already applied" — this is answered to whoever typed the
		# address, who may not be its owner. It points at sign-in, which sends a
		# link to the mailbox and tells an outsider nothing.
		raise HTTPException(
			409,
			"There is already an account for that email. Use the sign-in link on the "
			"dashboard and we will email you a way in.",
		)
	if country and not country.isalpha():
		raise HTTPException(400, "Country should be two letters, e.g. PK, GB, US.")

	affiliate = store.add_affiliate(
		code=code,
		name=name,
		email=email,
		# From settings, never from the form.
		rate_pct=int(config["ratePct"]),
		payout_method="manual",
		payout_to="",
		status="pending",
		source="signup",
		country=country,
		promo=promo,
	)
	mail.send_affiliate_verify(
		email,
		name,
		f"{PORTAL_URL}/confirm?t={crypto.make_scoped('aff-verify', code, VERIFY_LINK_MINUTES)}",
	)
	log.info("affiliate %s applied (%s, %s)", code, email, country or "no country")
	return {"ok": True, "status": "pending", "code": affiliate["code"], "email": email}


@app.post("/partner/join", include_in_schema=False)
async def affiliate_join_form(request: Request):
	"""The sign-up form, submitted by the browser itself.

	This is the front door, and it is a plain HTML form post — no JavaScript, no
	`fetch`, no CORS. That is not nostalgia. A form the page submits with
	`fetch` is an XHR, and ad blockers, privacy extensions, company firewalls and
	antivirus proxies all filter XHR by URL while leaving ordinary navigation
	alone. When one of them decides against the request, the browser hands
	JavaScript a bare "Failed to fetch" — no status, no reason — while the server
	sits there healthy, answering everyone else. That happened here, and it cost
	most of a day to find, because every layer looked innocent from the inside.

	A form submission is a navigation. There is nothing in it for any of those
	things to match on, it works with JavaScript switched off, and it works in a
	browser older than any of this. The reply is a redirect to a page that says
	what happened.

	The body is parsed by hand rather than with `Form(...)`, which would pull in
	python-multipart for one endpoint — the same trade already made in
	stripe_api.py, where four form posts were written out instead of taking the
	SDK.
	"""
	raw = (await request.body()).decode("utf-8", "replace")
	fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

	try:
		result = _register_affiliate(fields, _client_ip(request))
	except HTTPException as exc:
		# Back to the page that said it, with the reason. A form post that ends on
		# a bare error page is one somebody has to retype from memory.
		return RedirectResponse(
			f"{PORTAL_URL}?problem={urllib.parse.quote(str(exc.detail))}", status_code=303
		)

	# 303 and not 307: the browser must follow this with a GET, or the back
	# button and a refresh re-submit the application.
	return RedirectResponse(
		f"{PORTAL_URL}?joined={urllib.parse.quote(result['email'])}", status_code=303
	)


@app.post("/api/partner/join")
@app.post("/api/affiliates/apply")
def affiliate_apply(request: Request, body: dict = Body(...)):
	"""The same sign-up, as JSON.

	Kept because it is what shipped and something may still be calling it. The
	form above is the path the site uses.
	"""
	return _register_affiliate(body, _client_ip(request))


@app.get("/api/partner/confirm")
@app.get("/api/affiliates/verify")
def affiliate_verify(t: str = ""):
	"""The link in the confirmation email.

	Idempotent on purpose. People click these twice, and forwarded mail gets
	opened by a scanner first — the second visit has to sign them in rather than
	tell them the link is dead.
	"""
	code = crypto.read_scoped(t, "aff-verify")
	affiliate = store.get_affiliate(code) if code else None
	if not affiliate:
		return RedirectResponse(f"{PORTAL_URL}?problem=link", status_code=303)

	if affiliate["status"] == "pending":
		config = _affiliate_settings()
		auto = bool(config.get("autoApprove", True))
		affiliate = store.verify_affiliate_email(code, "active" if auto else "review")
		if auto:
			mail.send_affiliate_welcome(
				affiliate["email"], affiliate["name"], code,
				_ref_link(code), PORTAL_URL, int(affiliate["rate_pct"]),
			)
		# The owner hears about it once the address is proved, not when the form
		# was submitted — so the inbox only ever sees real people.
		mail.notify_owner_new_affiliate(affiliate, needs_review=not auto, admin_url=ADMIN_URL)
		log.info("affiliate %s confirmed -> %s", code, affiliate["status"])

	return _sign_in(code)


@app.post("/api/partner/signin")
@app.post("/api/affiliates/login")
def affiliate_login(request: Request, body: dict = Body(...)):
	"""Ask for a sign-in link.

	No passwords anywhere in this system. There is nothing to store, nothing to
	reset, nothing to leak, and the mailbox is already the thing that proves who
	an affiliate is — it is where their money is arranged and where their link
	was sent.

	The answer is the same whatever is behind the address. Anything else turns
	this into a way to test whether a given person promotes us.
	"""
	_throttle(f"login:{_client_ip(request)}", limit=6, window=600)
	email = (body.get("email") or "").strip().lower()
	affiliate = store.affiliate_by_email(email) if _looks_like_email(email) else None

	if affiliate and affiliate["status"] != "rejected":
		if not affiliate["email_verified"]:
			# They never finished signing up. Send the confirmation again rather
			# than a sign-in link, which would skip the one check that matters.
			mail.send_affiliate_verify(
				affiliate["email"], affiliate["name"],
				f"{PORTAL_URL}/confirm?t={crypto.make_scoped('aff-verify', affiliate['code'], VERIFY_LINK_MINUTES)}",
			)
		else:
			mail.send_affiliate_login(
				affiliate["email"],
				f"{PORTAL_URL}/enter?t={crypto.make_scoped('aff-login', affiliate['code'], LOGIN_LINK_MINUTES)}",
			)

	return {"ok": True, "sent": True}


@app.get("/api/partner/enter")
@app.get("/api/affiliates/session")
def affiliate_session(t: str = ""):
	code = crypto.read_scoped(t, "aff-login")
	if not code or not store.get_affiliate(code):
		return RedirectResponse(f"{PORTAL_URL}?problem=link", status_code=303)
	return _sign_in(code)


@app.post("/api/partner/visit")
@app.post("/api/affiliates/click")
def affiliate_click(request: Request, code: str = ""):
	"""A visit through somebody's referral link.

	Taken as a query parameter with no body, so the browser sends it as a plain
	beacon with no preflight — the site fires this with `sendBeacon` while the
	page is loading and must not pay a round trip for the privilege.

	The answer is `{"ok": true}` whether or not the code exists. It is a counter,
	not a lookup, and a version that said "unknown code" would enumerate our
	affiliates for anybody who asked.
	"""
	_throttle(f"click:{_client_ip(request)}", limit=60, window=60)
	try:
		store.record_click(code)
	except Exception:  # noqa: BLE001 - a statistic must never fail a page view
		log.exception("could not record a click for %r", code)
	return {"ok": True}


# ── affiliates: their own dashboard ──────────────────────────────────────────


def current_affiliate(request: Request) -> dict:
	"""Whoever the session cookie names, as they stand right now.

	Re-read from the database on every request rather than trusted from the
	token. The token is signed and cannot be edited, but it was minted up to a
	month ago and says nothing about whether the account has been disabled since.
	"""
	code = crypto.read_scoped(request.cookies.get(SESSION_COOKIE, ""), "aff-session")
	affiliate = store.get_affiliate(code) if code else None
	if not affiliate:
		raise HTTPException(401, "Please sign in again — we will email you a link.")
	return affiliate


@app.get("/api/partner/me")
@app.get("/api/affiliate/me")
def affiliate_me(request: Request):
	"""Everything an affiliate's own dashboard shows.

	A disabled or rejected account still gets to see this. Commission already
	earned is still owed to them — the terms say so — and locking somebody out of
	the page that says what they are owed is how a disagreement becomes a
	complaint.
	"""
	affiliate = current_affiliate(request)
	code = affiliate["code"]
	config = _affiliate_settings()
	rows = store.referrals(code, limit=200)
	clicks = store.clicks_for(code)

	# Same split the admin page uses, worked out from the same rows, so the two
	# screens can never quote different numbers to the two people in the
	# conversation.
	now = int(time.time())
	due = sum(r["commission"] for r in rows if r["status"] == "pending" and r["due_at"] <= now)
	holding = sum(r["commission"] for r in rows if r["status"] == "pending" and r["due_at"] > now)
	paid = sum(r["commission"] for r in rows if r["status"] == "paid")
	sales = [r for r in rows if r["status"] != "void"]

	return {
		"affiliate": {
			"code": code,
			"name": affiliate["name"],
			"email": affiliate["email"],
			"status": affiliate["status"],
			"rate_pct": affiliate["rate_pct"],
			"country": affiliate["country"] or "",
			"payout_method": affiliate["payout_method"],
			"payout_to": affiliate["payout_to"] or "",
			"stripe_account": bool(affiliate["stripe_account"]),
			"stripe_ready": bool(affiliate["stripe_ready"]),
			"decided_note": affiliate["decided_note"] or "",
		},
		"link": _ref_link(code),
		"clicks": clicks,
		"totals": {
			"due": due,
			"holding": holding,
			"paid": paid,
			"sales": len(sales),
			"currency": (rows[0]["currency"] if rows else "usd"),
		},
		# Their own sales, without the buyer's email address. An affiliate is owed
		# the fact of the sale and the money; they are not owed the identity of a
		# customer who never dealt with them.
		"sales": [
			{
				"created_at": r["created_at"],
				"due_at": r["due_at"],
				"gross": r["gross"],
				"commission": r["commission"],
				"currency": r["currency"],
				"status": r["status"],
				"payable": bool(r["payable"]),
			}
			for r in rows
		],
		"programme": {
			"open": bool(config["enabled"]),
			"hold_days": int(config["holdDays"]),
			"stripe_payouts": stripe_api.configured(),
		},
	}


@app.post("/api/partner/payout")
@app.post("/api/affiliate/payout")
def affiliate_set_payout(request: Request, body: dict = Body(...)):
	"""Where the affiliate wants their money sent.

	This is the part of the programme that has to work from anywhere. Stripe
	pays out to around fifty countries and to nobody else, so an affiliate in
	Pakistan, Bangladesh or Nigeria picks `manual` and writes down a Wise or
	PayPal address — the same commission, on the same schedule, sent by hand.
	"""
	affiliate = current_affiliate(request)
	method = (body.get("payout_method") or "").strip().lower()
	if method not in ("manual", "stripe"):
		raise HTTPException(400, "Choose either automatic Stripe payouts or being paid by hand.")

	payout_to = (body.get("payout_to") or "").strip()
	if method == "manual" and not payout_to:
		raise HTTPException(
			400, "Tell us where to send it — a Wise or PayPal email, or account details."
		)
	if method == "stripe" and not stripe_api.configured():
		raise HTTPException(
			503,
			"Automatic Stripe payouts are not switched on yet. Choose paying by hand for now — "
			"you will be paid exactly the same, we just send it ourselves.",
		)

	store.set_affiliate_payout(affiliate["code"], method, payout_to if method == "manual" else "")
	return {"ok": True}


def _stripe_onboarding(affiliate: dict, country: str) -> dict:
	"""Create the Connect account if there is not one, and return a fresh link.

	Shared by the affiliate doing it themselves and the owner doing it for them,
	so both paths create at most one account per person. Two Stripe accounts for
	one affiliate is a mess only Stripe support can untangle.
	"""
	account_id = affiliate["stripe_account"]
	if not account_id:
		country = (country or affiliate["country"] or "").strip()
		if len(country) != 2:
			raise HTTPException(400, "A two-letter country code is required, e.g. GB or DE")
		account_id = stripe_api.create_express_account(affiliate["email"], country)["id"]
		store.set_affiliate_stripe(affiliate["code"], account_id, ready=False)
	return {"ok": True, "account": account_id, "url": stripe_api.onboarding_link(account_id)}


@app.post("/api/partner/stripe")
@app.post("/api/affiliate/stripe")
def affiliate_stripe(request: Request, body: dict = Body(default={})):
	"""The affiliate starts their own Stripe onboarding.

	They go to Stripe, who collect the identity documents and the bank details
	and own the compliance that comes with both. We never see or hold either —
	which is the point of using Connect rather than asking for an IBAN in a form.
	"""
	affiliate = current_affiliate(request)
	if not stripe_api.configured():
		raise HTTPException(
			503, "Automatic payouts are not switched on yet — you will be paid by hand instead."
		)
	try:
		return _stripe_onboarding(affiliate, (body.get("country") or "").strip())
	except stripe_api.StripeError as exc:
		# Stripe's wording is the useful wording. "Country not supported" is
		# exactly what this person needs to read, and it is the cue to pick being
		# paid by hand instead.
		raise HTTPException(400, str(exc)) from exc


@app.post("/api/partner/signout")
@app.post("/api/affiliate/logout")
def affiliate_logout():
	res = JSONResponse({"ok": True})
	res.delete_cookie(SESSION_COOKIE, path="/")
	return res


_PORTAL_PAGE = pathlib.Path(__file__).with_name("affiliate.html")


@app.get("/partner", response_class=HTMLResponse, include_in_schema=False)
@app.get("/partner/confirm", include_in_schema=False)
@app.get("/partner/enter", include_in_schema=False)
@app.get("/affiliate", response_class=HTMLResponse, include_in_schema=False)
@app.get("/affiliate/verify", include_in_schema=False)
@app.get("/affiliate/session", include_in_schema=False)
def affiliate_portal(request: Request, t: str = ""):
	"""The affiliate's dashboard, and the two links that land on it.

	These are under `/partner` rather than `/api` because they are pasted into
	emails and read by people — a link with `/api/` in it looks like something
	that was not meant for them. They hand straight off to the endpoints that do
	the work.
	"""
	if request.url.path.endswith(("/verify", "/confirm")):
		return affiliate_verify(t)
	if request.url.path.endswith(("/session", "/enter")):
		return affiliate_session(t)
	return HTMLResponse(_PORTAL_PAGE.read_text(encoding="utf-8"))


# ── site settings ────────────────────────────────────────────────────────────


@app.get("/api/site-config")
def site_config():
	"""What the marketing site reads when it builds.

	Public and unauthenticated, because every value in it is about to be printed
	into public HTML anyway. The site treats a failure here as "use the values I
	have committed" rather than an error, so this being down delays a price
	change; it never breaks a build or ships an empty pricing page.
	"""
	return JSONResponse(
		settings.public(),
		# A build reads this once. A short cache absorbs a burst without ever
		# being old enough that "save, publish" reads yesterday's price.
		headers={"Cache-Control": "public, max-age=10"},
	)


@app.get("/api/admin/settings")
def admin_settings(x_admin_token: str = Header("")):
	_require_admin(x_admin_token)
	current = settings.get(fresh=True)
	return {
		"settings": current,
		"defaults": settings.DEFAULTS,
		# Whether the Publish button can do anything. Told, not guessed, so the
		# page can explain instead of failing.
		"publish_ready": bool(PUBLISH_HOOK_URL),
		"warnings": _setting_warnings(current),
	}


def _setting_warnings(s: dict) -> list[str]:
	"""Things that are allowed but are probably a mistake.

	Kept apart from validation on purpose. Validation refuses to save; this only
	says so out loud, because an owner is allowed to do something unusual
	deliberately and being blocked from it is worse than being warned.
	"""
	out = []
	price = s["price"]

	if not price["checkoutUrl"]:
		out.append("No checkout link is set, so nobody can buy. The buy button offers WhatsApp instead.")
	elif "/test_" in price["checkoutUrl"]:
		out.append("The checkout link is a Stripe TEST link — it takes no money.")

	# The one that costs real money. The price on the page is ours; the price
	# charged belongs to the Stripe Payment Link, and nothing here can change
	# that. If they disagree the customer is charged something they did not
	# agree to, which is a chargeback the buyer wins.
	if price["checkoutUrl"]:
		out.append(
			f"The site now says ${price['amount']}. Check the Stripe Payment Link charges "
			f"exactly that — this dashboard cannot change what Stripe charges."
		)

	if not s["downloads"]["enabled"]:
		out.append("Downloads are switched off. Buyers can still pay but cannot install.")

	version = s.get("version", {})
	latest = str(version.get("latest", "") or "").strip()
	if latest and version.get("announce", True) and s["downloads"]["enabled"]:
		out.append(
			f"Installed copies of Soft Clipper will start offering {latest} within a day. "
			"Check that build really is on R2 under the usual filename."
		)
	elif latest and not version.get("announce", True):
		out.append(f"Version {latest} is set but not announced — installed apps are told nothing.")
	aff = s["affiliates"]
	if not aff["enabled"]:
		out.append("The affiliate programme is closed. New referrals earn nothing; existing commission is still owed.")
	elif not aff.get("selfSignup", True):
		out.append(
			"Self sign-up is off, so the affiliate page shows 'email us' instead of a form. "
			"Anyone already signed up is unaffected."
		)
	elif not aff.get("autoApprove", True):
		out.append(
			"New affiliates wait for your approval. They confirm their email, then sit on the "
			"Affiliates tab until you approve or reject them — nobody earns until you do."
		)
	return out


@app.post("/api/admin/settings")
def admin_save_settings(body: dict = Body(...), x_admin_token: str = Header("")):
	"""Save, but do not publish. The site still shows the old values until the
	rebuild runs — two steps rather than one, so a half-finished edit is never
	live while you are still typing the rest of it."""
	_require_admin(x_admin_token)
	try:
		saved = settings.save(body)
	except settings.Invalid as exc:
		raise HTTPException(400, str(exc)) from exc
	log.info("settings saved: %s", ", ".join(sorted(body.keys())))
	return {"ok": True, "settings": saved, "warnings": _setting_warnings(saved)}


@app.post("/api/admin/publish")
def admin_publish(x_admin_token: str = Header("")):
	"""Rebuild the marketing site so the saved settings reach the public HTML.

	The hook is whatever rebuilds the site — a Cloudflare deploy hook, a GitHub
	repository_dispatch. Kept as a URL in the environment rather than wired to
	one vendor, because the thing most likely to change here is the host.
	"""
	_require_admin(x_admin_token)
	if not PUBLISH_HOOK_URL:
		raise HTTPException(
			503,
			"Publishing is not wired up. Set PUBLISH_HOOK_URL on the server, or push the "
			"site repository to rebuild it — the settings are saved either way.",
		)
	try:
		res = httpx.post(PUBLISH_HOOK_URL, timeout=20)
		res.raise_for_status()
	except httpx.HTTPError as exc:
		raise HTTPException(502, f"The rebuild could not be triggered: {exc}") from exc
	log.info("site rebuild triggered")
	return {"ok": True, "note": "Rebuilding. The site updates in about a minute."}


_ADMIN_PAGE = pathlib.Path(__file__).with_name("admin.html")


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page():
	"""The page a human uses to do the four things support actually needs.

	Serving it here rather than anywhere else means no second deployment, no
	build step and no new hostname — it is one file next to the API it calls.

	The page itself is not a secret and needs no auth to load: it is empty until
	someone supplies the admin token, and every endpoint behind it checks that
	token on every call. Guarding the HTML would protect nothing that the API
	does not already protect.
	"""
	return HTMLResponse(_ADMIN_PAGE.read_text(encoding="utf-8"))


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
	# The app shows `error` straight to the customer, so it is written for them.
	return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
