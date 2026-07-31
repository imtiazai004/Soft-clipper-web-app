"""Tests for the licence server, run against the real HTTP stack.

The rules these check are the ones that cost money when they are wrong: a key
must not work on two machines, a revoked key must stop working, a Stripe retry
must not issue two licences, and an offline token must not be forgeable.

    python -m pytest licence/test_licence.py -q
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time

import pathlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

# Configure before importing the app — module-level env reads happen at import.
_tmp = tempfile.mkdtemp()
os.environ["LICENCE_DB"] = os.path.join(_tmp, "test.db")
os.environ["BANK_PROOF_DIR"] = os.path.join(_tmp, "payment-proofs")
_priv = Ed25519PrivateKey.generate()
os.environ["LICENCE_PRIVATE_KEY"] = base64.b64encode(
	_priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
).decode()
os.environ["LICENCE_ADMIN_TOKEN"] = "test-admin-token"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
# The affiliate portal runs over plain HTTP here, and its links have to resolve
# back to the test server: a Secure cookie is never sent over http, and an
# absolute redirect to the real host would send TestClient to the internet.
os.environ["AFFILIATE_COOKIE_SECURE"] = "0"
os.environ["AFFILIATE_PORTAL_URL"] = "http://testserver/partner"

from licence import app as licence_app  # noqa: E402
from licence import bank_payments, crypto, mail, store  # noqa: E402

client = TestClient(licence_app.app)
ADMIN = {"x-admin-token": "test-admin-token"}
PC_A = "fingerprint-machine-aaaaaaaa"
PC_B = "fingerprint-machine-bbbbbbbb"


@pytest.fixture(autouse=True)
def _clear_throttle():
	licence_app._hits.clear()
	yield


def make_licence(email="buyer@example.com") -> str:
	r = client.post(
		"/api/admin/licences", json={"email": email, "send_email": False}, headers=ADMIN
	)
	assert r.status_code == 200, r.text
	return r.json()["key"]


# ── key format ───────────────────────────────────────────────────────────────


def test_keys_are_unique_and_normalise_from_messy_input():
	keys = {crypto.new_key() for _ in range(500)}
	assert len(keys) == 500

	key = crypto.new_key()
	assert crypto.normalise(key.lower()) == key
	assert crypto.normalise(key.replace("-", " ")) == key
	assert crypto.normalise(key.removeprefix("SC-")) == key
	assert crypto.normalise("nonsense") == ""


# ── activation ───────────────────────────────────────────────────────────────


def test_activation_binds_to_one_machine_and_returns_a_valid_token():
	key = make_licence()
	r = client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	assert r.status_code == 200

	payload = crypto.verify_token(r.json()["token"], crypto.public_key_b64())
	assert payload and payload["key"] == key
	assert payload["exp"] > time.time()


def test_token_carries_the_fingerprint_the_client_sent():
	"""The whole activation rests on this one equality.

	The desktop app decides it is activated by comparing the token's `machine`
	claim against its own fingerprint. If the server puts anything else in there
	the comparison can never succeed, the app says "this activation belongs to a
	different computer", and every paying customer is locked out — while the
	server logs a cheerful 200 for every attempt.

	That shipped. It was caught by installing the built app and typing a real key
	into it, not here: the test above checks the token verifies and carries the
	right key, and never looked at `machine` at all.
	"""
	key = make_licence()
	r = client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	payload = crypto.verify_token(r.json()["token"], crypto.public_key_b64())
	assert payload["machine"] == PC_A

	# Refresh has to agree, or the app deactivates itself a month after it was
	# bought — the worst possible moment to find out.
	v = client.post("/api/licence/validate", json={"key": key, "fingerprint": PC_A})
	assert v.status_code == 200
	assert crypto.verify_token(v.json()["token"], crypto.public_key_b64())["machine"] == PC_A


def test_the_database_stores_a_hash_not_the_raw_fingerprint():
	"""Why the server hashes at all: a dump of this table must not hand anyone a
	working machine identity. That property has to survive the fix above — the
	token gets the raw value, storage does not."""
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})

	stored = store.get(key)["machine"]
	assert stored != PC_A
	assert stored == hashlib.sha256(f"sc:{PC_A}".encode()).hexdigest()


def test_same_machine_can_reactivate():
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	again = client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	assert again.status_code == 200


def test_second_machine_is_refused():
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	r = client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_B})
	assert r.status_code == 409
	assert "another computer" in r.json()["error"]


def test_unknown_and_malformed_keys_are_refused():
	assert client.post("/api/licence/activate", json={"key": "SC-AAAAA-AAAAA-AAAAA-AAAAA", "fingerprint": PC_A}).status_code == 404
	assert client.post("/api/licence/activate", json={"key": "hello", "fingerprint": PC_A}).status_code == 400
	assert client.post("/api/licence/activate", json={"key": make_licence(), "fingerprint": ""}).status_code == 400


# ── moving machine ───────────────────────────────────────────────────────────


def test_release_then_activate_elsewhere():
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	assert client.post("/api/licence/release", json={"key": key, "fingerprint": PC_A}).status_code == 200
	assert client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_B}).status_code == 200


def test_a_stranger_cannot_release_someone_elses_licence():
	"""A leaked key must not let anyone kick the real owner off their own PC."""
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	r = client.post("/api/licence/release", json={"key": key, "fingerprint": PC_B})
	assert r.status_code == 403


def test_support_can_release_a_dead_machine():
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	assert client.post(f"/api/admin/licences/{key}/release", headers=ADMIN).status_code == 200
	assert client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_B}).status_code == 200


def test_endless_machine_hopping_is_capped():
	key = make_licence()
	for _ in range(licence_app.MAX_RELEASES):
		store.release(key)
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	r = client.post("/api/licence/release", json={"key": key, "fingerprint": PC_A})
	assert r.status_code == 429


# ── validate and revoke ──────────────────────────────────────────────────────


def test_validate_refreshes_on_the_bound_machine_only():
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	assert client.post("/api/licence/validate", json={"key": key, "fingerprint": PC_A}).status_code == 200
	assert client.post("/api/licence/validate", json={"key": key, "fingerprint": PC_B}).status_code == 409


def test_revoked_licence_stops_working():
	key = make_licence()
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})
	client.post(f"/api/admin/licences/{key}/revoke", json={"reason": "refund"}, headers=ADMIN)

	assert client.post("/api/licence/validate", json={"key": key, "fingerprint": PC_A}).status_code == 403
	assert client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A}).status_code == 403


# ── tokens ───────────────────────────────────────────────────────────────────


def test_a_tampered_token_does_not_verify():
	key = make_licence()
	token = client.post(
		"/api/licence/activate", json={"key": key, "fingerprint": PC_A}
	).json()["token"]
	pub = crypto.public_key_b64()

	body, sig = token.split(".", 1)
	payload = json.loads(base64.urlsafe_b64decode(body))
	payload["exp"] += 10 * 365 * 86400  # try to grant yourself a decade offline
	forged = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()

	assert crypto.verify_token(f"{forged}.{sig}", pub) is None
	assert crypto.verify_token(token, pub) is not None


def test_a_token_from_a_different_keypair_does_not_verify():
	other = Ed25519PrivateKey.generate()
	other_pub = base64.b64encode(
		other.public_key().public_bytes(
			encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
		)
	).decode()
	key = make_licence()
	token = client.post(
		"/api/licence/activate", json={"key": key, "fingerprint": PC_A}
	).json()["token"]
	assert crypto.verify_token(token, other_pub) is None


# ── admin surface ────────────────────────────────────────────────────────────


def test_admin_needs_the_token():
	assert client.get("/api/admin/licences").status_code == 401
	assert client.get("/api/admin/licences", headers={"x-admin-token": "wrong"}).status_code == 401
	assert client.get("/api/admin/licences", headers=ADMIN).status_code == 200


# ── Stripe ───────────────────────────────────────────────────────────────────


def _stripe_post(event: dict, secret="whsec_test", ts: int | None = None):
	payload = json.dumps(event).encode()
	ts = ts or int(time.time())
	sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
	return client.post(
		"/webhooks/stripe",
		content=payload,
		headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"},
	)


def _session(session_id: str, email: str) -> dict:
	return {
		"type": "checkout.session.completed",
		"data": {
			"object": {
				"id": session_id,
				"customer_details": {"email": email},
				"payment_status": "paid",
			}
		},
	}


def test_a_paid_checkout_creates_a_licence():
	r = _stripe_post(_session("cs_test_1", "new@example.com"))
	assert r.status_code == 200
	key = r.json()["key"]
	assert store.get(key)["email"] == "new@example.com"


def test_stripe_retry_does_not_issue_a_second_licence():
	first = _stripe_post(_session("cs_test_2", "retry@example.com")).json()
	second = _stripe_post(_session("cs_test_2", "retry@example.com")).json()
	assert second["key"] == first["key"]
	assert second["duplicate"] is True


def test_a_delayed_stripe_session_waits_until_payment_succeeds():
	event = _session("cs_delayed_1", "delayed@example.com")
	event["data"]["object"]["payment_status"] = "unpaid"
	first = _stripe_post(event)
	assert first.status_code == 200
	assert first.json()["pending"] is True
	assert store.find_by_source("cs_delayed_1") is None

	event["type"] = "checkout.session.async_payment_succeeded"
	event["data"]["object"]["payment_status"] = "paid"
	paid = _stripe_post(event)
	assert paid.status_code == 200
	assert store.get(paid.json()["key"])["email"] == "delayed@example.com"


def test_a_forged_or_stale_webhook_is_rejected():
	assert _stripe_post(_session("cs_test_3", "x@example.com"), secret="wrong").status_code == 400
	stale = int(time.time()) - 3600
	assert _stripe_post(_session("cs_test_4", "x@example.com"), ts=stale).status_code == 400


def test_a_refund_revokes_the_licence():
	key = _stripe_post(_session("cs_test_5", "refund@example.com")).json()["key"]
	client.post("/api/licence/activate", json={"key": key, "fingerprint": PC_A})

	_stripe_post(
		{
			"type": "charge.refunded",
			"data": {"object": {"billing_details": {"email": "refund@example.com"}}},
		}
	)
	assert store.get(key)["status"] == "revoked"
	assert client.post("/api/licence/validate", json={"key": key, "fingerprint": PC_A}).status_code == 403


# ── Bank transfer / wallet checkout ─────────────────────────────────────────


def _bank_order(email: str = "pk-buyer@example.com", method: str = "bank", ref: str = "") -> dict:
	# A fresh persisted rate keeps tests deterministic and exercises the same
	# outage/cache path production uses without calling the State Bank website.
	store.save_fx_rate("USD/PKR", "280.00", "31-Jul-2026", int(time.time()))
	r = client.post(
		"/api/checkout/bank/orders",
		json={"email": email, "method": method, "ref": ref},
	)
	assert r.status_code == 200, r.text
	return r.json()


def _submit_bank(order_data: dict, transaction: str, proof: str = ""):
	return client.post(
		f"/api/checkout/bank/orders/{order_data['order']['reference']}/submit",
		json={
			"token": order_data["token"],
			"transaction_id": transaction,
			"proof_data": proof,
		},
	)


def test_state_bank_rate_parser_reads_the_weighted_average_offer():
	raw = """
		<section><h4>USD/ PKR Rates</h4><p>As on 30-Jul - 2026</p>
		<p>M2M Revaluation Rate</p><strong>277.8118</strong>
		<h5>Weighted Average Rate</h5><span>BID</span><b>277.5321</b>
		<span>Offer</span><b>277.9572</b><h4>Cut-off Rates in the Latest Auctions</h4></section>
	"""
	rate, date = bank_payments.parse_sbp_rate(raw)
	assert str(rate) == "277.9572"
	assert date == "30-Jul - 2026"


def test_bank_order_uses_the_current_configured_usd_price_and_locked_pkr_quote():
	order = _bank_order(method="jazzcash")
	assert order["order"]["usd_cents"] == 3900
	assert order["order"]["pkr_amount"] == 10920
	assert order["order"]["fx_rate"] == "280.00"
	assert order["order"]["method"] == "jazzcash"
	assert order["payment_details"]["bank"]["name"] == "Bank Al-Habib"
	assert order["payment_details"]["bank"]["iban"] == "PK76BAHL2030098100511601"
	assert order["payment_details"]["bank"]["account"] == "PK76BAHL2030098100511601"


def test_submitted_bank_payment_only_issues_a_licence_after_admin_approval(monkeypatch):
	sent = []
	monkeypatch.setattr(mail, "send_bank_payment_submitted", lambda *args: True)
	monkeypatch.setattr(mail, "notify_bank_payment", lambda *args: True)
	monkeypatch.setattr(mail, "send_licence", lambda email, key: sent.append((email, key)) or True)

	created = _bank_order("approve-me@example.com")
	reference = created["order"]["reference"]
	submitted = _submit_bank(created, "BAH-APPROVE-1001")
	assert submitted.status_code == 200, submitted.text
	assert store.bank_order(reference)["status"] == "submitted"
	assert store.find_by_source(f"bank:{reference}") is None

	approved = client.post(f"/api/admin/bank-orders/{reference}/approve", headers=ADMIN)
	assert approved.status_code == 200, approved.text
	order = store.bank_order(reference)
	licence = store.find_by_source(f"bank:{reference}")
	assert order["status"] == "paid"
	assert licence["key"] == order["licence_key"]
	assert sent == [("approve-me@example.com", licence["key"])]

	# Retrying the admin request returns the original result and sends nothing.
	second = client.post(f"/api/admin/bank-orders/{reference}/approve", headers=ADMIN)
	assert second.status_code == 200
	assert second.json()["duplicate"] is True
	assert sent == [("approve-me@example.com", licence["key"])]


def test_transaction_id_cannot_be_reused_for_a_second_bank_order(monkeypatch):
	monkeypatch.setattr(mail, "send_bank_payment_submitted", lambda *args: True)
	monkeypatch.setattr(mail, "notify_bank_payment", lambda *args: True)
	first = _bank_order("first-bank@example.com")
	second = _bank_order("second-bank@example.com")
	assert _submit_bank(first, "ONE-REAL-TRANSACTION").status_code == 200
	duplicate = _submit_bank(second, "ONE-REAL-TRANSACTION")
	assert duplicate.status_code == 409
	assert "already been submitted" in duplicate.json()["error"]


def test_payment_proof_is_private_and_requires_admin_token(monkeypatch):
	monkeypatch.setattr(mail, "send_bank_payment_submitted", lambda *args: True)
	monkeypatch.setattr(mail, "notify_bank_payment", lambda *args: True)
	created = _bank_order("proof@example.com")
	png = base64.b64encode(b"\x89PNG\r\n\x1a\nprivate-proof").decode()
	assert _submit_bank(created, "PROOF-TRANSACTION-1", f"data:image/png;base64,{png}").status_code == 200
	reference = created["order"]["reference"]
	path = f"/api/admin/bank-orders/{reference}/proof"
	assert client.get(path).status_code == 401
	proof = client.get(path, headers=ADMIN)
	assert proof.status_code == 200
	assert proof.content.startswith(b"\x89PNG")


def test_approved_bank_sale_credits_affiliate_at_the_same_usd_price(monkeypatch):
	monkeypatch.setattr(mail, "send_bank_payment_submitted", lambda *args: True)
	monkeypatch.setattr(mail, "notify_bank_payment", lambda *args: True)
	monkeypatch.setattr(mail, "send_licence", lambda *args: True)
	_affiliate("pkpartner", rate=30)
	created = _bank_order("pk-customer@example.com", ref="pkpartner")
	reference = created["order"]["reference"]
	assert _submit_bank(created, "PK-AFFILIATE-1001").status_code == 200
	approved = client.post(f"/api/admin/bank-orders/{reference}/approve", headers=ADMIN)
	assert approved.status_code == 200, approved.text
	referral = store.referral_for_licence(approved.json()["order"]["licence_key"])
	assert referral["currency"] == "usd"
	assert referral["gross"] == 3900
	assert referral["commission"] == 1170


# ── affiliates ───────────────────────────────────────────────────────────────
#
# This is the part of the service that decides how much money leaves the
# business, so what is checked here is the arithmetic and the guards around it:
# a commission must be counted once, must come off the amount actually charged,
# must not be payable while a refund could still cancel it, and must not survive
# that refund.


def _sale(session_id: str, email: str, ref: str = "", total: int = 3900) -> dict:
	return {
		"type": "checkout.session.completed",
		"data": {
			"object": {
				"id": session_id,
				"customer_details": {"email": email},
				"client_reference_id": ref,
				"amount_total": total,
				"currency": "usd",
				"payment_status": "paid",
			}
		},
	}


def _affiliate(code: str, rate: int = 30, **extra) -> dict:
	body = {"code": code, "name": code.title(), "email": f"{code}@example.com", "rate_pct": rate}
	r = client.post("/api/admin/affiliates", json={**body, **extra}, headers=ADMIN)
	assert r.status_code == 200, r.text
	return r.json()["affiliate"]


def test_a_referred_sale_credits_the_affiliate():
	_affiliate("ali")
	key = _stripe_post(_sale("cs_aff_1", "buyer1@example.com", ref="ali")).json()["key"]

	assert store.get(key)["ref"] == "ali"
	row = store.referral_for_licence(key)
	assert row["commission"] == 1170  # 30% of $39.00, in cents
	assert row["status"] == "pending"


def test_commission_follows_what_was_actually_charged_not_the_list_price():
	"""A discount code, a currency conversion or a price change must not leave us
	paying a percentage of a number nobody was charged."""
	_affiliate("sara", rate=25)
	key = _stripe_post(_sale("cs_aff_2", "buyer2@example.com", ref="sara", total=2000)).json()["key"]
	assert store.referral_for_licence(key)["commission"] == 500


def test_a_stripe_retry_does_not_pay_the_commission_twice():
	_affiliate("dupe")
	_stripe_post(_sale("cs_aff_3", "buyer3@example.com", ref="dupe"))
	_stripe_post(_sale("cs_aff_3", "buyer3@example.com", ref="dupe"))

	rows = store.referrals("dupe")
	assert len(rows) == 1


def test_commission_is_not_payable_until_the_refund_window_closes():
	_affiliate("held")
	_stripe_post(_sale("cs_aff_4", "buyer4@example.com", ref="held"))

	assert store.payable("held") == []
	summary = {a["code"]: a for a in store.affiliate_summary()}["held"]
	assert summary["holding"] == 1170 and summary["due"] == 0


def test_a_refund_cancels_the_commission():
	_affiliate("clawed")
	_stripe_post(_sale("cs_aff_5", "refundme@example.com", ref="clawed"))

	_stripe_post({
		"type": "charge.refunded",
		"data": {"object": {"billing_details": {"email": "refundme@example.com"}}},
	})

	rows = store.referrals("clawed")
	assert rows[0]["status"] == "void"
	summary = {a["code"]: a for a in store.affiliate_summary()}["clawed"]
	assert summary["due"] == 0 and summary["holding"] == 0


def test_commission_already_paid_survives_a_later_refund():
	"""Money that has left the building is not recovered by an UPDATE. The row
	stays paid so the books match reality and the conversation can happen."""
	_affiliate("paidout")
	key = _stripe_post(_sale("cs_aff_6", "late@example.com", ref="paidout")).json()["key"]
	row = store.referral_for_licence(key)
	store.mark_referrals_paid([row["id"]], how="manual")

	_stripe_post({
		"type": "charge.refunded",
		"data": {"object": {"billing_details": {"email": "late@example.com"}}},
	})

	assert store.referral_for_licence(key)["status"] == "paid"
	assert store.get(key)["status"] == "revoked"


def test_a_row_cannot_be_marked_paid_twice():
	_affiliate("once")
	# The buyer is deliberately not once@example.com. `_affiliate` gives every
	# affiliate the address `<code>@example.com`, so buying under that address is
	# a self-referral and earns nothing — which is correct, and is not what this
	# test is about.
	key = _stripe_post(_sale("cs_aff_7", "buyer-once@example.com", ref="once")).json()["key"]
	rid = store.referral_for_licence(key)["id"]

	assert store.mark_referrals_paid([rid]) == 1
	assert store.mark_referrals_paid([rid]) == 0


def test_an_unknown_or_disabled_code_still_sells_but_pays_nobody():
	"""The customer's licence must never depend on the affiliate bookkeeping."""
	ghost = _stripe_post(_sale("cs_aff_8", "ghost@example.com", ref="nosuchcode")).json()
	assert store.get(ghost["key"])["email"] == "ghost@example.com"
	assert store.referral_for_licence(ghost["key"]) is None

	_affiliate("gone")
	store.set_affiliate_status("gone", "disabled")
	off = _stripe_post(_sale("cs_aff_9", "off@example.com", ref="gone")).json()
	assert store.referral_for_licence(off["key"]) is None


