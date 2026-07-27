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
