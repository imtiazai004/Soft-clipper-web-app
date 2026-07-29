"""The small part of Stripe's API we call ourselves.

The webhook only ever needed to *verify* a signature, which is thirty lines of
hmac and no dependency. Paying an affiliate is different: it moves money out of
the account, so it has to be a real API call with a real secret key.

Four calls, not the SDK:

    create an Express account            so Stripe can pay them
    create an onboarding link            so they can prove who they are
    read an account                      so we know whether Stripe will pay it
    create a transfer                    the money

The SDK would bring a large dependency and its own release cadence to a service
whose whole point is to stay up. Four form-encoded POSTs are easier to reason
about, and the failure of any of them is something we want to handle explicitly
rather than catch as a class we have to go and read about.

Everything here is inert until STRIPE_SECRET_KEY is set. That is deliberate:
until it is, `configured()` is False, the admin page says Stripe payouts are
switched off, and every affiliate falls back to being paid by hand. Nothing
half-works.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("licence.stripe")

API = "https://api.stripe.com/v1"
SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
# Where Stripe sends the affiliate after onboarding. Both are pages on the
# marketing site; neither needs to exist for the transfer path to work.
RETURN_URL = os.environ.get("AFFILIATE_RETURN_URL", "https://softclipper.pro/affiliates/thanks/")
REFRESH_URL = os.environ.get("AFFILIATE_REFRESH_URL", "https://softclipper.pro/affiliates/")


class StripeError(RuntimeError):
	"""Whatever Stripe said, in a form worth showing an admin."""


def configured() -> bool:
	return bool(SECRET_KEY)


def _call(method: str, path: str, data: dict | None = None, idempotency_key: str = "") -> dict:
	if not configured():
		raise StripeError("Stripe payouts are not switched on — STRIPE_SECRET_KEY is not set.")

	headers = {"Authorization": f"Bearer {SECRET_KEY}"}
	# Stripe deduplicates by this key for 24 hours. On a transfer it is the
	# difference between a retried request and a second payment: without it, a
	# timeout that actually succeeded looks exactly like one that did not, and
	# the safe-looking thing to do — try again — sends the money twice.
	if idempotency_key:
		headers["Idempotency-Key"] = idempotency_key

	try:
		res = httpx.request(
			method, f"{API}{path}", data=_flatten(data or {}), headers=headers, timeout=25
		)
	except httpx.HTTPError as exc:
		raise StripeError(f"Could not reach Stripe: {exc}") from exc

	body = res.json() if res.content else {}
	if res.status_code >= 400:
		message = (body.get("error") or {}).get("message") or f"Stripe returned {res.status_code}"
		raise StripeError(message)
	return body


def _flatten(data: dict, prefix: str = "") -> dict:
	"""Stripe takes form encoding with bracket notation, not JSON."""
	out: dict[str, str] = {}
	for key, value in data.items():
		name = f"{prefix}[{key}]" if prefix else key
		if isinstance(value, dict):
			out.update(_flatten(value, name))
		elif isinstance(value, bool):
			out[name] = "true" if value else "false"
		elif value is not None:
			out[name] = str(value)
	return out


def create_express_account(email: str, country: str) -> dict:
	"""An Express account: Stripe hosts the onboarding, the identity checks and
	the payout schedule, and owns the compliance that comes with all three.

	`country` is the affiliate's, and it is the whole reason this path is not
	available to everyone — Stripe can only create an account in a country it
	supports, and it refuses here rather than failing later at transfer time.
	"""
	return _call(
		"POST",
		"/accounts",
		{
			"type": "express",
			"email": email,
			"country": (country or "").upper(),
			"capabilities": {"transfers": {"requested": True}},
			# The affiliate is being paid for referrals; they are not a merchant
			# selling anything of their own through us.
			"business_type": "individual",
		},
	)


def onboarding_link(account: str) -> str:
	"""A single-use link to Stripe's hosted onboarding. Short-lived by design —
	if it has expired, generate another; there is nothing to recover."""
	link = _call(
		"POST",
		"/account_links",
		{
			"account": account,
			"type": "account_onboarding",
			"return_url": RETURN_URL,
			"refresh_url": REFRESH_URL,
		},
	)
	return link.get("url", "")


def account(account_id: str) -> dict:
	return _call("GET", f"/accounts/{account_id}")


def payouts_enabled(acct: dict) -> bool:
	"""Stripe's own verdict. `payouts_enabled` can be true while `transfers` is
	still pending, and a transfer to that account fails — so both are required
	before we tell an admin the affiliate is ready."""
	caps = acct.get("capabilities") or {}
	return bool(acct.get("payouts_enabled")) and caps.get("transfers") == "active"


def create_transfer(account_id: str, amount: int, currency: str, idempotency_key: str,
                    description: str = "") -> dict:
	"""Move commission from our balance to theirs.

	This is the one call in the service that spends money, so it takes an
	idempotency key as a required argument rather than an optional one — there is
	no correct way to call it without.
	"""
	if amount <= 0:
		raise StripeError("Nothing to transfer")
	return _call(
		"POST",
		"/transfers",
		{
			"amount": amount,
			"currency": (currency or "usd").lower(),
			"destination": account_id,
			"description": description or "Soft Clipper affiliate commission",
		},
		idempotency_key=idempotency_key,
	)