def test_a_sale_with_no_referral_records_nothing():
	key = _stripe_post(_sale("cs_aff_10", "plain@example.com")).json()["key"]
	assert store.get(key)["ref"] is None
	assert store.referral_for_licence(key) is None


def test_codes_are_normalised_so_a_link_survives_being_retyped():
	_affiliate("Mixed-Case")
	key = _stripe_post(_sale("cs_aff_11", "typed@example.com", ref="  MIXED-CASE ")).json()["key"]
	assert store.referral_for_licence(key)["code"] == "mixed-case"


def test_changing_a_rate_does_not_rewrite_what_was_already_earned():
	_affiliate("raised", rate=20)
	first = _stripe_post(_sale("cs_aff_12", "a@example.com", ref="raised")).json()["key"]
	assert store.referral_for_licence(first)["commission"] == 780

	with store.db() as conn:
		conn.execute("UPDATE affiliates SET rate_pct = 50 WHERE code = 'raised'")

	second = _stripe_post(_sale("cs_aff_13", "b@example.com", ref="raised")).json()["key"]
	assert store.referral_for_licence(second)["commission"] == 1950
	assert store.referral_for_licence(first)["commission"] == 780


def test_affiliate_endpoints_need_the_admin_token():
	for path in ["/api/admin/affiliates", "/api/admin/referrals"]:
		assert client.get(path).status_code == 401, path
	for path in [
		"/api/admin/affiliates",
		"/api/admin/referrals/paid",
		"/api/admin/affiliates/x/pay",
		"/api/admin/affiliates/x/stripe",
		"/api/admin/affiliates/x/status",
		"/api/admin/affiliates/x/refresh",
	]:
		assert client.post(path, json={}).status_code == 401, path


