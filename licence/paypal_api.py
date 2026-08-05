"""Paying an affiliate through PayPal Payouts.

The second automatic rail. Stripe Connect reaches about fifty countries and
refuses to create an account for anybody else; PayPal reaches a different set —
overlapping, but wider in Europe, Latin America and South-East Asia — and needs
nothing from the affiliate except an email address they already have.

Shaped exactly like `stripe_api`: form of the same four ideas, plain httpx, no
SDK. One call moves money, and it is the one that takes an idempotency key as a
required argument.

    a token          OAuth2, cached until it expires
    a payout         the money, one recipient at a time
    a batch          what happened to it afterwards

Inert until PAYPAL_CLIENT_ID and PAYPAL_SECRET are set, the same way
`stripe_api` is inert without its key: `configured()` is False, the dashboards
do not offer PayPal, and everyone falls back to being paid by hand.

Two things about PayPal that are not obvious and cost money if they are:

  · **A payout to an address with no PayPal account does not fail.** It sits as
    UNCLAIMED for thirty days while PayPal emails them to come and get it, and
    then returns to us. So "sent" here means sent, not received — the affiliate
    row is marked paid because the money did leave, and `payout_status` is how
    an unclaimed one is noticed.
  · **`sender_batch_id` is the only thing standing between a retry and paying
    twice.** PayPal rejects a batch id it has seen before, which is exactly the
    behaviour we want, and it is capped at 30 characters — hence the hash.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

import httpx

log = logging.getLogger("licence.paypal")

CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
SECRET = os.environ.get("PAYPAL_SECRET", "")
# Sandbox is a different hostname with entirely separate accounts and money that
# is not money. Defaulting to live is deliberate: a deployment that quietly ran
# in sandbox would report every payout as sent and move nothing at all.
API = (
	"https://api-m.sandbox.paypal.com"
	if os.environ.get("PAYPAL_ENV", "live").lower() == "sandbox"
	else "https://api-m.paypal.com"
)


class PayPalError(RuntimeError):
	"""Whatever PayPal said, in a form worth showing an admin."""


def configured() -> bool:
	return bool(CLIENT_ID and SECRET)


# (token, expires_at). Tokens last around nine hours; asking for a new one on
# every payout would be a second call that can fail for every call that matters.
_token_cache: tuple[str, float] = ("", 0.0)


def _token() -> str:
	global _token_cache
	token, expires = _token_cache
	if token and time.time() < expires:
		return token

	try:
		res = httpx.post(
			f"{API}/v1/oauth2/token",
			data={"grant_type": "client_credentials"},
			auth=(CLIENT_ID, SECRET),
			timeout=25,
		)
	except httpx.HTTPError as exc:
		raise PayPalError(f"Could not reach PayPal: {exc}") from exc

	if res.status_code >= 400:
		# The usual cause is a live key against the sandbox host or the other way
		# round, and PayPal's own wording says so better than a guess would.
		raise PayPalError(_message(res, "PayPal refused our credentials"))

	body = res.json()
	# A minute short of what PayPal says, so a token never expires between the
	# check above and the call below.
	_token_cache = (body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60)
	return _token_cache[0]


def _message(res: httpx.Response, fallback: str) -> str:
	"""PayPal puts the useful sentence in one of three places depending on which
	part of PayPal answered. Try all three before falling back to a status code,
	which tells an admin nothing they can act on."""
	try:
		body = res.json()
	except ValueError:
		return f"{fallback} ({res.status_code})"

	details = body.get("details") or []
	if details and isinstance(details, list):
		first = details[0]
		issue = first.get("description") or first.get("issue") or ""
		if issue:
			return issue
	return body.get("message") or body.get("error_description") or f"{fallback} ({res.status_code})"


def _call(method: str, path: str, payload: dict | None = None) -> dict:
	if not configured():
		raise PayPalError("PayPal payouts are not switched on — PAYPAL_CLIENT_ID is not set.")

	try:
		res = httpx.request(
			method,
			f"{API}{path}",
			json=payload,
			headers={
				"Authorization": f"Bearer {_token()}",
				"Content-Type": "application/json",
			},
			timeout=30,
		)
	except httpx.HTTPError as exc:
		raise PayPalError(f"Could not reach PayPal: {exc}") from exc

	if res.status_code >= 400:
		raise PayPalError(_message(res, "PayPal refused the payout"))
	return res.json() if res.content else {}


def batch_id(reference: str) -> str:
	"""A stable `sender_batch_id` for one specific payout.

	PayPal caps this at 30 characters, and our reference — the affiliate code and
	the range of referral rows being settled — is longer than that. Hashing keeps
	it inside the limit while keeping the one property that matters: the same
	payout derives the same id, a different payout derives a different one. A
	retry after a timeout is therefore refused as a duplicate instead of paying
	the same commission a second time.
	"""
	return "sc" + hashlib.sha1(reference.encode()).hexdigest()[:26]


def create_payout(reference: str, email: str, amount: int, currency: str, note: str = "") -> dict:
	"""Send one affiliate what they are owed.

	One item per batch rather than everybody at once. A batch is atomic in
	PayPal's reporting but not in its failures — one bad address inside a batch of
	twenty leaves nineteen sent and one in a state that has to be unpicked by
	hand, against rows we have already marked paid. One payout per affiliate keeps
	"did this person get paid" a question with one answer.
	"""
	if amount <= 0:
		raise PayPalError("Nothing to pay")

	body = _call(
		"POST",
		"/v1/payments/payouts",
		{
			"sender_batch_header": {
				"sender_batch_id": batch_id(reference),
				"email_subject": "Your Soft Clipper commission",
				"email_message": note or "Thank you for the referrals.",
			},
			"items": [
				{
					"recipient_type": "EMAIL",
					"receiver": email,
					# PayPal takes a decimal string, not the smallest unit. Built
					# from the integer we hold rather than from a float, which is
					# how a payout ends up a cent short of what the row says.
					"amount": {"value": f"{amount // 100}.{amount % 100:02d}", "currency": currency.upper()},
					"note": note or "Soft Clipper affiliate commission",
					"sender_item_id": batch_id(reference),
				}
			],
		},
	)
	return {
		"id": (body.get("batch_header") or {}).get("payout_batch_id", ""),
		"status": (body.get("batch_header") or {}).get("batch_status", ""),
	}


def payout_status(batch: str) -> dict:
	"""What became of a payout. `SUCCESS` on the batch is PayPal accepting it;
	the item status underneath is whether the person actually has it — `UNCLAIMED`
	means they have no PayPal account for that address yet and have thirty days to
	open one before it comes back to us."""
	body = _call("GET", f"/v1/payments/payouts/{batch}")
	items = body.get("items") or []
	return {
		"status": (body.get("batch_header") or {}).get("batch_status", ""),
		"item_status": (items[0].get("transaction_status") if items else ""),
	}
