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

-- Someone who sends us customers and takes a cut.
--
-- Two ways of being paid, because affiliates are not all in the same place.
-- Stripe can pay out to connected accounts in around fifty countries and to
-- nobody else — Pakistan, Bangladesh and Nigeria among the nots — so a system
-- that only knew how to pay through Stripe could not pay the people most likely
-- to be promoting this first. `payout_method` is per affiliate, and the two
-- paths meet again at the same referral rows and the same totals.
--
-- Rows arrive two ways and the table cannot tell them apart afterwards, so
-- `source` records which: 'admin' for one the owner typed in, 'signup' for one
-- that came through the public form. Everything else about them is identical —
-- the same link, the same commission, the same payout paths.
CREATE TABLE IF NOT EXISTS affiliates (
    code           TEXT PRIMARY KEY,                 -- what goes in ?ref=, lowercase
    name           TEXT NOT NULL,
    email          TEXT NOT NULL,
    rate_pct       INTEGER NOT NULL,                 -- their cut, per affiliate: a big
                                                     -- partner can be worth more than 30%
    payout_method  TEXT NOT NULL DEFAULT 'manual',   -- manual | stripe
    payout_to      TEXT,                             -- manual: Wise/PayPal/bank, as given
    stripe_account TEXT,                             -- stripe: acct_… once onboarded
    stripe_ready   INTEGER NOT NULL DEFAULT 0,       -- Stripe says payouts are enabled
    -- pending  applied, has not clicked the link in their email yet
    -- review   email confirmed, waiting for the owner to approve
    -- active   earning; the only status a sale is ever credited to
    -- disabled turned off by the owner
    -- rejected application declined
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     INTEGER NOT NULL,
    note           TEXT,
    country        TEXT,                             -- two letters; decides Stripe eligibility
    promo          TEXT,                             -- where they said they would promote
    source         TEXT,                             -- admin | signup
    email_verified INTEGER NOT NULL DEFAULT 0,
    applied_at     INTEGER,
    decided_at     INTEGER,                          -- when it was approved or rejected
    decided_note   TEXT,
    last_seen_at   INTEGER                           -- last sign-in to their own dashboard
);
CREATE INDEX IF NOT EXISTS affiliates_email ON affiliates(email);

-- Link clicks, counted per day rather than one row per visitor.
--
-- Aggregated on the way in because the only question anyone asks of this is
-- "how many, and is it converting" — and a row per click would grow without
-- limit for a number nobody reads at that resolution. Nothing identifying is
-- kept: no IP, no user agent, no visitor id, so this stays out of the way of
-- consent rules that would otherwise apply to it.
CREATE TABLE IF NOT EXISTS clicks (
    code TEXT NOT NULL,
    day  TEXT NOT NULL,                              -- YYYY-MM-DD, UTC
    n    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, day)
);

-- One row per sale that carried a ref tag. This is the money record, so amounts
-- are stored in the smallest currency unit as integers: a commission worked out
-- in floating point is a commission that is one cent wrong, and it is wrong in
-- the direction of whoever is not looking.
CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,                    -- affiliates.code
    licence_key TEXT NOT NULL,
    session     TEXT NOT NULL UNIQUE,             -- Stripe session id: the retry guard
    currency    TEXT NOT NULL,
    gross       INTEGER NOT NULL,                 -- what the customer actually paid
    rate_pct    INTEGER NOT NULL,                 -- copied, not looked up: changing an
                                                  -- affiliate's rate must not silently
                                                  -- rewrite what they already earned
    commission  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | void
    created_at  INTEGER NOT NULL,
    due_at      INTEGER NOT NULL,                 -- when the refund window closes
    paid_at     INTEGER,
    paid_how    TEXT,                             -- 'stripe' or how you sent it by hand
    transfer_id TEXT,                             -- Stripe tr_… where there is one
    void_reason TEXT
);
CREATE INDEX IF NOT EXISTS referrals_code ON referrals(code);
CREATE INDEX IF NOT EXISTS referrals_licence ON referrals(licence_key);