def test_a_duplicate_code_is_refused():
	_affiliate("taken")
	r = client.post(
		"/api/admin/affiliates",
		json={"code": "taken", "name": "Someone Else", "email": "e@example.com"},
		headers=ADMIN,
	)
	assert r.status_code == 409


def test_automatic_payout_refuses_when_stripe_is_not_set_up():
	"""Nothing half-works: with no secret key the button is not offered, and the
	endpoint behind it says so rather than failing somewhere inside Stripe."""
	_affiliate("byhand", payout_method="manual", payout_to="Wise, PK")
	r = client.post("/api/admin/affiliates/byhand/pay", headers=ADMIN)
	assert r.status_code == 400
	assert "by hand" in r.json()["error"]


# ── site settings ────────────────────────────────────────────────────────────
#
# These are the switches an owner can throw without a developer, so the tests
# are about the ones that would cost money or take the shop down: a price that
# cannot be a price, a "discount" that is not one, and a holding period shorter
# than the refund window.


@pytest.fixture(autouse=True)
def _reset_settings():
	yield
	from licence import settings as settings_mod
	with store.db() as conn:
		conn.execute("DELETE FROM settings")
	settings_mod._cache = None


def _save(patch: dict):
	return client.post("/api/admin/settings", json=patch, headers=ADMIN)


