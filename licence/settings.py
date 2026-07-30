"""The settings an owner should be able to change without a developer.

Price, the discount, the download links, whether the affiliate programme is
open, and a notice banner. All of it lives in one JSON row and is served from
one endpoint, which the marketing site reads **at build time** and bakes into
its HTML.

Why build time and not runtime. The site is static: there is no server between
Cloudflare and the visitor, which is what makes it fast and free and gives it
nothing to attack. Fetching the price in the browser instead would put the old
price in the HTML that Google indexes and in the JSON-LD offer, and show the
visitor a number that changes under them a moment after the page paints. So a
change is saved here, the site is rebuilt, and about a minute later every page
is correct — including the ones a search engine reads.

The defaults below are the same values the site has committed in its own
`consts.ts`. That duplication is deliberate: if this service is unreachable when
the site builds, the site must still build, with the last known-good values,
rather than shipping a pricing page with nothing on it.
"""
from __future__ import annotations

import json
import time

from . import store

# What the product is, before anyone has changed anything.
DEFAULTS: dict = {
	"price": {
		"amount": 39,
		"listAmount": 49,  # 0 turns the strikethrough off entirely
		"currency": "USD",
		"checkoutUrl": "",
	},
	"downloads": {
		"enabled": True,
		"installerUrl": "https://dl.softclipper.pro/Soft-Clipper-Setup.exe",
		"installerSize": "120 MB",
		"zipUrl": "https://dl.softclipper.pro/Soft-Clipper.zip",
		"zipSize": "164 MB",
		# Shown in place of the buttons when downloads are off. There is always a
		# reason, and a page that just hides the button looks broken.
		"offMessage": "Downloads are paused while we ship an update. Back shortly.",
	},
	"affiliates": {
		"enabled": True,
		"ratePct": 30,
		"holdDays": 30,
	},
	"notice": {
		"enabled": False,
		"text": "",
		"tone": "info",  # info | warn
	},
}

_KEY = "site"
# One process, one small row, read on every build and every admin page load.
# Cached for a few seconds so a burst of requests is one query, and short enough
# that "save then publish" never reads a stale copy.
_cache: tuple[float, dict] | None = None
_TTL = 5.0


def _merge(base: dict, over: dict) -> dict:
	"""Overlay saved values on the defaults, one level deep.

	Merged rather than replaced so that adding a new setting in a later version
	does not come back as missing for anyone who saved before it existed — the
	default fills the gap instead of the site building with a hole in it.
	"""
	out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
	for section, values in (over or {}).items():
		if section in out and isinstance(out[section], dict) and isinstance(values, dict):
			out[section].update(values)
		else:
			out[section] = values
	return out


def get(fresh: bool = False) -> dict:
	global _cache
	if not fresh and _cache and time.time() - _cache[0] < _TTL:
		return _cache[1]
	with store.db() as conn:
		row = conn.execute("SELECT value FROM settings WHERE key = ?", (_KEY,)).fetchone()
	saved = json.loads(row["value"]) if row else {}
	merged = _merge(DEFAULTS, saved)
	_cache = (time.time(), merged)
	return merged


def save(patch: dict) -> dict:
	"""Validate and store. Returns the full settings as they now stand."""
	global _cache
	current = get(fresh=True)
	merged = _merge(current, patch)
	validate(merged)
	with store.db() as conn:
		conn.execute(
			"INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
			" ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
			(_KEY, json.dumps(merged), int(time.time())),
		)
		store.log(conn, None, "settings_saved", ", ".join(sorted(patch.keys())))
	_cache = None
	return merged


class Invalid(ValueError):
	"""A setting that would break the site or misprice the product."""


def validate(s: dict):
	price = s.get("price", {})
	amount = price.get("amount")
	if not isinstance(amount, int) or amount <= 0:
		raise Invalid("Price must be a whole number of dollars, above zero.")
	if amount > 10000:
		raise Invalid("That price looks like a typo. Above $10,000 is refused on purpose.")

	listed = price.get("listAmount", 0)
	if not isinstance(listed, int) or listed < 0:
		raise Invalid("The 'was' price must be a whole number, or 0 for no discount.")
	if listed and listed <= amount:
		# A "was" price at or below what is charged is not a discount, it is a
		# claim that is either meaningless or false, and price-marking rules
		# treat it as the second one.
		raise Invalid(
			f"The 'was' price (${listed}) has to be higher than the price you charge "
			f"(${amount}), or set it to 0 to turn the discount off."
		)

	url = price.get("checkoutUrl", "")
	if url and not url.startswith("https://buy.stripe.com/"):
		raise Invalid("The checkout link must be a Stripe Payment Link (https://buy.stripe.com/…).")

	downloads = s.get("downloads", {})
	for field in ("installerUrl", "zipUrl"):
		link = downloads.get(field, "")
		if link and not link.startswith("https://"):
			raise Invalid(f"{field} must be an https:// link.")

	aff = s.get("affiliates", {})
	rate = aff.get("ratePct")
	if not isinstance(rate, int) or not 0 <= rate <= 100:
		raise Invalid("Affiliate commission must be between 0 and 100 percent.")
	hold = aff.get("holdDays")
	if not isinstance(hold, int) or hold < 0:
		raise Invalid("The holding period must be a whole number of days.")
	if hold < 14:
		# The refund window is 14 days. Releasing commission sooner means paying
		# out on sales that can still come back, and clawing it back afterwards
		# is a conversation, not a database update.
		raise Invalid(
			f"Holding commission for only {hold} days is shorter than the 14-day refund "
			"window — a refund would arrive after the commission had been paid."
		)

	notice = s.get("notice", {})
	if notice.get("enabled") and not (notice.get("text") or "").strip():
		raise Invalid("A notice with no text would show an empty bar on every page.")
	if notice.get("tone") not in ("info", "warn"):
		raise Invalid("Notice tone must be 'info' or 'warn'.")


def public() -> dict:
	"""What the marketing site is allowed to read, unauthenticated.

	Everything here ends up in public HTML anyway, so there is nothing to hide —
	but it is built explicitly rather than dumping the settings row, so that a
	setting added later is not published by accident.
	"""
	s = get()
	price, downloads, aff, notice = s["price"], s["downloads"], s["affiliates"], s["notice"]
	return {
		"price": {
			"amount": price["amount"],
			"listAmount": price["listAmount"],
			"currency": price["currency"],
			"checkoutUrl": price["checkoutUrl"],
		},
		"downloads": {
			"enabled": bool(downloads["enabled"]),
			"installerUrl": downloads["installerUrl"],
			"installerSize": downloads["installerSize"],
			"zipUrl": downloads["zipUrl"],
			"zipSize": downloads["zipSize"],
			"offMessage": downloads["offMessage"],
		},
		"affiliates": {"enabled": bool(aff["enabled"]), "ratePct": aff["ratePct"], "holdDays": aff["holdDays"]},
		"notice": notice if notice.get("enabled") else {"enabled": False, "text": "", "tone": "info"},
	}