-- Everything an owner can change without a developer: price, discount, download
-- links, whether the affiliate programme is open. One JSON row, so a save is a
-- single atomic write and there is no half-applied settings state.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

# Columns added after the table first shipped. SQLite has no "ADD COLUMN IF NOT
# EXISTS", and the live database already holds real licences, so this is how a
# new column reaches it without a hand-run migration on the server — which is
# the step that gets forgotten and takes the service down on restart.
_ADDED_COLUMNS = {
	"licences": [("ref", "TEXT")],  # the affiliate code that sold it, if any
	# Everything self-signup needed. The live database already holds affiliates
	# the owner added by hand, and they must keep working untouched: every column
	# here is nullable or defaults to the value those rows already behave as, so
	# an existing 'active' affiliate is still active, still earning, still paid
	# the same way after this runs.
	"affiliates": [
		("country", "TEXT"),
		("promo", "TEXT"),
		("source", "TEXT"),
		("email_verified", "INTEGER NOT NULL DEFAULT 0"),
		("applied_at", "INTEGER"),
		("decided_at", "INTEGER"),
		("decided_note", "TEXT"),
		("last_seen_at", "INTEGER"),
	],
}


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
		for table, columns in _ADDED_COLUMNS.items():
			have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
			for name, kind in columns:
				if name not in have:
					conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def log(conn, key: str | None, kind: str, detail: str = ""):
	"""Every state change is recorded. When a customer says "it says my key is in
	use" this is the only way to know whether they moved machines twice or shared it."""
	conn.execute(
		"INSERT INTO events (key, kind, detail, at) VALUES (?,?,?,?)",
		(key, kind, detail, int(time.time())),
	)


def create(key: str, email: str, source: str = "manual", note: str = "", ref: str = "") -> dict:
	with db() as conn:
		conn.execute(
			"INSERT INTO licences (key, email, created_at, source, note, ref) VALUES (?,?,?,?,?,?)",
			(key, email.lower().strip(), int(time.time()), source, note, ref or None),
		)
		log(conn, key, "created", f"{email} via {source}" + (f" ref:{ref}" if ref else ""))
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


# ── affiliates ───────────────────────────────────────────────────────────────


def normalise_code(code: str) -> str:
	"""Affiliate codes travel through a URL, get typed into a podcast description
	and get read out loud. So they are lowercased and stripped to the characters
	that survive all three — anything else is a support email waiting to happen."""
	return "".join(c for c in (code or "").strip().lower() if c.isalnum() or c in "-_")[:40]


# Codes nobody may take for themselves.
#
# A referral code is read out loud and typed into a URL beside our own name, so
# `?ref=softclipper-official` is a person passing themselves off as us — the one
# form of affiliate abuse that damages the brand rather than just costing a
# commission. The rest are paths and words that would be confusing next to the
# product's own.
_RESERVED = {
	"admin", "api", "app", "www", "support", "help", "info", "sales", "billing",
	"soft", "clipper", "softclipper", "official", "team", "staff", "test", "null",
}


def code_problem(code: str) -> str:
	"""Why this code cannot be used, or "" if it can.

	One function so the public sign-up form, the admin form and the tests all
	refuse exactly the same things — a code accepted in one place and rejected in
	another is a person who has already put the link in a video description.
	"""
	cleaned = normalise_code(code)
	if len(cleaned) < 3:
		return "Pick at least 3 letters or numbers."
	if cleaned in _RESERVED or "softclip" in cleaned:
		return "That one is reserved. Pick something that is clearly yours — your name or channel works well."
	if cleaned.isdigit():
		return "An all-numbers code reads like an order number. Use some letters."
	return ""


