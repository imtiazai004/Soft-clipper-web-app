"""Licence keys and the signed tokens that let the app work offline.

The server holds an Ed25519 private key; the desktop app ships with only the
public half. After activation the app stores a signed token and verifies it
locally on every launch, so a customer on a plane still has working software
while a copied token still cannot be forged.

Generate a keypair once:

    python -m licence.crypto keygen
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
	Ed25519PrivateKey,
	Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

# Ambiguous characters are left out: a customer reads this off an email and
# types it, and 0/O and 1/I/l cost support time every single time.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GROUPS, GROUP_LEN = 4, 5


def new_key() -> str:
	"""A licence key like SC-7F3KQ-M2XPD-9WRTV-BH4NC."""
	body = "-".join(
		"".join(secrets.choice(ALPHABET) for _ in range(GROUP_LEN)) for _ in range(GROUPS)
	)
	return f"SC-{body}"


def normalise(key: str) -> str:
	"""Accept what people actually paste: spaces, lowercase, missing prefix."""
	cleaned = "".join(ch for ch in (key or "").upper() if ch.isalnum())
	if cleaned.startswith("SC"):
		cleaned = cleaned[2:]
	if len(cleaned) != GROUPS * GROUP_LEN:
		return ""
	parts = [cleaned[i : i + GROUP_LEN] for i in range(0, len(cleaned), GROUP_LEN)]
	return "SC-" + "-".join(parts)


def _private() -> Ed25519PrivateKey:
	raw = os.environ.get("LICENCE_PRIVATE_KEY", "").strip()
	if not raw:
		raise RuntimeError("LICENCE_PRIVATE_KEY is not set — run `python -m licence.crypto keygen`")
	return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))


def public_key_b64() -> str:
	pub = _private().public_key()
	raw = pub.public_bytes(
		encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
	)
	return base64.b64encode(raw).decode()


def sign_token(payload: dict) -> str:
	"""`base64(json).base64(signature)` — small enough to sit in a JSON field."""
	body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
	sig = base64.urlsafe_b64encode(_private().sign(body.encode())).decode()
	return f"{body}.{sig}"


def verify_token(token: str, public_b64: str) -> dict | None:
	"""Client-side check. Returns the payload, or None if it does not verify."""
	try:
		body, sig = token.split(".", 1)
		pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
		pub.verify(base64.urlsafe_b64decode(sig), body.encode())
		return json.loads(base64.urlsafe_b64decode(body))
	except (ValueError, InvalidSignature, Exception):
		return None


def make_token(key: str, machine: str, days: int) -> str:
	"""The offline pass. `exp` is when the app must check in again, not when the
	licence dies — an expired token means re-validate, never 'you lost your licence'."""
	now = int(time.time())
	return sign_token(
		{"key": key, "machine": machine, "iat": now, "exp": now + days * 86400, "v": 1}
	)


if __name__ == "__main__":
	import sys

	if len(sys.argv) > 1 and sys.argv[1] == "keygen":
		priv = Ed25519PrivateKey.generate()
		raw = priv.private_bytes(
			encoding=serialization.Encoding.Raw,
			format=serialization.PrivateFormat.Raw,
			encryption_algorithm=serialization.NoEncryption(),
		)
		pub = priv.public_key().public_bytes(
			encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
		)
		print("LICENCE_PRIVATE_KEY=" + base64.b64encode(raw).decode())
		print()
		print("Public key — paste into the desktop app's core/licence.py:")
		print("LICENCE_PUBLIC_KEY = \"" + base64.b64encode(pub).decode() + "\"")
	else:
		print(__doc__)
