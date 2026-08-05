"""Paying an affiliate through Wise.

The third automatic rail, and the one that reaches furthest. Stripe needs the
affiliate to live in a country Stripe supports; PayPal needs them to be able to
hold a PayPal balance. Wise needs neither — **it pays into an ordinary bank
account**, in around 160 countries including the ones the other two refuse, and
the affiliate does not need a Wise account of their own to receive it.

That last point is worth saying twice, because everyone assumes the opposite:
"pay by Wise" here means *we* send from our Wise business account to *their
local bank*, in their own currency.

What makes this module longer than the other two is not the transfer. It is that
a bank account is a different shape in every country — IBAN here, sort code and
account number there, routing number and a postal address somewhere else. Rather
than hard-code a guess per currency and get PKR or BRL subtly wrong, we **ask
Wise what it needs** (`account_requirements`) and hand that straight to the
affiliate's browser to render. Wise even supplies the validation regex, so the
field is checked by the people who will reject it, not by us.

The sequence, which is fixed and cannot be reordered:

    profile          who we are sending as (the business, not the personal one)
    quote            the rate and, with it, what fields this currency needs
    recipient        their bank account, created once and reused
    transfer         the instruction
    fund             the money actually leaving our balance

Inert until WISE_API_TOKEN is set, like the other two rails.

**The funding step is the one that will surprise you.** Wise treats "move money
out of the balance" as a strong-authentication action: the first call comes back
403 with a one-time token in a header, which has to be signed with a private key
registered in the Wise dashboard and the call repeated. Without WISE_PRIVATE_KEY
set, transfers are created and left *unfunded* — visible in Wise, waiting for
somebody to press the button there. That is a deliberate half-step and it is
reported as one: money that has not moved must never be recorded as sent.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import uuid

import httpx

log = logging.getLogger("licence.wise")

TOKEN = os.environ.get("WISE_API_TOKEN", "")
API = (
	"https://api.sandbox.transferwise.tech"
	if os.environ.get("WISE_ENV", "live").lower() == "sandbox"
	else "https://api.wise.com"
)
# Set to skip the profile lookup, or when the token can see more than one
# business. Blank means "find the business profile", which is right for the
# ordinary case of one account with one business on it.
PROFILE_ID = os.environ.get("WISE_PROFILE_ID", "")
# What our balance is held in. Commission is recorded in the currency the
# customer paid — dollars today — and the transfer is priced from that side, so
# a conversion can never spend more than the row says.
SOURCE_CURRENCY = os.environ.get("WISE_SOURCE_CURRENCY", "USD")
# PEM private key, base64-encoded so it survives a one-line .env. Its public half
# goes in the Wise dashboard under API tokens.
PRIVATE_KEY = os.environ.get("WISE_PRIVATE_KEY", "")
# A reference the recipient sees on their statement. Kept short: several banking
# networks truncate it, and a truncated word is worse than a short one.
REFERENCE = os.environ.get("WISE_REFERENCE", "Soft Clipper")
# Wise-to-Wise only: the affiliate gives the email address on their own Wise
# account and the money lands in their Wise balance, rather than being paid out
# to a bank account we were given the details of.
#
# The owner's choice, and there are real reasons for it — it is instant, it costs
# less, and no bank details ever pass through this service. **The cost is
# reach**: a Wise account can only be opened from a country Wise serves, and that
# list is much shorter than the list of countries Wise can *send* to. Pakistan is
# on the second list and not the first. So with this on, an affiliate in Pakistan
# cannot use this rail at all and falls back to being paid by hand — which is
# exactly the person the Wise rail was added for. Off by default for that reason.
WISE_ONLY = os.environ.get("WISE_WISE_ONLY", "").strip().lower() in ("1", "true", "yes")

# One UUID namespace for the whole service, so `customerTransactionId` is a pure
# function of what is being paid. Wise deduplicates on it: a retry after a
# timeout produces the same UUID and Wise returns the original transfer rather
# than creating a second one. This is the Wise spelling of an idempotency key,
# and it is the only thing between a network blip and paying twice.
_NAMESPACE = uuid.UUID("6f2a1d7e-4c3b-5a89-9d1e-0b7c2f4a8e35")


class WiseError(RuntimeError):
	"""Whatever Wise said, in a form worth showing an admin."""


def configured() -> bool:
	return bool(TOKEN)


def can_fund() -> bool:
	"""Whether we can complete a transfer, or only create one.

	Separate from `configured()` on purpose. A deployment with a token but no
	signing key can do everything up to the last step, and the honest thing is to
	say so before somebody presses Pay, not after.
	"""
	return bool(PRIVATE_KEY)


def _message(res: httpx.Response, fallback: str) -> str:
	"""Wise reports errors in two shapes: a top-level `errors` array for
	validation, and `error`/`error_description` for auth. The field-level one is
	the useful one — it names the field the affiliate typed wrong."""
	try:
		body = res.json()
	except ValueError:
		return f"{fallback} ({res.status_code})"

	errors = body.get("errors") or []
	if errors and isinstance(errors, list):
		parts = [
			f"{e.get('path') or e.get('code') or ''}: {e.get('message', '')}".strip(": ")
			for e in errors
			if e.get("message")
		]
		if parts:
			return "; ".join(parts)
	return body.get("error_description") or body.get("message") or f"{fallback} ({res.status_code})"


def _call(
	method: str, path: str, payload: dict | None = None, extra_headers: dict | None = None
) -> tuple[dict, httpx.Response]:
	"""Every Wise call, returning the response as well as the body.

	The response object is needed by exactly one caller — funding, which reads a
	challenge out of a header on a 403 that is not really a failure — and hiding
	it would mean a second HTTP helper that exists only for that.
	"""
	if not configured():
		raise WiseError("Wise payouts are not switched on — WISE_API_TOKEN is not set.")

	headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
	headers.update(extra_headers or {})
	try:
		res = httpx.request(method, f"{API}{path}", json=payload, headers=headers, timeout=30)
	except httpx.HTTPError as exc:
		raise WiseError(f"Could not reach Wise: {exc}") from exc

	# 403 is handled by the funding call rather than raised here: it is how Wise
	# asks for a signature, not how it refuses.
	if res.status_code >= 400 and res.status_code != 403:
		raise WiseError(_message(res, "Wise refused the request"))
	return (res.json() if res.content and res.status_code < 400 else {}), res


_profile_cache = ""


def profile_id() -> str:
	"""The business profile to send from.

	Cached for the life of the process: it is a constant per account, and looking
	it up on every payout is a call that can fail in front of a call that matters.
	The business one, never the personal one — sending business money from a
	personal profile is a tax problem, not a code problem.
	"""
	global _profile_cache
	if PROFILE_ID:
		return PROFILE_ID
	if _profile_cache:
		return _profile_cache

	body, _ = _call("GET", "/v2/profiles")
	profiles = body if isinstance(body, list) else body.get("profiles", [])
	business = [p for p in profiles if p.get("type") == "BUSINESS"]
	chosen = (business or profiles or [{}])[0].get("id")
	if not chosen:
		raise WiseError("No Wise profile found for this token.")
	_profile_cache = str(chosen)
	return _profile_cache


def quote(target_currency: str, amount: int) -> dict:
	"""Price a transfer of `amount` (in the smallest unit of SOURCE_CURRENCY).

	`sourceAmount` rather than `targetAmount`, always: it fixes what leaves our
	account at exactly what the referral rows say, and lets the rate decide what
	arrives. The other way round, a rate move between quoting and funding spends
	more than the commission was.
	"""
	body, _ = _call(
		"POST",
		f"/v3/profiles/{profile_id()}/quotes",
		{
			"sourceCurrency": SOURCE_CURRENCY.upper(),
			"targetCurrency": target_currency.upper(),
			"sourceAmount": amount / 100,
			"payOut": "BANK_TRANSFER",
		},
	)
	return body


def account_requirements(quote_id: str) -> list[dict]:
	"""What Wise needs to know about a bank account in this currency.

	Handed to the browser and rendered as-is. Every country's answer is different
	and several of them are surprising — this is why there is no per-currency
	table in this file to go stale, and why a currency Wise adds next year works
	without a deploy.
	"""
	body, _ = _call("GET", f"/v1/quotes/{quote_id}/account-requirements")
	types = body if isinstance(body, list) else []
	out = []
	for t in types:
		fields = []
		for group_holder in t.get("fields") or []:
			for g in group_holder.get("group") or []:
				fields.append(
					{
						"key": g.get("key", ""),
						"name": g.get("name", ""),
						"type": g.get("type", "text"),
						"required": bool(g.get("required")),
						"example": g.get("example", ""),
						# Wise's own regex and its own list of allowed values. The
						# people who will reject the account are the people who
						# wrote these, which is the only validation worth shipping.
						"regexp": g.get("validationRegexp") or "",
						"options": [
							{"key": v.get("key", ""), "name": v.get("name", "")}
							for v in (g.get("valuesAllowed") or [])
						],
					}
				)
		out.append({"type": t.get("type", ""), "title": t.get("title", ""), "fields": fields})
	return out


def email_requirement() -> list[dict]:
	"""The Wise-to-Wise "form", which is one box.

	Shaped exactly like what `account_requirements` returns so the page renders it
	through the same code path. Not fetched from Wise, because there is nothing to
	ask: an email recipient takes an email address in every currency Wise has.
	"""
	return [
		{
			"type": "email",
			"title": "Their Wise account",
			"fields": [
				{
					"key": "email",
					"name": "The email address on your Wise account",
					"type": "text",
					"required": True,
					"example": "you@example.com",
					"regexp": "",
					"options": [],
				}
			],
		}
	]


def create_recipient(currency: str, account_type: str, holder: str, details: dict) -> dict:
	"""Their bank account, as Wise understands it.

	Created when the affiliate saves their details rather than when we pay them,
	so a wrong IBAN is refused while they are looking at the form — by Wise, in
	Wise's words, naming the field. Left until payout time it would surface as a
	failed transfer days later, to the one person who cannot fix it.
	"""
	body, _ = _call(
		"POST",
		"/v1/accounts",
		{
			"profile": profile_id(),
			"currency": currency.upper(),
			"type": account_type,
			"accountHolderName": holder,
			"details": details,
		},
	)
	return body


def create_transfer(recipient_id: str, quote_id: str, reference: str) -> dict:
	"""The instruction. Creating it moves nothing — `fund_transfer` does."""
	body, _ = _call(
		"POST",
		"/v1/transfers",
		{
			"targetAccount": recipient_id,
			"quoteUuid": quote_id,
			"customerTransactionId": str(uuid.uuid5(_NAMESPACE, reference)),
			"details": {"reference": REFERENCE[:20]},
		},
	)
	return body


def _sign(challenge: str) -> str:
	"""Sign Wise's one-time challenge with the key registered on the account.

	This is the whole of Wise's strong-authentication requirement for moving
	money: they hand back a nonce, we prove we hold the private key whose public
	half is in their dashboard, and the retry goes through. RSA with PKCS#1 v1.5
	and SHA-256 — Wise's choice, not ours.
	"""
	from cryptography.hazmat.primitives import hashes, serialization
	from cryptography.hazmat.primitives.asymmetric import padding

	try:
		pem = base64.b64decode(PRIVATE_KEY)
	except Exception as exc:  # noqa: BLE001 - a mangled env var, reported as one
		raise WiseError("WISE_PRIVATE_KEY is not valid base64 of a PEM private key.") from exc

	try:
		key = serialization.load_pem_private_key(pem, password=None)
		signature = key.sign(challenge.encode(), padding.PKCS1v15(), hashes.SHA256())
	except Exception as exc:  # noqa: BLE001 - wrong key type, encrypted key, etc.
		raise WiseError(f"Could not sign Wise's approval challenge: {exc}") from exc
	return base64.b64encode(signature).decode()


def fund_transfer(transfer_id: str) -> dict:
	"""Take the money out of our Wise balance. This is the call that spends.

	Wise answers the first attempt with 403 and a challenge, every time — that is
	not an error and is not retried as one. We sign it and repeat the identical
	request. If there is no signing key, the transfer is left created and
	unfunded, and that is what we report: it is sitting in Wise waiting for a
	human, and pretending otherwise would mark commission paid that has not moved.
	"""
	path = f"/v3/profiles/{profile_id()}/transfers/{transfer_id}/payments"
	body, res = _call("POST", path, {"type": "BALANCE"})
	if res.status_code != 403:
		return body

	challenge = res.headers.get("x-2fa-approval", "")
	if not challenge:
		raise WiseError(_message(res, "Wise refused to fund the transfer"))
	if not can_fund():
		raise WiseError(
			"The transfer was created in Wise but not funded: WISE_PRIVATE_KEY is not set, so we "
			"cannot approve payments through the API. Complete it in Wise, or set the key."
		)

	body, res = _call(
		"POST",
		path,
		{"type": "BALANCE"},
		extra_headers={"x-2fa-approval": challenge, "X-Signature": _sign(challenge)},
	)
	if res.status_code >= 400:
		raise WiseError(_message(res, "Wise refused to fund the transfer"))
	return body


def transfer_status(transfer_id: str) -> dict:
	body, _ = _call("GET", f"/v1/transfers/{transfer_id}")
	return {"status": body.get("status", ""), "id": body.get("id", "")}


def describe(currency: str, details: dict) -> str:
	"""A one-line "where it goes" for the admin table and the payout email.

	Deliberately partial. The account number is on file because Wise needs it, not
	so that it can be read off a dashboard — the last four digits are enough to
	tell two accounts apart, which is the only question anybody asks of this line.
	"""
	# An email recipient is shown whole. It is not a bank account number, it is the
	# same kind of thing as the PayPal line next to it, and half of it would be
	# useless to the one person who has to check it before pressing Pay.
	email = str((details or {}).get("email") or "")
	if email:
		return f"Wise: {currency.upper()} → {email}"

	for key in ("iban", "accountNumber", "bankCode", "phoneNumber"):
		value = str((details or {}).get(key) or "")
		if value:
			return f"Wise: {currency.upper()} ····{value[-4:]}"
	return f"Wise: {currency.upper()}"


def redact(details: dict) -> str:
	"""What we keep in the database about the account, as JSON.

	Kept whole: Wise holds the account and we hold the recipient id, but if the
	recipient is ever deleted at Wise's end the only way to recreate it is from
	what the affiliate typed. Losing it means asking them to type it again.
	"""
	return json.dumps(details or {}, separators=(",", ":"))[:2000]
