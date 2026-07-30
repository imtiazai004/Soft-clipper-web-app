"""The fonts captions can be drawn in, and where they live.

Captions were burnt in with Arial and nothing else. On a Latin-script clip that
is merely plain; on an Urdu one it is a broken promise. The site says Soft
Clipper captions Urdu and Pashto, and Arial has no Nastaliq at all — the shapes
it does have for Arabic script are the wrong ones, joined wrongly, and on a
machine without even those the renderer draws empty boxes. Nobody would ship a
clip that came out like that, so for those customers the feature did not exist.

So a handful of fonts travel with the app. Bundled rather than named-and-hoped-
for, because a font referenced by name is a font the customer might not have:
libass silently substitutes something else and the clip renders in a typeface
nobody chose. What is bundled is what gets used, on both platforms, every time.

All of them are SIL Open Font License, which permits bundling inside an
application and selling it. The condition is that the licence travels with the
font, which is what the OFL-*.txt files beside them are for. Do not remove them.

`System` is first and is the default, and it is the old behaviour exactly:
Arial, whatever the machine has. Every clip rendered before this existed used
it, and every saved clip record without a font still does.
"""
from __future__ import annotations

import os
import sys

# The family name is what goes into the ASS Fontname field, and it has to be the
# name inside the file rather than the filename — libass matches on the former.
# These were read out of the fonts' own name tables, not guessed.
#
# Weight matters as much as shape here. Captions are drawn bold, and a variable
# font whose default instance is thin renders a hairline the Bold flag then has
# to fake. Montserrat was dropped from this list for exactly that reason: its
# variable file reports itself as "Montserrat Thin".
CAPTION_FONTS: dict[str, dict] = {
	"System": {
		"family": "Arial",
		"file": "",
		"label": "System default",
		"note": "What every clip used before there was a choice.",
	},
	"Anton": {
		"family": "Anton",
		"file": "Anton-Regular.ttf",
		"label": "Anton — heavy condensed",
		"note": "The tall, tight, shouting caps most short-form clips use.",
	},
	"Bebas Neue": {
		"family": "Bebas Neue",
		"file": "BebasNeue-Regular.ttf",
		"label": "Bebas Neue — condensed",
		"note": "Anton's lighter cousin. Fits more words on a line.",
	},
	"Poppins": {
		"family": "Poppins",
		"file": "Poppins-Bold.ttf",
		"label": "Poppins — round and clean",
		"note": "Geometric and friendly. Reads well at small sizes.",
	},
	"Noto Nastaliq Urdu": {
		"family": "Noto Nastaliq Urdu",
		"file": "NotoNastaliqUrdu-Variable.ttf",
		"label": "Urdu — Nastaliq",
		"note": "The script Urdu is actually written in. Use this for Urdu clips.",
		"rtl": True,
		"script": "nastaliq",
		# Measured, not guessed — see size_scale() and glyph_width() below for
		# how, and for what the first guessed pair got wrong.
		"size_scale": 2.0,
		"glyph_width": 0.11,
	},
	"Noto Naskh Arabic": {
		"family": "Noto Naskh Arabic",
		"file": "NotoNaskhArabic-Variable.ttf",
		"label": "Arabic / Pashto — Naskh",
		"note": "For Arabic, Pashto, Persian and Sindhi.",
		"rtl": True,
		"script": "arabic",
		# Naskh sits flat rather than sloping, so it needs less of both than
		# Nastaliq does. Same method.
		"size_scale": 1.5,
		"glyph_width": 0.19,
	},

	# ── the rest of the scripts ───────────────────────────────────────────────
	# Bundling Urdu and stopping there was a market talking, not a decision: the
	# app transcribes in any language and the site names Hindi by name. Every one
	# of these is a script Arial cannot draw, which is the same bug that made an
	# Urdu clip a row of empty boxes.
	#
	# On Windows most of these already worked, quietly, because Windows ships
	# Nirmala UI and libass falls back to it per glyph. Two places they did not:
	# a machine without it, and the web app's container, which has Liberation and
	# nothing else — so every Hindi caption that server has ever burnt in was
	# blank. Bundling them makes the answer the same everywhere.
	#
	# Metrics measured the same way as the two above.
	"Noto Sans Devanagari": {
		"family": "Noto Sans Devanagari",
		"file": "NotoSansDevanagari-Variable.ttf",
		"label": "Hindi / Marathi / Nepali",
		"note": "Devanagari.",
		"script": "devanagari",
		"size_scale": 1.15,
		"glyph_width": 0.22,
	},
	"Noto Sans Bengali": {
		"family": "Noto Sans Bengali",
		"file": "NotoSansBengali-Variable.ttf",
		"label": "Bengali / Assamese",
		"note": "Bangla.",
		"script": "bengali",
		"size_scale": 1.15,
		"glyph_width": 0.32,
	},
	"Noto Sans Gurmukhi": {
		"family": "Noto Sans Gurmukhi",
		"file": "NotoSansGurmukhi-Variable.ttf",
		"label": "Punjabi (Gurmukhi)",
		"note": "For Punjabi written in India. Punjabi in Pakistan uses Nastaliq.",
		"script": "gurmukhi",
		"size_scale": 1.15,
		"glyph_width": 0.34,
	},
	"Noto Sans Gujarati": {
		"family": "Noto Sans Gujarati",
		"file": "NotoSansGujarati-Variable.ttf",
		"label": "Gujarati",
		"note": "",
		"script": "gujarati",
		"size_scale": 1.1,
		"glyph_width": 0.33,
	},
	"Noto Sans Tamil": {
		"family": "Noto Sans Tamil",
		"file": "NotoSansTamil-Variable.ttf",
		"label": "Tamil",
		"note": "",
		"script": "tamil",
		"size_scale": 1.1,
		"glyph_width": 0.46,
	},
	"Noto Sans Telugu": {
		"family": "Noto Sans Telugu",
		"file": "NotoSansTelugu-Variable.ttf",
		"label": "Telugu",
		"note": "",
		"script": "telugu",
		"size_scale": 1.2,
		"glyph_width": 0.30,
	},
	"Noto Sans Thai": {
		"family": "Noto Sans Thai",
		"file": "NotoSansThai-Variable.ttf",
		"label": "Thai",
		"note": "",
		"script": "thai",
		"size_scale": 1.15,
		"glyph_width": 0.27,
	},
}

