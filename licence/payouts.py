"""One place that decides how an affiliate's money actually leaves.

There are four ways to pay somebody here and three of them are automatic. Before
this module the admin endpoint knew about one — Stripe — and adding a second
would have meant a second endpoint, a second button and a second copy of the
"which rows, how much, mark them paid" logic. That is the shape of code where
two rails slowly stop agreeing about what has been paid.

So: the rails know how to talk to their provider and nothing else, and this knows
which one to use and what is true of all of them.

    ready_for(affiliate)   can this person be paid automatically at all
    send(...)              do it, and say what happened

**What every rail must guarantee**, and the reason each has an odd-looking
argument:

  · *Sending twice is worse than not sending.* Every provider gets the same
    `reference` — the affiliate's code and the exact range of referral rows being
    settled — turned into whatever that provider calls an idempotency key. A
    retried request after a timeout is therefore the same request, not a second
    payment.
  · *Nothing is marked paid unless money moved.* Every failure here raises, and
    the caller marks rows paid only on the way back. A row wrongly left open is a
    second attempt; a row wrongly closed is an affiliate who is never paid and
    has no way of knowing.
"""
from __future__ import annotations

import logging

from . import paypal_api, stripe_api, wise_api

log = logging.getLogger("licence.payouts")

# Every method an affiliate can choose, automatic or not. `manual` is not a rail
# — it is the absence of one, and it is the fallback that must always exist:
# there is no set of providers that covers everybody, and an affiliate we cannot
# pay is an affiliate we should not have signed up.
METHODS = ("manual", "stripe", "paypal", "wise")
AUTOMATIC = ("stripe", "paypal", "wise")


class PayoutError(RuntimeError):
	"""Something a person can read and act on. Every rail's own error class is
	translated into this so the caller has one thing to catch — and so no
	provider's exception type leaks into the HTTP layer."""


def available() -> dict:
	"""Which automatic rails this deployment can actually use.

	The dashboards ask this and hide what is off. A radio button leading to "not
	switched on" is worse than no radio button: it reads as the good option being
	withheld from this particular person.
	"""
	return {
		"stripe": stripe_api.configured(),
		"paypal": paypal_api.configured(),
		# Funding needs a signing key on top of the token. Reported separately
		# because a deployment with one and not the other can create transfers it
		# cannot complete, and that is worth knowing before payday.
		"wise": wise_api.configured(),
		"wise_can_fund": wise_api.can_fund(),
		# Whether the Wise rail accepts only other Wise accounts. Published so the
		# affiliate's page asks for the right thing — an email address rather than
		# a form of bank fields it would then be refused for filling in.
		"wise_only": wise_api.WISE_ONLY,
	}


def ready_for(affiliate: dict) -> bool:
	"""Whether pressing Pay on this affiliate would work.

	Each rail has its own idea of ready. Stripe's is Stripe's verdict, which we
	are told by webhook. The other two are simply whether we have somewhere to
	send it — PayPal accepts any email address, and a Wise recipient only exists
	because Wise already validated the account when it was created.
	"""
	method = affiliate.get("payout_method")
	if method not in AUTOMATIC or not available().get(method):
		return False
	if method == "stripe":
		return bool(affiliate.get("stripe_account")) and bool(affiliate.get("stripe_ready"))
	if method == "paypal":
		return bool(affiliate.get("paypal_email"))
	return bool(affiliate.get("wise_recipient"))


def reference(code: str, ids: list[int], amount: int) -> str:
	"""The name of one specific payout, derived from what is in it.

	Not a random token and not a timestamp: it has to come out the same when the
	same payment is attempted again after a timeout, and different when a later
	payout covers different rows. Every rail turns this into its own kind of key.
	"""
	return f"sc-payout-{code}-{min(ids)}-{max(ids)}-{amount}"


def send(affiliate: dict, amount: int, currency: str, ids: list[int]) -> dict:
	"""Pay one affiliate everything in `ids`, by whichever rail they chose.

	Returns what to write against the rows: `how` (which rail) and `id` (the
	provider's own reference, so a payment can be found again at their end).
	Raises `PayoutError` and moves nothing if anything at all goes wrong.
	"""
	code = affiliate["code"]
	method = affiliate.get("payout_method")
	if method not in AUTOMATIC:
		raise PayoutError("This affiliate is paid by hand — send the money, then mark it paid.")
	if not available().get(method):
		raise PayoutError(f"{method.title()} payouts are not switched on for this server.")
	if not ready_for(affiliate):
		raise PayoutError(f"{code} has no working {method} destination on file yet.")
	if amount <= 0:
		raise PayoutError("Nothing to pay")

	ref = reference(code, ids, amount)
	note = f"Soft Clipper commission — {len(ids)} sale(s)"

	try:
		if method == "stripe":
			transfer = stripe_api.create_transfer(
				affiliate["stripe_account"], amount, currency, idempotency_key=ref, description=note
			)
			return {"how": "stripe", "id": transfer.get("id", "")}

		if method == "paypal":
			batch = paypal_api.create_payout(
				ref, affiliate["paypal_email"], amount, currency, note=note
			)
			# PayPal accepting the batch is not the same as the affiliate having
			# the money — an address with no PayPal account behind it sits
			# unclaimed for thirty days. The money has left us either way, which is
			# what marking the row paid records.
			return {"how": "paypal", "id": batch.get("id", "")}

		# Wise. The quote has to be made now rather than reused: a quote is a
		# price with an expiry, and paying against a stale one is how a transfer
		# fails at the last step after everything else succeeded.
		if currency.upper() != wise_api.SOURCE_CURRENCY.upper():
			raise PayoutError(
				f"This commission is in {currency.upper()} but our Wise balance is "
				f"{wise_api.SOURCE_CURRENCY.upper()}. Pay this one by hand."
			)
		quote = wise_api.quote(affiliate["wise_currency"] or currency, amount)
		transfer = wise_api.create_transfer(affiliate["wise_recipient"], quote.get("id", ""), ref)
		wise_api.fund_transfer(transfer.get("id", ""))
		return {"how": "wise", "id": str(transfer.get("id", ""))}

	except (stripe_api.StripeError, paypal_api.PayPalError, wise_api.WiseError) as exc:
		# The provider's own wording, unedited. "Insufficient funds", "country not
		# supported", "IBAN checksum failed" — each is exactly what the person
		# reading it needs to do something about, and rewording it would hide it.
		raise PayoutError(str(exc)) from exc
