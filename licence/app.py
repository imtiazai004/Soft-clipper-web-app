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

import pathlib

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import crypto, mail, settings, store, stripe_api

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


def _credit_affiliate(ref: str, key: str, session_id: str, session: dict):
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
		log.warning("session %s carried disabled ref %r", session_id, ref)
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
				_credit_affiliate(ref, key, session_id, obj)
			except Exception:
				log.exception("could not credit affiliate %s for session %s", ref, session_id)

		return {"ok": True, "key": key}

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
		account_id = affiliate["stripe_account"]
		if not account_id:
			country = (body.get("country") or "").strip()
			if len(country) != 2:
				raise HTTPException(400, "A two-letter country code is required, e.g. GB or DE")
			created = stripe_api.create_express_account(affiliate["email"], country)
			account_id = created["id"]
			store.set_affiliate_stripe(code, account_id, ready=False)
		return {"ok": True, "account": account_id, "url": stripe_api.onboarding_link(account_id)}
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
	store.set_affiliate_status(code, status)
	return {"ok": True}


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
	if not s["affiliates"]["enabled"]:
		out.append("The affiliate programme is closed. New referrals earn nothing; existing commission is still owed.")
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