def add_affiliate(
	code: str,
	name: str,
	email: str,
	rate_pct: int,
	payout_method: str = "manual",
	payout_to: str = "",
	note: str = "",
	status: str = "active",
	source: str = "admin",
	country: str = "",
	promo: str = "",
) -> dict:
	"""Create an affiliate.

	The defaults are the behaviour this function had before self-sign-up existed:
	call it with the original six arguments and you get an active affiliate the
	owner added by hand, exactly as before. The public form passes the rest.
	"""
	code = normalise_code(code)
	now = int(time.time())
	with db() as conn:
		conn.execute(
			"INSERT INTO affiliates (code, name, email, rate_pct, payout_method, payout_to,"
			" created_at, note, status, source, country, promo, applied_at)"
			" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
			(code, name.strip(), email.lower().strip(), int(rate_pct), payout_method,
			 payout_to.strip(), now, note.strip(), status, source,
			 (country or "").strip().upper()[:2], (promo or "").strip()[:500], now),
		)
		log(conn, None, "affiliate_added", f"{code} at {rate_pct}% via {payout_method} ({source}, {status})")
	return get_affiliate(code)


def affiliate_by_email(email: str) -> dict | None:
	"""The one account for an address.

	Sign-up refuses a second application from an address that already has one, and
	signing in finds the account this way — so one person cannot quietly end up
	with three codes and wonder why their totals are split across them.
	"""
	with db() as conn:
		row = conn.execute(
			"SELECT * FROM affiliates WHERE email = ? ORDER BY created_at LIMIT 1",
			((email or "").lower().strip(),),
		).fetchone()
		return dict(row) if row else None


def verify_affiliate_email(code: str, status: str) -> dict | None:
	"""Mark the address confirmed and move the application on.

	`status` is decided by the caller from the owner's settings — straight to
	active when applications are approved automatically, to `review` when they are
	not. Confirming twice is harmless and lands in the same place, which matters
	because people click the link in an email more than once.
	"""
	code = normalise_code(code)
	with db() as conn:
		conn.execute(
			"UPDATE affiliates SET email_verified = 1, status = ?"
			" WHERE code = ? AND status = 'pending'",
			(status, code),
		)
		log(conn, None, "affiliate_verified", f"{code} -> {status}")
	return get_affiliate(code)


def set_affiliate_payout(code: str, method: str, payout_to: str) -> dict | None:
	"""How this affiliate wants to be paid, as they set it themselves.

	Deliberately does not touch `stripe_account` or `stripe_ready`. Someone who
	switches to being paid by hand and later switches back should not have to go
	through Stripe's identity checks a second time for an account that still
	exists and is still verified.
	"""
	code = normalise_code(code)
	with db() as conn:
		conn.execute(
			"UPDATE affiliates SET payout_method = ?, payout_to = ? WHERE code = ?",
			(method, (payout_to or "").strip()[:300], code),
		)
		log(conn, None, "affiliate_payout", f"{code} via {method}")
	return get_affiliate(code)


def touch_affiliate(code: str):
	with db() as conn:
		conn.execute(
			"UPDATE affiliates SET last_seen_at = ? WHERE code = ?",
			(int(time.time()), normalise_code(code)),
		)


def record_click(code: str) -> bool:
	"""Count one visit through a referral link.

	Only counted for a code that exists, so a bot walking `?ref=aaa`, `?ref=aab`
	cannot fill the table with rows for affiliates who do not exist. Returns
	whether it counted, which is only used by the tests — the endpoint itself
	answers the same way either way, because telling a caller which codes are real
	is a list of our affiliates for anyone who asks for it.
	"""
	code = normalise_code(code)
	if not code:
		return False
	day = time.strftime("%Y-%m-%d", time.gmtime())
	with db() as conn:
		exists = conn.execute("SELECT 1 FROM affiliates WHERE code = ?", (code,)).fetchone()
		if not exists:
			return False
		conn.execute(
			"INSERT INTO clicks (code, day, n) VALUES (?,?,1)"
			" ON CONFLICT(code, day) DO UPDATE SET n = n + 1",
			(code, day),
		)
	return True