def test_settings_start_at_the_defaults_and_are_public():
	r = client.get("/api/site-config")
	assert r.status_code == 200
	body = r.json()
	assert body["price"]["amount"] == 39
	assert body["downloads"]["enabled"] is True


def test_a_saved_price_reaches_the_public_config():
	assert _save({"price": {"amount": 29}}).status_code == 200
	assert client.get("/api/site-config").json()["price"]["amount"] == 29


def test_a_was_price_at_or_below_the_real_price_is_refused():
	"""Not a discount — a claim that is either meaningless or false, and
	price-marking rules treat it as the second one."""
	r = _save({"price": {"amount": 39, "listAmount": 39}})
	assert r.status_code == 400 and "higher" in r.json()["error"]

	r = _save({"price": {"amount": 39, "listAmount": 20}})
	assert r.status_code == 400

	assert _save({"price": {"amount": 39, "listAmount": 0}}).status_code == 200


def test_an_impossible_price_is_refused():
	for bad in (0, -5, "free", 99999):
		assert _save({"price": {"amount": bad}}).status_code == 400, bad


def test_the_checkout_link_must_be_a_stripe_payment_link():
	assert _save({"price": {"checkoutUrl": "https://evil.example.com/pay"}}).status_code == 400
	assert _save({"price": {"checkoutUrl": "https://buy.stripe.com/abc"}}).status_code == 200
	assert _save({"price": {"checkoutUrl": ""}}).status_code == 200


def test_holding_commission_for_less_than_the_refund_window_is_refused():
	"""14 days is the refund policy. Paying sooner means paying out on sales that
	can still come back."""
	r = _save({"affiliates": {"holdDays": 7}})
	assert r.status_code == 400 and "refund" in r.json()["error"]
	assert _save({"affiliates": {"holdDays": 14}}).status_code == 200


def test_an_out_of_range_commission_is_refused():
	assert _save({"affiliates": {"ratePct": 150}}).status_code == 400
	assert _save({"affiliates": {"ratePct": -1}}).status_code == 400
	assert _save({"affiliates": {"ratePct": 0}}).status_code == 200


