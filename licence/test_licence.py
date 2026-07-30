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

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

# Configure before importing the app — module-level env reads happen at import.
_tmp = tempfile.mkdtemp()
os.environ["LICENCE_DB"] = os.path.join(_tmp, "test.db")
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

from licence import app as licence_app  # noqa: E402
from licence import crypto, store  # noqa: E402

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
		"data": {"object": {"id": session_id, "customer_details": {"email": email}}},
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
	key = _stripe_post(_sale("cs_aff_7", "once@example.com", ref="once")).json()["key"]
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