def clicks_for(code: str, days: int = 30) -> dict:
	"""Total clicks, and the recent ones, for one affiliate's own dashboard."""
	code = normalise_code(code)
	since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
	with db() as conn:
		total = conn.execute(
			"SELECT COALESCE(SUM(n), 0) AS n FROM clicks WHERE code = ?", (code,)
		).fetchone()["n"]
		recent = conn.execute(
			"SELECT COALESCE(SUM(n), 0) AS n FROM clicks WHERE code = ? AND day >= ?",
			(code, since),
		).fetchone()["n"]
		return {"total": int(total), "recent": int(recent), "days": days}


def set_affiliate_stripe(code: str, account: str, ready: bool):
	"""Remember the connected account, and whether Stripe will actually pay it.

	`ready` is Stripe's answer, not ours — an account can exist for weeks while
	its owner ignores the identity documents, and transferring to one that is not
	ready fails at the worst moment. It is refreshed from `account.updated`.
	"""
	with db() as conn:
		conn.execute(
			"UPDATE affiliates SET stripe_account = ?, stripe_ready = ?, payout_method = 'stripe'"
			" WHERE code = ?",
			(account, 1 if ready else 0, normalise_code(code)),
		)
		log(conn, None, "affiliate_stripe", f"{normalise_code(code)} {account} ready={int(ready)}")


def affiliate_by_stripe_account(account: str) -> dict | None:
	with db() as conn:
		row = conn.execute(
			"SELECT * FROM affiliates WHERE stripe_account = ?", (account,)
		).fetchone()
		return dict(row) if row else None


def get_affiliate(code: str) -> dict | None:
	with db() as conn:
		row = conn.execute(
			"SELECT * FROM affiliates WHERE code = ?", (normalise_code(code),)
		).fetchone()
		return dict(row) if row else None


def set_affiliate_status(code: str, status: str, note: str = ""):
	"""Approve, reject, disable or re-enable.

	`note` is why, and it is kept because the answer to "you turned me off, what
	did I do" is otherwise whatever anyone remembers three months later.
	"""
	with db() as conn:
		conn.execute(
			"UPDATE affiliates SET status = ?, decided_at = ?, decided_note = ? WHERE code = ?",
			(status, int(time.time()), (note or "").strip()[:300] or None, normalise_code(code)),
		)
		log(conn, None, "affiliate_" + status, f"{normalise_code(code)} {note}".strip())


def record_referral(
	code: str,
	licence_key: str,
	session: str,
	gross: int,
	currency: str,
	rate_pct: int,
	hold_days: int,
) -> dict | None:
	"""Credit a sale to an affiliate.

	Returns None if this session was already credited. Stripe retries a webhook
	until it gets a 2xx, so without that guard a slow response would pay the same
	commission twice — and unlike a duplicate licence, nobody notices a duplicate
	commission until the money has gone.
	"""
	now = int(time.time())
	commission = gross * int(rate_pct) // 100  # integer maths, rounded down, in our favour
	with db() as conn:
		cur = conn.execute(
			"INSERT OR IGNORE INTO referrals (code, licence_key, session, currency, gross,"
			" rate_pct, commission, created_at, due_at) VALUES (?,?,?,?,?,?,?,?,?)",
			(normalise_code(code), licence_key, session, currency.lower(), int(gross),
			 int(rate_pct), commission, now, now + hold_days * 86400),
		)
		if cur.rowcount == 0:
			return None
		log(conn, licence_key, "referral", f"{code} {commission/100:.2f} {currency.lower()}")
	return referral_for_licence(licence_key)


def referral_for_licence(licence_key: str) -> dict | None:
	with db() as conn:
		row = conn.execute(
			"SELECT * FROM referrals WHERE licence_key = ?", (licence_key,)
		).fetchone()
		return dict(row) if row else None


def void_referral(licence_key: str, reason: str) -> bool:
	"""A refunded or disputed sale earns nothing. Only unpaid commission is
	clawed back here — once it has been sent, taking it back is a conversation,
	not a database update, so an already-paid row is left alone and shows up in
	the admin list as paid against a revoked licence."""
	with db() as conn:
		cur = conn.execute(
			"UPDATE referrals SET status = 'void', void_reason = ?"
			" WHERE licence_key = ? AND status = 'pending'",
			(reason, licence_key),
		)
		if cur.rowcount:
			log(conn, licence_key, "referral_void", reason)
		return bool(cur.rowcount)