def test_a_notice_with_no_text_is_refused():
	assert _save({"notice": {"enabled": True, "text": "  "}}).status_code == 400
	assert _save({"notice": {"enabled": True, "text": "Back Monday"}}).status_code == 200


def test_closing_the_programme_stops_new_commission_but_keeps_the_old():
	_affiliate("closing")
	key = _stripe_post(_sale("cs_set_1", "before@example.com", ref="closing")).json()["key"]
	assert store.referral_for_licence(key) is not None

	assert _save({"affiliates": {"enabled": False}}).status_code == 200

	after = _stripe_post(_sale("cs_set_2", "after@example.com", ref="closing")).json()["key"]
	assert store.referral_for_licence(after) is None, "a closed programme must not credit"
	assert store.referral_for_licence(key) is not None, "existing commission must survive"


def test_a_new_setting_does_not_come_back_missing_for_old_saves():
	"""Settings are merged over the defaults, so a value added in a later version
	fills in rather than leaving the site building with a hole in it."""
	from licence import settings as settings_mod
	with store.db() as conn:
		conn.execute(
			"INSERT INTO settings (key, value, updated_at) VALUES ('site', ?, 0)",
			(json.dumps({"price": {"amount": 25}}),),
		)
	settings_mod._cache = None
	body = client.get("/api/site-config").json()
	assert body["price"]["amount"] == 25
	assert body["downloads"]["installerUrl"].startswith("https://")
	assert body["affiliates"]["ratePct"] == 30


def test_nothing_is_announced_until_a_version_is_typed():
	"""The safe starting state. An empty version means installed apps are told
	nothing at all — which is what should happen before anyone has released."""
	version = client.get("/api/site-config").json()["version"]
	assert version["latest"] == ""
	assert version["winUrl"].startswith("https://")


def test_a_saved_version_reaches_the_app():
	assert _save({"version": {"latest": "1.4.0", "notes": "Faster renders"}}).status_code == 200
	version = client.get("/api/site-config").json()["version"]
	assert version["latest"] == "1.4.0"
	assert version["notes"] == "Faster renders"
	assert version["winUrl"].endswith("Soft-Clipper-Setup.exe")


def test_something_that_is_not_a_version_number_is_refused():
	"""The app pulls the digits out and compares them as numbers. Text with no
	digits compares as (0,), which would quietly mean "no update" forever — a
	silence nobody would think to look for."""
	for bad in ("latest", "v1.4-beta", "next release", "1.4.0 (final)"):
		r = _save({"version": {"latest": bad}})
		assert r.status_code == 400, bad
		assert "version number" in r.json()["error"]

	for good in ("1.4.0", "v1.4.0", "2", "1.10.2.1"):
		assert _save({"version": {"latest": good}}).status_code == 200, good


def test_release_notes_have_a_limit():
	assert _save({"version": {"notes": "x" * 1501}}).status_code == 400
	assert _save({"version": {"notes": "x" * 1500}}).status_code == 200


def test_announcing_can_be_switched_off_without_unpublishing():
	"""For when a build turns out to be bad: stop nudging people towards it
	without losing the version number itself."""
	assert _save({"version": {"latest": "1.4.0", "announce": False}}).status_code == 200
	assert client.get("/api/site-config").json()["version"]["latest"] == ""

	from licence import settings as settings_mod
	assert settings_mod.get(fresh=True)["version"]["latest"] == "1.4.0", "still stored"

	assert _save({"version": {"announce": True}}).status_code == 200
	assert client.get("/api/site-config").json()["version"]["latest"] == "1.4.0"


def test_switching_downloads_off_silences_the_update_too():
	"""The page says "paused while we ship an update". It would be strange for the
	app to be queueing people up to fetch it in the same breath."""
	assert _save({"version": {"latest": "1.4.0"}, "downloads": {"enabled": False}}).status_code == 200
	assert client.get("/api/site-config").json()["version"]["latest"] == ""


def test_the_mac_link_is_empty_until_the_mac_build_is_published():
	"""So a Mac that checks for updates before there is a Mac build is not sent to
	a file that is not there."""
	assert _save({"downloads": {"macEnabled": False}}).status_code == 200
	assert client.get("/api/site-config").json()["version"]["macUrl"] == ""

	assert _save({"downloads": {"macEnabled": True}}).status_code == 200
	assert client.get("/api/site-config").json()["version"]["macUrl"].endswith(".dmg")


def test_announcing_a_version_warns_about_uploading_it():
	"""The one setting here that reaches customers' machines unprompted."""
	warnings = _save({"version": {"latest": "1.4.0"}}).json()["warnings"]
	assert any("1.4.0" in w and "R2" in w for w in warnings)


def test_settings_endpoints_need_the_admin_token():
	assert client.get("/api/admin/settings").status_code == 401
	assert client.post("/api/admin/settings", json={}).status_code == 401
	assert client.post("/api/admin/publish").status_code == 401
	# The public one is public on purpose — it is about to be printed into HTML.
	assert client.get("/api/site-config").status_code == 200


def test_publish_says_so_when_it_is_not_wired_up():
	r = client.post("/api/admin/publish", headers=ADMIN)
	assert r.status_code == 503 and "saved either way" in r.json()["error"]


def test_the_admin_view_warns_when_the_checkout_is_a_test_link():
	_save({"price": {"checkoutUrl": "https://buy.stripe.com/test_abc"}})
	warnings = " ".join(client.get("/api/admin/settings", headers=ADMIN).json()["warnings"])
	assert "TEST" in warnings

	_save({"price": {"checkoutUrl": ""}})
	warnings = " ".join(client.get("/api/admin/settings", headers=ADMIN).json()["warnings"])
	assert "nobody can buy" in warnings


def test_the_mac_download_cannot_be_published_without_a_link():
	"""Turning Mac on publishes the download button, the install guide and the
	sitemap entry together. Doing that with no URL ships a page advertising a
	platform whose download 404s."""
	r = _save({"downloads": {"macEnabled": True, "macUrl": ""}})
	assert r.status_code == 400 and "Mac" in r.json()["error"]

	assert _save({
		"downloads": {"macEnabled": True, "macUrl": "https://dl.softclipper.pro/Soft-Clipper.dmg"}
	}).status_code == 200
	assert client.get("/api/site-config").json()["downloads"]["macEnabled"] is True


# ── affiliates signing themselves up ─────────────────────────────────────────
#
# The open sign-up form is the only part of this service a stranger can drive,
# so what is checked here is what stops it being abused: nothing earns before an
# email address has been proved, one address gets one account, a form cannot
# choose its own commission rate, and none of these endpoints will say whether a
# given person is an affiliate.


