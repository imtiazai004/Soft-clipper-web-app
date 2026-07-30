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
		# Naskh sits flat rather than sloping, so it needs less of both than
		# Nastaliq does. Same method.
		"size_scale": 1.5,
		"glyph_width": 0.19,
	},
}

DEFAULT_FONT = "System"

# What a Latin caption measures, and the baseline both numbers below are
# relative to. 0.62 is the average glyph advance as a fraction of the point size
# for bold Arial capitals, which is what captions are drawn as.
DEFAULT_SIZE_SCALE = 1.0
DEFAULT_GLYPH_WIDTH = 0.62


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
	out = []
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