def mark_referrals_paid(
	ids: list[int], how: str = "manual", transfer_id: str = "", detail: str = ""
) -> int:
	"""Close rows as paid. `status = 'pending'` in the WHERE is not decoration:
	it is what stops a double-click, a retried request or a second admin tab from
	paying the same commission twice."""
	with db() as conn:
		now = int(time.time())
		marked = 0
		for rid in ids:
			cur = conn.execute(
				"UPDATE referrals SET status = 'paid', paid_at = ?, paid_how = ?, transfer_id = ?"
				" WHERE id = ? AND status = 'pending'",
				(now, how, transfer_id or None, int(rid)),
			)
			marked += cur.rowcount
		if marked:
			log(conn, None, "referrals_paid", f"{marked} rows via {how} {detail}".strip())
		return marked


def payable(code: str) -> list[dict]:
	"""Rows that are genuinely payable right now: past the hold, not already paid,
	not clawed back. Both payout paths ask this same question."""
	now = int(time.time())
	with db() as conn:
		rows = conn.execute(
			"SELECT * FROM referrals WHERE code = ? AND status = 'pending' AND due_at <= ?"
			" ORDER BY created_at",
			(normalise_code(code), now),
		).fetchall()
		return [dict(r) for r in rows]


def affiliate_summary() -> list[dict]:
	"""Every affiliate with what they have earned, split by what is payable now.

	`due` is derived from the clock rather than stored, so there is no scheduled
	job that has to keep running for the numbers to be right — a cron that dies
	quietly would leave commissions stuck at pending forever, and the first person
	to notice would be the affiliate who was not paid.
	"""
	now = int(time.time())
	with db() as conn:
		rows = conn.execute(
			"""
			SELECT a.*,
			  -- A scalar subquery, not a second LEFT JOIN. Joining clicks as well
			  -- as referrals would multiply the rows of one by the other and every
			  -- money column below would come out wrong — silently, and too high.
			  (SELECT COALESCE(SUM(c.n), 0) FROM clicks c WHERE c.code = a.code)  AS clicks,
			  COUNT(r.id)                                                        AS sales,
			  -- Totals only mean something in one currency. Today every sale is
			  -- USD, so rather than build multi-currency accounting for a case
			  -- that does not exist, the count is surfaced and the admin page
			  -- says so out loud if it ever stops being 1.
			  COUNT(DISTINCT r.currency)                                         AS currencies,
			  COALESCE(MAX(r.currency), 'usd')                                   AS currency,
			  COALESCE(SUM(CASE WHEN r.status='pending' AND r.due_at<=:now
			                    THEN r.commission END), 0)                       AS due,
			  COALESCE(SUM(CASE WHEN r.status='pending' AND r.due_at>:now
			                    THEN r.commission END), 0)                       AS holding,
			  COALESCE(SUM(CASE WHEN r.status='paid' THEN r.commission END), 0)  AS paid,
			  COALESCE(SUM(CASE WHEN r.status='void' THEN r.commission END), 0)  AS voided
			FROM affiliates a
			LEFT JOIN referrals r ON r.code = a.code
			GROUP BY a.code
			ORDER BY due DESC, sales DESC, a.created_at DESC
			""",
			{"now": now},
		).fetchall()
		return [dict(r) for r in rows]


def referrals(code: str = "", limit: int = 200) -> list[dict]:
	now = int(time.time())
	with db() as conn:
		sql = (
			"SELECT r.*, l.email AS customer_email, l.status AS licence_status,"
			" (r.status='pending' AND r.due_at<=?) AS payable"
			" FROM referrals r LEFT JOIN licences l ON l.key = r.licence_key"
		)
		args: list = [now]
		if code:
			sql += " WHERE r.code = ?"
			args.append(normalise_code(code))
		sql += " ORDER BY r.created_at DESC LIMIT ?"
		args.append(limit)
		return [dict(r) for r in conn.execute(sql, args).fetchall()]