@pytest.fixture
def mailbox(monkeypatch):
	"""Every message the service sends, captured instead of posted.

	Patched at `_send` rather than at each helper: that is the single point every
	email goes through, so a message added later is caught by this without the
	fixture having to know about it.
	"""
	sent: list[dict] = []

	def fake(to, subject, body, what="email"):
		sent.append({"to": to, "subject": subject, "body": body, "what": what})
		return True

	monkeypatch.setattr(mail, "_send", fake)
	return sent


@pytest.fixture(autouse=True)
def _affiliate_isolation():
	"""Two things this module shares that a test can leave behind.

	The settings row, so a test that closes sign-ups does not close them for
	everything that runs after it. And the client's cookie jar: `TestClient` is
	created once for the module and keeps cookies, so an affiliate who signed in
	during one test is still signed in during the next — which quietly turns
	"this needs a session" into a test that cannot fail.
	"""
	from licence import settings as settings_mod

	client.cookies.clear()
	with store.db() as conn:
		row = conn.execute("SELECT value FROM settings WHERE key = 'site'").fetchone()
	before = row["value"] if row else None

	yield

	client.cookies.clear()
	# Put the row back exactly as it was, including putting *no* row back when
	# there was none. Saving a tidied-up copy instead would leave a settings row
	# behind for every test in the file, and one of them below asserts what
	# happens when there is not one.
	with store.db() as conn:
		if before is None:
			conn.execute("DELETE FROM settings WHERE key = 'site'")
		else:
			conn.execute(
				"INSERT INTO settings (key, value, updated_at) VALUES ('site', ?, 0)"
				" ON CONFLICT(key) DO UPDATE SET value = excluded.value",
				(before,),
			)
	settings_mod._cache = None


def _apply(code: str, email: str = "", **extra):
	body = {
		"code": code,
		"name": code.title(),
		"email": email or f"{code}@applicant.test",
		"country": "PK",
		"promo": "YouTube channel about video editing",
	}
	return client.post("/api/partner/join", json={**body, **extra})


def _verify_url(sent: list[dict]) -> str:
	"""The confirmation link, taken out of the email that was actually sent.

	Read from the message rather than rebuilt from the code, because the thing
	most likely to be wrong is the link — a token minted with the wrong scope or
	a URL pointing at a path nothing serves both look fine from the inside.
	"""
	body = next(m["body"] for m in reversed(sent) if m["what"] == "affiliate verification")
	return next(line.strip() for line in body.splitlines() if "/confirm?t=" in line)


def test_a_sign_up_earns_nothing_until_the_email_is_confirmed(mailbox):
	r = _apply("newbie")
	assert r.status_code == 200 and r.json()["status"] == "pending"
	assert store.get_affiliate("newbie")["status"] == "pending"
	assert store.get_affiliate("newbie")["source"] == "signup"

	# The code is reserved from the moment they apply, so nobody takes it while
	# they are reading their email.
	assert _apply("newbie", email="other@applicant.test").status_code == 409

	# A sale through an unconfirmed link is a real sale that pays nobody.
	key = _stripe_post(_sale("cs_signup_1", "b1@example.com", ref="newbie")).json()["key"]
	assert store.get(key)["email"] == "b1@example.com"
	assert store.referral_for_licence(key) is None


def test_confirming_the_email_makes_the_link_live_and_sends_it_to_them(mailbox):
	_apply("liveone")
	assert client.get(_verify_url(mailbox)).status_code == 200

	affiliate = store.get_affiliate("liveone")
	assert affiliate["status"] == "active" and affiliate["email_verified"] == 1

	# The welcome email has to carry the link — it is the message they will search
	# their inbox for weeks later.
	welcome = next(m for m in mailbox if m["what"] == "affiliate welcome")
	assert "?ref=liveone" in welcome["body"]
	# And the owner is told, once the address is proved rather than when the form
	# was submitted.
	assert any(m["what"] == "affiliate signup notice" for m in mailbox)

	key = _stripe_post(_sale("cs_signup_2", "b2@example.com", ref="liveone")).json()["key"]
	assert store.referral_for_licence(key)["commission"] == 1170


def test_confirming_twice_signs_them_in_rather_than_breaking(mailbox):
	"""People click these links more than once, and mail scanners open them first."""
	_apply("twice")
	url = _verify_url(mailbox)
	assert client.get(url).status_code == 200
	assert client.get(url).status_code == 200
	assert store.get_affiliate("twice")["status"] == "active"
	# Still one welcome, not one per click.
	assert sum(1 for m in mailbox if m["what"] == "affiliate welcome") == 1


def test_the_form_cannot_choose_its_own_commission_rate(mailbox):
	"""The request comes from a page we do not control."""
	_apply("greedy", rate_pct=90)
	assert store.get_affiliate("greedy")["rate_pct"] == 30


def test_one_email_address_gets_one_account(mailbox):
	_apply("first", email="same@applicant.test")
	r = _apply("second", email="same@applicant.test")
	assert r.status_code == 409
	# Pointed at signing in, and told nothing about who owns the address.
	assert "sign-in" in r.json()["error"]
	assert store.get_affiliate("second") is None


def test_codes_that_could_pass_for_us_are_refused(mailbox):
	for code in ("softclipper", "SoftClipper-Official", "admin", "ab"):
		r = _apply(code)
		assert r.status_code == 400, f"{code} should not be allowed"


def test_the_honeypot_looks_like_success_and_creates_nothing(mailbox):
	r = _apply("botcode", website="http://spam.example")
	assert r.status_code == 200
	assert store.get_affiliate("botcode") is None
	assert mailbox == []


def test_sign_ups_can_be_closed_without_touching_anyone_already_in(mailbox):
	_apply("early")
	client.get(_verify_url(mailbox))

	_save({"affiliates": {"selfSignup": False}})
	assert _apply("late").status_code == 403
	assert client.get("/api/site-config").json()["affiliates"]["selfSignup"] is False

	# The one who got in before the door closed still earns.
	key = _stripe_post(_sale("cs_signup_3", "b3@example.com", ref="early")).json()["key"]
	assert store.referral_for_licence(key)["commission"] == 1170


def test_closing_the_programme_closes_the_form_with_it(mailbox):
	_save({"affiliates": {"enabled": False}})
	assert _apply("shut").status_code == 403
	assert client.get("/api/site-config").json()["affiliates"]["selfSignup"] is False


