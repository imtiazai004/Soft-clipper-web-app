"""SQLite storage for licences.

SQLite because the whole point of this service is to answer one question —
"is this key allowed on this machine?" — a few times per customer per month.
A database server would be more moving parts to keep alive for no gain.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("LICENCE_DB", "/data/licences.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS licences (
    key           TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | revoked
    machine       TEXT,                             -- fingerprint hash, NULL until first activation
    created_at    INTEGER NOT NULL,
    activated_at  INTEGER,
    last_seen_at  INTEGER,
    releases      INTEGER NOT NULL DEFAULT 0,       -- how many times it moved machine
    source        TEXT,                             -- stripe session id, or 'manual'
    note          TEXT
);
CREATE INDEX IF NOT EXISTS licences_email ON licences(email);
CREATE INDEX IF NOT EXISTS licences_source ON licences(source);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key       TEXT,
    kind      TEXT NOT NULL,
    detail    TEXT,
    at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_key ON events(key);
"""


@contextmanager
def db():
	os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
	conn = sqlite3.connect(DB_PATH, timeout=10)
	conn.row_factory = sqlite3.Row
	try:
		conn.execute("PRAGMA journal_mode=WAL")
		yield conn
		conn.commit()
	finally:
		conn.close()


def init():
	with db() as conn:
		conn.executescript(SCHEMA)


def log(conn, key: str | None, kind: str, detail: str = ""):
	"""Every state change is recorded. When a customer says "it says my key is in
	use" this is the only way to know whether they moved machines twice or shared it."""
	conn.execute(
		"INSERT INTO events (key, kind, detail, at) VALUES (?,?,?,?)",
		(key, kind, detail, int(time.time())),
	)


def create(key: str, email: str, source: str = "manual", note: str = "") -> dict:
	with db() as conn:
		conn.execute(
			"INSERT INTO licences (key, email, created_at, source, note) VALUES (?,?,?,?,?)",
			(key, email.lower().strip(), int(time.time()), source, note),
		)
		log(conn, key, "created", f"{email} via {source}")
	return get(key)


def get(key: str) -> dict | None:
	with db() as conn:
		row = conn.execute("SELECT * FROM licences WHERE key = ?", (key,)).fetchone()
		return dict(row) if row else None


def find_by_source(source: str) -> dict | None:
	with db() as conn:
		row = conn.execute("SELECT * FROM licences WHERE source = ?", (source,)).fetchone()
		return dict(row) if row else None


def bind(key: str, machine: str):
	"""First activation, or re-activation on the same machine."""
	now = int(time.time())
	with db() as conn:
		conn.execute(
			"UPDATE licences SET machine = ?, activated_at = COALESCE(activated_at, ?), last_seen_at = ? WHERE key = ?",
			(machine, now, now, key),
		)
		log(conn, key, "activated", machine[:12])


def touch(key: str):
	with db() as conn:
		conn.execute("UPDATE licences SET last_seen_at = ? WHERE key = ?", (int(time.time()), key))


def release(key: str) -> int:
	"""Unbind from its machine so it can be activated elsewhere. Returns the new
	release count — the caller decides whether that number has got suspicious."""
	with db() as conn:
		conn.execute(
			"UPDATE licences SET machine = NULL, releases = releases + 1 WHERE key = ?", (key,)
		)
		log(conn, key, "released")
		row = conn.execute("SELECT releases FROM licences WHERE key = ?", (key,)).fetchone()
		return row["releases"] if row else 0


def revoke(key: str, reason: str):
	with db() as conn:
		conn.execute("UPDATE licences SET status = 'revoked' WHERE key = ?", (key,))
		log(conn, key, "revoked", reason)


def recent(limit: int = 100) -> list[dict]:
	with db() as conn:
		rows = conn.execute(
			"SELECT * FROM licences ORDER BY created_at DESC LIMIT ?", (limit,)
		).fetchall()
		return [dict(r) for r in rows]


def events_for(key: str, limit: int = 50) -> list[dict]:
	with db() as conn:
		rows = conn.execute(
			"SELECT * FROM events WHERE key = ? ORDER BY at DESC LIMIT ?", (key, limit)
		).fetchall()
		return [dict(r) for r in rows]
