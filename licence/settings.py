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
import re
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
		# Two files, and only two. A ZIP of the same Windows build used to sit
		# beside the installer for anyone who would rather not run a setup
		# wizard. It is a different thing to hand over — the product as a folder
		# to open, read and pass on — so it is gone from here, from the site and
		# from the licence email. The terminal command on the download page is
		# the escape hatch for people who dislike installers now.
		"installerUrl": "https://dl.softclipper.pro/Soft-Clipper-Setup.exe",
		"installerSize": "161 MB",
		# Shown in place of the buttons when downloads are off. There is always a
		# reason, and a page that just hides the button looks broken.
		"offMessage": "Downloads are paused while we ship an update. Back shortly.",
		# The Mac build. One switch publishes the download, the install guide and
		# the platform line together, so they cannot disagree.
		#
		# What CI has proved: the suite passes on macOS, a vertical clip really
		# renders, the bundled ffmpeg is self-contained and can burn in
		# subtitles, and the .dmg mounts with the app and an Applications
		# shortcut inside it. What it cannot prove is how the app feels to use,
		# or what Gatekeeper does to an unsigned download on a real machine —
		# the install guide is written for exactly that, and it is worth having
		# someone with a Mac walk it before this is pushed at people.
		"macEnabled": True,
		"macUrl": "https://dl.softclipper.pro/Soft-Clipper.dmg",
		"macSize": "202 MB",
	},
	# What the *desktop app* asks for when it checks for updates — not the site.
	#
	# It used to ask GitHub for the latest release. That stopped working the day
	# the repository went private: GitHub answers an unauthenticated request for a
	# private repo's releases with 404, the app swallows the error and reports
	# "up to date", and every customer stays on an old build forever with nothing
	# to show that anything is wrong. Worse, when it did work it handed out a
	# GitHub release asset — a download for anyone with the URL, which is exactly
	# what the site stopped doing.
	#
	# So the app asks us instead. `latest` is typed here after the build has been
	# uploaded to R2, and that ordering matters: announcing a version whose
	# installer is not yet in place sends every customer to a 404.
	"version": {
		# Empty means "say nothing". The app treats no answer and an empty answer
		# the same way — no update — so a blank field is the safe state, not a
		# broken one.
		"latest": "",
		# Shown in the app beside the offer. A couple of lines about what changed
		# is the difference between an update people install and one they dismiss.
		"notes": "",
		# Whether the app is allowed to mention it at all. Turning this off does
		# not un-publish anything; it stops the nagging while a bad build is being
		# replaced, which is when you least want people installing it.
		"announce": True,
	},
	"affiliates": {
		"enabled": True,
		"ratePct": 30,
		"holdDays": 30,
		# Whether people can sign themselves up, from anywhere, without being
		# added by hand. On, because a programme that needs the owner to type
		# every affiliate in is a programme with as many affiliates as the owner
		# has evenings.
		#
		# Turning it off closes the form and puts the "email us" wording back on
		# the affiliate page. It never touches anyone who has already signed up.
		"selfSignup": True,
		# Whether a confirmed email address is enough to start earning.
		#
		# On by default because the whole point is that nobody has to do anything.
		# The thing to know before leaving it on: an open programme attracts
		# coupon sites, people who bid on the brand name in ads, and self-referral
		# — none of which cost anything until a sale, all of which the owner can
		# stop afterwards by disabling the code. Turning this off puts every new
		# application in a queue on the dashboard instead, which is safer and is
		# work.
		"autoApprove": True,
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
	for field in ("installerUrl", "macUrl"):
		link = downloads.get(field, "")
		if link and not link.startswith("https://"):
			raise Invalid(f"{field} must be an https:// link.")
	if downloads.get("macEnabled") and not downloads.get("macUrl"):
		raise Invalid("Turning on the Mac download needs a link to the Mac build.")

	version = s.get("version", {})
	latest = str(version.get("latest", "") or "").strip()
	if latest:
		# The app compares this numerically against its own version — see
		# parse_version in the desktop app's core/updates.py, which pulls the
		# digits out and compares them as numbers so 1.10 beats 1.9. Anything
		# without a digit in it compares as (0,) and would tell every customer
		# they are ahead of the latest release, which is silence, not an error.
		if not re.fullmatch(r"v?\d+(\.\d+){0,3}", latest):
			raise Invalid(
				f"'{latest}' is not a version number. Write it like 1.4.0 — the app "
				"compares the digits, and anything else quietly means 'no update'."
			)

	notes = str(version.get("notes", "") or "")
	if len(notes) > 1500:
		# The app shows these in a small panel and truncates GitHub's at the same
		# length. Refusing here is kinder than silently cutting a sentence in half
		# on the customer's screen.
		raise Invalid(f"Release notes are {len(notes)} characters. Keep them under 1500.")

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


def _public_version(s: dict) -> dict:
	"""What an installed copy of the desktop app is told about newer ones.

	Two conditions silence it, and both are the same judgement: never point a
	customer at a download that is not there. `announce` off is someone deciding
	that deliberately, usually because a build turned out to be bad. Downloads
	being off is the same decision made on the site — it would be strange for the
	page to say "paused while we ship an update" while the app queued people up to
	fetch it anyway.

	The installer links come from `downloads` rather than being typed again here.
	They are the same two files, and a second copy of a URL is a second chance for
	one of them to be wrong.
	"""
	version = s.get("version", DEFAULTS["version"])
	downloads = s["downloads"]
	quiet = not version.get("announce", True) or not downloads["enabled"]
	return {
		"latest": "" if quiet else str(version.get("latest", "") or "").strip(),
		"notes": "" if quiet else str(version.get("notes", "") or ""),
		"winUrl": downloads["installerUrl"],
		"macUrl": downloads["macUrl"] if downloads["macEnabled"] else "",
	}


def public() -> dict:
	"""What the marketing site is allowed to read, unauthenticated.

	Everything here ends up in public HTML anyway, so there is nothing to hide —
	but it is built explicitly rather than dumping the settings row, so that a
	setting added later is not published by accident.
	"""
	s = get()
	price, downloads, aff, notice = s["price"], s["downloads"], s["affiliates"], s["notice"]
	return {
		"version": _public_version(s),
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
			# No zipUrl. A settings row saved before this still has one — _merge
			# keeps unknown keys — and it is simply not published, which is the
			# point of building this dict by hand rather than dumping the row.
			"offMessage": downloads["offMessage"],
			"macEnabled": bool(downloads["macEnabled"]),
			"macUrl": downloads["macUrl"],
			"macSize": downloads["macSize"],
		},
		"affiliates": {
			"enabled": bool(aff["enabled"]),
			"ratePct": aff["ratePct"],
			"holdDays": aff["holdDays"],
			# The site reads this to decide whether the affiliate page shows a
			# sign-up form or the old "email us" paragraph. It is published rather
			# than assumed, because a form that posts to a closed endpoint is worse
			# than no form: it takes somebody's details and then refuses them.
			"selfSignup": bool(aff["enabled"]) and bool(aff.get("selfSignup", True)),
		},
		"notice": notice if notice.get("enabled") else {"enabled": False, "text": "", "tone": "info"},
	}