def test_manual_approval_holds_them_in_a_queue_and_tells_them_the_answer(mailbox):
	_save({"affiliates": {"autoApprove": False}})
	_apply("queued")
	client.get(_verify_url(mailbox))

	assert store.get_affiliate("queued")["status"] == "review"
	# No welcome yet — they have not been approved.
	assert not any(m["what"] == "affiliate welcome" for m in mailbox)
	# And a sale in the meantime pays nobody.
	key = _stripe_post(_sale("cs_signup_4", "b4@example.com", ref="queued")).json()["key"]
	assert store.referral_for_licence(key) is None

	r = client.post("/api/admin/affiliates/queued/decide", json={"approve": True}, headers=ADMIN)
	assert r.status_code == 200 and store.get_affiliate("queued")["status"] == "active"
	assert any("?ref=queued" in m["body"] for m in mailbox if m["what"] == "affiliate welcome")


def test_a_rejected_applicant_is_told_so(mailbox):
	_save({"affiliates": {"autoApprove": False}})
	_apply("nope")
	client.get(_verify_url(mailbox))

	r = client.post(
		"/api/admin/affiliates/nope/decide",
		json={"approve": False, "reason": "Your site is not a fit."},
		headers=ADMIN,
	)
	assert r.status_code == 200
	assert store.get_affiliate("nope")["status"] == "rejected"
	decision = next(m for m in mailbox if m["what"] == "affiliate decision")
	assert "not a fit" in decision["body"]

	# Deciding twice is refused rather than sending a second email.
	assert client.post(
		"/api/admin/affiliates/nope/decide", json={"approve": True}, headers=ADMIN
	).status_code == 400


def test_deciding_needs_the_admin_token():
	assert client.post("/api/admin/affiliates/anything/decide", json={"approve": True}).status_code == 401


def test_signing_in_says_the_same_thing_whether_or_not_the_account_exists(mailbox):
	_apply("known")
	client.get(_verify_url(mailbox))
	mailbox.clear()

	real = client.post("/api/partner/signin", json={"email": "known@applicant.test"})
	fake = client.post("/api/partner/signin", json={"email": "nobody@nowhere.test"})
	assert real.status_code == fake.status_code == 200
	assert real.json() == fake.json()
	# One link went out, and only to the address that has an account.
	assert [m["to"] for m in mailbox if m["what"] == "affiliate sign-in link"] == ["known@applicant.test"]


def test_a_sign_in_link_cannot_be_used_as_a_confirmation_link_or_a_session():
	"""Every one of these is a valid signature over a valid payload. The scope is
	the only thing that says what the holder was actually given permission to do."""
	token = crypto.make_scoped("aff-login", "somebody", 30)
	assert crypto.read_scoped(token, "aff-login") == "somebody"
	assert crypto.read_scoped(token, "aff-verify") == ""
	assert crypto.read_scoped(token, "aff-session") == ""
	# And an expired one is nobody, whatever it says.
	assert crypto.read_scoped(crypto.make_scoped("aff-login", "x", -1), "aff-login") == ""


def test_the_old_affiliate_paths_still_answer():
	"""They were renamed because ad blockers cancel a request with "affiliate" in
	the URL. Anything already sent to somebody — a confirmation link sitting in an
	inbox, a bookmark — has to keep working, so both names reach one handler."""
	assert client.get("/affiliate").status_code == 200
	assert client.get("/partner").status_code == 200
	# Unauthenticated, but answering. A 404 here would mean the route was dropped.
	assert client.get("/api/affiliate/me").status_code == 401
	assert client.get("/api/partner/me").status_code == 401
	assert client.post("/api/affiliates/click?code=whoever").status_code == 200
	assert client.post("/api/partner/visit?code=whoever").status_code == 200


def test_no_endpoint_a_browser_calls_carries_a_blocker_keyword():
	"""The paths the *site* and the portal call are the ones that have to survive
	a tracker blocker. The sign-up fetch was being cancelled before it left the
	browser, and all JavaScript is told is "Failed to fetch" — no status, no body,
	nothing pointing at a cause. The old names stay reachable; nothing reaches for
	them."""
	from licence import app as mod

	routes = {r.path for r in mod.app.routes}
	for path in ("/api/partner/join", "/api/partner/visit", "/api/partner/signin"):
		assert path in routes, path
		assert "affiliate" not in path and "click" not in path

	page = pathlib.Path(mod.__file__).with_name("affiliate.html").read_text(encoding="utf-8")
	assert "/api/affiliate/" not in page and "/api/affiliates/" not in page


def test_the_sign_up_form_posts_itself_with_no_javascript(mailbox):
	"""The path the site actually uses.

	It is a plain form post, not a `fetch`, because a cross-host XHR is the one
	kind of request ad blockers, privacy extensions, company firewalls and
	antivirus proxies filter — and when one drops it the browser reports a bare
	"Failed to fetch" while the server has no record it was ever asked. That
	happened to a real person here. A navigation has nothing for any of them to
	match on, and works with JavaScript switched off.
	"""
	r = client.post(
		"/partner/join",
		content="name=Sara+Khan&email=formpost%40applicant.test&code=formpost&country=PK&promo=YouTube",
		headers={"Content-Type": "application/x-www-form-urlencoded"},
		follow_redirects=False,
	)
	# 303 and not 307: the browser must follow with a GET, or refreshing the page
	# re-submits the application.
	assert r.status_code == 303, r.text
	assert "joined=formpost%40applicant.test" in r.headers["location"]

	affiliate = store.get_affiliate("formpost")
	assert affiliate["status"] == "pending" and affiliate["source"] == "signup"
	assert any(m["what"] == "affiliate verification" for m in mailbox)

	# And it goes live the same way a JSON sign-up does.
	assert client.get(_verify_url(mailbox)).status_code == 200
	assert store.get_affiliate("formpost")["status"] == "active"


def test_a_refused_form_post_comes_back_as_a_page_saying_why(mailbox):
	"""Not a bare error page. Somebody who has just typed six fields should be
	told what was wrong, somewhere they can read it."""
	_apply("taken")
	r = client.post(
		"/partner/join",
		content="name=X&email=other%40applicant.test&code=taken",
		headers={"Content-Type": "application/x-www-form-urlencoded"},
		follow_redirects=False,
	)
	assert r.status_code == 303
	assert "problem=" in r.headers["location"]
	assert "taken" in r.headers["location"]


