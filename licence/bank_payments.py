"""Manual bank transfer and wallet payment support.

Stripe remains the card/international path.  This module supplies the pieces
that are specific to a domestic PKR transfer: an official USD/PKR quote, the
public receiving details, and private receipt-image storage.  A receipt is only
supporting evidence; an admin still verifies the incoming transaction before a
licence is issued.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import httpx

from . import store

log = logging.getLogger("licence.bank_payments")

SBP_RATE_URL = os.environ.get(
	"SBP_RATE_URL", "https://www.sbp.org.pk/ecodata/rates/war/WAR-Current.asp"
)
PROOF_DIR = Path(os.environ.get("BANK_PROOF_DIR", "/data/payment-proofs"))
MAX_PROOF_BYTES = int(os.environ.get("BANK_PROOF_MAX_BYTES", str(4 * 1024 * 1024)))
RATE_CACHE_SECONDS = 6 * 60 * 60
RATE_FALLBACK_SECONDS = 7 * 24 * 60 * 60
ORDER_QUOTE_SECONDS = 24 * 60 * 60

# These details are intentionally customer-visible. Environment variables let
# them be replaced without a code release, while the supplied values make the
# first deployment usable immediately.
BANK_ENABLED = os.environ.get("BANK_PAYMENTS_ENABLED", "1") != "0"
BANK_NAME = os.environ.get("BANK_PAYMENT_BANK", "Bank Al-Habib")
BANK_TITLE = os.environ.get("BANK_PAYMENT_TITLE", "Imtiaz Ahmad")
BANK_IBAN = (
	os.environ.get("BANK_PAYMENT_IBAN", "").strip()
	or os.environ.get("BANK_PAYMENT_ACCOUNT", "").strip()
	or "PK76BAHL2030098100511601"
)
BANK_QR_URL = os.environ.get(
	"BANK_PAYMENT_QR_URL", "https://softclipper.pro/payments/bank-al-habib-qr.jpeg"
)
JAZZCASH_TITLE = os.environ.get("JAZZCASH_TITLE", "Imtiaz Ahmad")
JAZZCASH_NUMBER = os.environ.get("JAZZCASH_NUMBER", "03005863032")


class BankPaymentError(RuntimeError):
	"""A customer-safe bank checkout failure."""


def public_details() -> dict:
	"""Receiving details printed after an order and quote have been created."""
	return {
		"enabled": BANK_ENABLED,
		"bank": {
			"name": BANK_NAME,
			"title": BANK_TITLE,
			# Keep ``account`` during the rollout so an older cached checkout page
			# still works while the new page reads the semantically correct key.
			"iban": BANK_IBAN,
			"account": BANK_IBAN,
			"qr_url": BANK_QR_URL,
		},
		"jazzcash": {"title": JAZZCASH_TITLE, "number": JAZZCASH_NUMBER},
	}


def _plain_html(raw: str) -> str:
	# The SBP page is an ordinary HTML page rather than a JSON API. Restrict the
	# parser to visible text and collapse whitespace so cosmetic markup changes do
	# not change the pattern we rely on.
	raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.I | re.S)
	raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
	return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def parse_sbp_rate(raw: str) -> tuple[Decimal, str]:
	"""Extract the latest weighted-average USD/PKR offer rate from SBP HTML."""
	text = _plain_html(raw)
	section = re.search(
		r"USD\s*/\s*PKR\s+Rates(?P<body>.{0,900}?)Cut-off\s+Rates",
		text,
		flags=re.I,
	)
	if not section:
		raise BankPaymentError("The State Bank exchange-rate page has changed format.")
	body = section.group("body")
	date = re.search(r"As\s+on\s+(.{3,40}?)\s+M2M", body, flags=re.I)
	offer = re.search(
		r"Weighted\s+Average\s+Rate.*?BID\s+\d+(?:\.\d+)?\s+Offer\s+(\d+(?:\.\d+)?)",
		body,
		flags=re.I,
	)
	if not offer:
		raise BankPaymentError("The State Bank USD/PKR offer rate could not be read.")
	try:
		rate = Decimal(offer.group(1))
	except InvalidOperation as exc:
		raise BankPaymentError("The State Bank returned an invalid USD/PKR rate.") from exc
	if not Decimal("100") <= rate <= Decimal("1000"):
		raise BankPaymentError("The State Bank USD/PKR rate is outside the safe range.")
	return rate, (date.group(1).strip() if date else "latest SBP working day")


def _fetch_sbp_rate() -> tuple[Decimal, str]:
	try:
		response = httpx.get(SBP_RATE_URL, timeout=12, follow_redirects=True)
		response.raise_for_status()
	except httpx.HTTPError as exc:
		raise BankPaymentError("The State Bank exchange rate is temporarily unavailable.") from exc
	return parse_sbp_rate(response.text)


def current_usd_pkr() -> dict:
	"""Return a fresh SBP offer rate, with a persisted outage fallback.

	An explicit override is an emergency control for a changed SBP page. It is not
	the normal path and is labelled as such in the customer-facing quote.
	"""
	override = os.environ.get("BANK_USD_PKR_RATE", "").strip()
	if override:
		try:
			rate = Decimal(override)
		except InvalidOperation as exc:
			raise BankPaymentError("BANK_USD_PKR_RATE is not a valid number.") from exc
		if not Decimal("100") <= rate <= Decimal("1000"):
			raise BankPaymentError("BANK_USD_PKR_RATE is outside the safe range.")
		return {"rate": rate, "date": "manual server override", "stale": False}

	now = int(time.time())
	cached = store.get_fx_rate("USD/PKR")
	if cached and now - int(cached["fetched_at"]) <= RATE_CACHE_SECONDS:
		return {"rate": Decimal(cached["rate"]), "date": cached["rate_date"], "stale": False}

	try:
		rate, rate_date = _fetch_sbp_rate()
		store.save_fx_rate("USD/PKR", str(rate), rate_date, now)
		return {"rate": rate, "date": rate_date, "stale": False}
	except BankPaymentError:
		if cached and now - int(cached["fetched_at"]) <= RATE_FALLBACK_SECONDS:
			log.warning("SBP rate fetch failed; using cached USD/PKR rate from %s", cached["rate_date"])
			return {
				"rate": Decimal(cached["rate"]),
				"date": cached["rate_date"],
				"stale": True,
			}
		raise


def quote(usd_cents: int) -> dict:
	if usd_cents <= 0:
		raise BankPaymentError("The configured USD price is invalid.")
	fx = current_usd_pkr()
	pkr = (Decimal(usd_cents) / Decimal(100) * fx["rate"]).quantize(
		Decimal("1"), rounding=ROUND_HALF_UP
	)
	return {
		"usd_cents": int(usd_cents),
		"rate": str(fx["rate"]),
		"rate_date": fx["date"],
		"rate_stale": bool(fx["stale"]),
		"pkr_amount": int(pkr),
	}


def save_proof(reference: str, data_url: str) -> tuple[str, str]:
	"""Validate and privately store a receipt image.

	The browser's filename is never used. Both the extension and the actual file
	signature are checked before bytes reach disk, and the generated order
	reference is the complete filename.
	"""
	if not data_url:
		return "", ""
	match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=\r\n]+)", data_url)
	if not match:
		raise BankPaymentError("Upload a JPG, PNG or WebP payment screenshot.")
	try:
		payload = base64.b64decode(match.group(2), validate=True)
	except (binascii.Error, ValueError) as exc:
		raise BankPaymentError("The payment screenshot could not be read.") from exc
	if not payload or len(payload) > MAX_PROOF_BYTES:
		raise BankPaymentError("The payment screenshot must be smaller than 4 MB.")

	kind = match.group(1)
	valid = (
		(kind == "jpeg" and payload.startswith(b"\xff\xd8\xff"))
		or (kind == "png" and payload.startswith(b"\x89PNG\r\n\x1a\n"))
		or (kind == "webp" and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
	)
	if not valid:
		raise BankPaymentError("The uploaded file is not a valid payment screenshot.")

	ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}[kind]
	PROOF_DIR.mkdir(parents=True, exist_ok=True)
	path = PROOF_DIR / f"{reference}.{ext}"
	temporary = PROOF_DIR / f".{reference}.{ext}.tmp"
	temporary.write_bytes(payload)
	os.replace(temporary, path)
	return str(path), hashlib.sha256(payload).hexdigest()


def remove_proof(path: str):
	"""Remove a just-written orphan after a rejected duplicate submission."""
	if not path:
		return
	try:
		candidate = Path(path).resolve()
		root = PROOF_DIR.resolve()
		if root in candidate.parents:
			candidate.unlink(missing_ok=True)
	except OSError:
		log.exception("could not remove orphan payment proof %s", path)