DEFAULT_FONT = "System"

# What a Latin caption measures, and the baseline both numbers below are
# relative to. 0.62 is the average glyph advance as a fraction of the point size
# for bold Arial capitals, which is what captions are drawn as.
DEFAULT_SIZE_SCALE = 1.0
DEFAULT_GLYPH_WIDTH = 0.62



# ── choosing one without being asked ──────────────────────────────────────────
# Unicode blocks, by the script they belong to. Enough to tell one writing system
# from another, which is all that is being asked — not a language detector.
SCRIPT_RANGES = (
	("devanagari", 0x0900, 0x097F),
	("bengali", 0x0980, 0x09FF),
	("gurmukhi", 0x0A00, 0x0A7F),
	("gujarati", 0x0A80, 0x0AFF),
	("tamil", 0x0B80, 0x0BFF),
	("telugu", 0x0C00, 0x0C7F),
	("thai", 0x0E00, 0x0E7F),
	("arabic", 0x0600, 0x06FF),
	("arabic", 0x0750, 0x077F),
	("arabic", 0xFB50, 0xFDFF),
	("arabic", 0xFE70, 0xFEFF),
)

AUTO_FONT = "Auto"

# Which font wins for a script. Arabic-script text is the one genuinely
# ambiguous case: Urdu is written in Nastaliq and Arabic, Pashto and Persian in
# Naskh, and the letters are the same. Nastaliq is the choice because Urdu is
# who this app is used by, and because a Naskh reader can read Nastaliq — the
# picker is there for anyone who wants the other.
SCRIPT_FONTS = {
	"devanagari": "Noto Sans Devanagari",
	"bengali": "Noto Sans Bengali",
	"gurmukhi": "Noto Sans Gurmukhi",
	"gujarati": "Noto Sans Gujarati",
	"tamil": "Noto Sans Tamil",
	"telugu": "Noto Sans Telugu",
	"thai": "Noto Sans Thai",
	"arabic": "Noto Nastaliq Urdu",
}


def script_of(text: str) -> str:
	"""The writing system most of this text is in, or "" for Latin and the rest.

	Counted rather than decided on the first non-Latin character, because one
	borrowed word or a stray character should not change the typeface of a whole
	clip. Latin is not a candidate: a caption that is mostly Hindi with an
	English brand name in it is a Hindi caption.
	"""
	counts: dict[str, int] = {}
	for ch in str(text or ""):
		code = ord(ch)
		for name, low, high in SCRIPT_RANGES:
			if low <= code <= high:
				counts[name] = counts.get(name, 0) + 1
				break
	if not counts:
		return ""
	return max(counts, key=counts.get)


def for_text(text: str) -> str:
	"""The font this text should be drawn in. DEFAULT_FONT for Latin.

	The point of it: nobody should have to know the word "Nastaliq" to caption a
	video in Urdu. The picker still exists, and a chosen font always wins — this
	is only what happens when nobody has chosen.
	"""
	return SCRIPT_FONTS.get(script_of(text), DEFAULT_FONT)