def test_the_form_and_the_json_endpoint_refuse_the_same_things(mailbox):
	"""Two front doors, one set of rules. A code allowed at one and refused at the
	other is somebody who has already put their link in a video description."""
	form = client.post(
		"/partner/join",
		content="name=X&email=res%40applicant.test&code=softclipper",
		headers={"Content-Type": "application/x-www-form-urlencoded"},
		follow_redirects=False,
	)
	api = client.post(
		"/api/partner/join",
		json={"name": "X", "email": "res2@applicant.test", "code": "softclipper"},
	)
	assert form.status_code == 303 and "problem=" in form.headers["location"]
	assert api.status_code == 400
	assert store.get_affiliate("softclipper") is None


def test_the_portal_page_is_served_and_is_not_indexed():
	"""The page every link in every affiliate email points at. It is one file next
	to the API, so the way this breaks is the file not shipping — which nothing
	else here would notice."""
	r = client.get("/partner")
	assert r.status_code == 200
	assert "noindex" in r.text
	assert "/api/partner/me" in r.text


def test_the_marketing_site_may_post_the_sign_up_form_and_nobody_else():
	"""The form lives on another origin, so this header is the whole of what makes
	it work. Nothing in the tests above would fail if it were missing — the sign-up
	endpoint answers perfectly well, and every browser throws the answer away."""
	allowed = client.post(
		"/api/partner/join",
		json={"name": "X", "email": "cors@applicant.test", "code": "corsone"},
		headers={"Origin": "https://softclipper.pro"},
	)
	assert allowed.headers.get("access-control-allow-origin") == "https://softclipper.pro"

	stranger = client.post(
		"/api/partner/join",
		json={"name": "X", "email": "cors2@applicant.test", "code": "corstwo"},
		headers={"Origin": "https://not-us.example"},
	)
	assert "access-control-allow-origin" not in stranger.headers

	# Credentials are deliberately not allowed across origins: the portal session
	# cookie is only ever used on this host, by the page this service serves.
	assert "access-control-allow-credentials" not in allowed.headers


def test_a_broken_link_lands_on_the_portal_saying_so():
	r = client.get("/partner/confirm?t=rubbish", follow_redirects=False)
	assert r.status_code == 303 and "problem=link" in r.headers["location"]


def test_the_dashboard_needs_a_session_and_then_shows_their_own_numbers(mailbox):
	assert client.get("/api/partner/me").status_code == 401

	_apply("dash")
	client.get(_verify_url(mailbox))  # signs them in and sets the cookie
	_stripe_post(_sale("cs_signup_5", "b5@example.com", ref="dash"))

	me = client.get("/api/partner/me").json()
	assert me["affiliate"]["code"] == "dash"
	assert me["link"].endswith("?ref=dash")
	assert me["totals"]["sales"] == 1
	assert me["totals"]["holding"] == 1170  # still inside the hold window
	assert me["totals"]["due"] == 0
	# The affiliate is owed the sale and the money, not the buyer's identity.
	assert "b5@example.com" not in json.dumps(me)

	client.post("/api/partner/signout")
	assert client.get("/api/partner/me").status_code == 401


def test_an_affiliate_sets_their_own_payout_details(mailbox):
	_apply("payme")
	client.get(_verify_url(mailbox))

	# Being paid by hand is the path that works from anywhere, and it needs to
	# know where to send it.
	assert client.post(
		"/api/partner/payout", json={"payout_method": "manual", "payout_to": ""}
	).status_code == 400

	r = client.post(
		"/api/partner/payout",
		json={"payout_method": "manual", "payout_to": "wise: payme@applicant.test"},
	)
	assert r.status_code == 200
	assert store.get_affiliate("payme")["payout_to"] == "wise: payme@applicant.test"

	# Stripe is not configured in tests, and the answer says so rather than
	# offering a route that can only fail.
	r = client.post("/api/partner/payout", json={"payout_method": "stripe"})
	assert r.status_code == 503 and "by hand" in r.json()["error"]


def test_a_disabled_affiliate_can_still_see_what_they_are_owed(mailbox):
	"""Commission already earned is still owed to them — the terms say so, and a
	locked door on the page that says how much is how that becomes a complaint."""
	_apply("offnow")
	client.get(_verify_url(mailbox))
	_stripe_post(_sale("cs_signup_6", "b6@example.com", ref="offnow"))
	store.set_affiliate_status("offnow", "disabled", "testing")

	me = client.get("/api/partner/me").json()
	assert me["affiliate"]["status"] == "disabled"
	assert me["totals"]["holding"] == 1170


def test_clicks_are_counted_only_for_codes_that_exist(mailbox):
	_apply("clicky")
	client.get(_verify_url(mailbox))

	for _ in range(3):
		assert client.post("/api/partner/visit?code=clicky").status_code == 200
	# An unknown code is accepted and counted for nobody. Answering differently
	# would let anyone walk the alphabet and list our affiliates.
	assert client.post("/api/partner/visit?code=nosuchaffiliate").json() == {"ok": True}

	assert store.clicks_for("clicky")["total"] == 3
	assert store.clicks_for("nosuchaffiliate")["total"] == 0
	assert next(a for a in store.affiliate_summary() if a["code"] == "clicky")["clicks"] == 3


def test_buying_through_your_own_link_earns_nothing():
	"""The one rule the affiliate terms name as grounds for closing an account."""
	_affiliate("selfie")
	key = _stripe_post(_sale("cs_self_1", "selfie@example.com", ref="selfie")).json()["key"]
	assert store.get(key)["email"] == "selfie@example.com"  # the sale still stands
	assert store.referral_for_licence(key) is None


def test_an_affiliate_added_by_hand_is_untouched_by_any_of_this():
	"""The golden rule, as a test. Everything above is additive: a row the owner
	typed in before self sign-up existed must still be active, still earn, and
	still be paid exactly as it was."""
	created = _affiliate("oldschool", rate=40)
	assert created["status"] == "active"
	assert created["source"] == "admin"
	assert created["email_verified"] == 0  # never asked, never needed

	key = _stripe_post(_sale("cs_hand_1", "handbuyer@example.com", ref="oldschool")).json()["key"]
	assert store.referral_for_licence(key)["commission"] == 1560  # 40% of $39


def test_the_mac_default_ships_with_a_link_to_go_with_it():
	"""This used to assert the Mac switch defaulted to off, and it went stale the
	day the Mac build was published — the default moved and the test did not.

	What is worth holding on to is the pairing, not the value: whichever way the
	switch starts, it must not start turned on with nowhere to send anyone. That
	stays true the next time the default changes."""
	downloads = client.get("/api/site-config").json()["downloads"]
	if downloads["macEnabled"]:
		assert downloads["macUrl"].startswith("https://")
		assert downloads["macUrl"].endswith(".dmg"), "the dmg ships, a zip of the app does not"