def choose(name: str | None, text: str = "") -> str:
	"""Resolve a font setting, following Auto to whatever the text needs."""
	if str(name or "") == AUTO_FONT:
		return for_text(text)
	return str(name or DEFAULT_FONT)

def _bundle_root() -> str:
	"""The folder the app's own files were unpacked into.

	Two answers, because there are two ways this runs. Frozen, PyInstaller puts
	data files under _MEIPASS and the source tree is not there at all. From
	source, it is the repository. backend/main.py works this out the same way
	for the frontend and ffmpeg; this cannot import that without dragging the
	whole web app in, so it repeats the two lines instead.
	"""
	if getattr(sys, "frozen", False):
		return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
	return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fonts_dir() -> str:
	"""Where the bundled fonts are, or "" if they are not there.

	Empty is a supported answer and not an error. Running from a source checkout
	that has not fetched them, the app carries on and captions come out in
	whatever the system provides — which is precisely the old behaviour.
	"""
	path = os.path.join(_bundle_root(), "assets", "fonts")
	return path if os.path.isdir(path) else ""


def resolve(name: str | None) -> str:
	"""A font choice — or None, or something unrecognised — to an ASS family name.

	Unknown names fall back to Arial rather than raising. A clip record saved by
	an older version has no font at all, and a record naming a font that has
	since been dropped should still render; in both cases the right outcome is
	the clip that customer already had, not a failed job.
	"""
	entry = CAPTION_FONTS.get(str(name or ""), CAPTION_FONTS[DEFAULT_FONT])
	return entry["family"]


def is_rtl(name: str | None) -> bool:
	"""Whether this font is for a right-to-left script."""
	return bool(CAPTION_FONTS.get(str(name or ""), {}).get("rtl"))


def size_scale(name: str | None) -> float:
	"""Point-size multiplier so this font draws as large as Latin does. 1.0 for most.

	Point size is not visual size. A Nastaliq face reserves a very tall em box
	for its slope and its diacritics, so the letterforms fill a small part of it:
	at the size that makes Latin capitals fill a caption, Urdu came out about a
	quarter of the height — legible on a monitor, invisible on a phone.

	The first version of this guessed, and guessed the wrong way round, shrinking
	the script that needed enlarging. These are measured: the same caption
	rendered at a range of sizes and the drawn pixels counted, then compared
	against a Latin caption until the letterforms carried the same weight on the
	frame. Matching total ink height alone is not enough and was the second wrong
	answer — most of a Nastaliq line's height is slope and diacritics, so a line
	that measures as tall as Latin still reads far smaller.
	"""
	entry = CAPTION_FONTS.get(str(name or ""), {})
	return float(entry.get("size_scale", DEFAULT_SIZE_SCALE))


def glyph_width(name: str | None) -> float:
	"""Average glyph advance as a fraction of the point size.

	How many characters fit on a line, which is a different question from how
	tall they are and has a very different answer per script. Arabic script is
	cursive and joins: measured against bold Latin capitals, Nastaliq is about
	six times narrower per character and Naskh about three. Using the Latin
	figure for them broke lines after three or four words, so an Urdu caption
	arrived cut in half.

	The measurement that produced these also produced 0.594 for bold Latin
	capitals, against the 0.62 this module has used since long before any of it
	existed — which is the reassurance that the method is sound rather than the
	numbers being fitted to what was wanted.
	"""
	entry = CAPTION_FONTS.get(str(name or ""), {})
	return float(entry.get("glyph_width", DEFAULT_GLYPH_WIDTH))


def available() -> list[dict]:
	"""What the picker should offer, with the bundled ones marked.

	`present` is checked rather than assumed so the UI can be honest in a source
	checkout where the files were never fetched: a font that is not there is
	still listed, and still selectable, but the app knows it will be substituted.
	"""
	folder = fonts_dir()
	out = [{
		"name": AUTO_FONT,
		"label": "Auto — match the language",
		"note": "Picks the right script from the words themselves. Leave this on "
		        "unless you want a particular look.",
		"rtl": False,
		"bundled": False,
		"present": True,
	}]
	for name, entry in CAPTION_FONTS.items():
		bundled = bool(entry["file"])
		out.append({
			"name": name,
			"label": entry["label"],
			"note": entry["note"],
			"rtl": bool(entry.get("rtl")),
			"bundled": bundled,
			"present": not bundled or bool(folder and os.path.isfile(os.path.join(folder, entry["file"]))),
		})
	return out
