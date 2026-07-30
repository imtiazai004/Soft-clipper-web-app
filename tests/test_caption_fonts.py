"""Caption typefaces, and the caption styles that grew alongside them.

The bug behind all of this: captions were burnt in as Arial and nothing else,
while the website said the app captions Urdu. Arial has no Nastaliq, so those
customers got the wrong shapes or empty boxes from a render that exited 0.

Most of these are pure — a function in, a value out. The ones behind the ffmpeg
skip are the ones that matter, because whether libass really found a bundled
font is not something a unit test can assert about a string.
"""
import os
import shutil
import subprocess

import pytest

from core import captions, fonts, video

needs_ffmpeg = pytest.mark.skipif(
	not shutil.which("ffmpeg"), reason="needs ffmpeg on PATH"
)

URDU = "یہ ایک اردو جملہ ہے"
PASHTO = "دا یوه پښتو جمله ده"


# ── the promise that nothing already rendered moves ───────────────────────────
def test_no_font_is_the_arial_every_clip_was_rendered_with():
	"""The compatibility promise. A clip record saved before fonts existed has no
	font in it, and re-rendering it must produce the same file it did before."""
	assert fonts.resolve(None) == "Arial"
	assert fonts.resolve("") == "Arial"
	assert fonts.resolve("System") == "Arial"


def test_an_unknown_font_falls_back_rather_than_failing():
	"""A record naming a font that has since been dropped should still render.
	A failed job helps nobody; the clip they already had does."""
	assert fonts.resolve("Comic Sans Deluxe") == "Arial"
	assert fonts.size_scale("Comic Sans Deluxe") == 1.0
	assert fonts.glyph_width("Comic Sans Deluxe") == fonts.DEFAULT_GLYPH_WIDTH


def test_the_default_font_changes_nothing_about_size_or_wrapping(tmp_path):
	before = captions.build_ass(
		[{"start": 0.0, "duration": 2.0, "text": "hello world this is a caption"}],
		str(tmp_path / "a.ass"), ratio="9:16",
	)
	after = captions.build_ass(
		[{"start": 0.0, "duration": 2.0, "text": "hello world this is a caption"}],
		str(tmp_path / "b.ass"), ratio="9:16", font="System",
	)
	assert open(before, encoding="utf-8").read() == open(after, encoding="utf-8").read()


# ── the metrics, which were twice wrong before they were measured ─────────────
def test_urdu_is_drawn_larger_not_smaller():
	"""The first guess shrank the script that needed enlarging. Point size is not
	visual size: a Nastaliq face reserves a tall em box for its slope, so at the
	size that fills a caption with Latin capitals, Urdu came out a quarter as
	tall."""
	assert fonts.size_scale("Noto Nastaliq Urdu") > 1.5
	assert fonts.size_scale("Noto Naskh Arabic") > 1.0
	assert fonts.size_scale("Anton") == 1.0


def test_arabic_script_fits_far_more_characters_on_a_line():
	"""Using the Latin figure broke an Urdu line after three words, so the caption
	arrived cut in half."""
	assert fonts.glyph_width("Noto Nastaliq Urdu") < fonts.DEFAULT_GLYPH_WIDTH / 4
	assert fonts.glyph_width("Noto Naskh Arabic") < fonts.DEFAULT_GLYPH_WIDTH / 2


def test_the_line_budget_still_answers_what_it_always_did():
	"""line_budget grew a parameter. Every existing caller passes two arguments
	and must get the number it got before."""
	assert captions.line_budget(17, 162) == captions.line_budget(17, 162, captions._GLYPH_WIDTH)
	wide = captions.line_budget(17, 162, fonts.glyph_width("Noto Nastaliq Urdu"))
	assert wide > captions.line_budget(17, 162) * 3


def test_a_bigger_font_scale_reaches_the_ass_file(tmp_path):
	path = captions.build_ass(
		[{"start": 0.0, "duration": 2.0, "text": URDU}],
		str(tmp_path / "u.ass"), ratio="9:16", font="Noto Nastaliq Urdu",
	)
	style = [l for l in open(path, encoding="utf-8") if l.startswith("Style: Default")][0]
	name, size = style.split(",")[1], int(style.split(",")[2])
	assert name == "Noto Nastaliq Urdu"
	assert size > captions.CAPTION_STYLES["TikTok Bold"]["fontsize"]


# ── the bundle ────────────────────────────────────────────────────────────────
def test_every_bundled_font_is_really_in_the_repository():
	"""Names are cheap. A font entry claiming a file that is not there renders as
	a silent substitution, which is the whole failure this set out to fix."""
	folder = fonts.fonts_dir()
	assert folder, "assets/fonts is missing from the checkout"
	for name, entry in fonts.CAPTION_FONTS.items():
		if entry["file"]:
			assert os.path.isfile(os.path.join(folder, entry["file"])), name


def test_the_open_font_licences_travel_with_the_fonts():
	"""Bundling these is permitted on the condition that the licence goes with
	them. Deleting one to save 4 KB would make the app's distribution unlicensed."""
	folder = fonts.fonts_dir()
	licences = [f for f in os.listdir(folder) if f.startswith("OFL")]
	bundled = {n for n, e in fonts.CAPTION_FONTS.items() if e["file"]}
	assert len(licences) >= len(bundled), f"{len(bundled)} fonts but {len(licences)} licences"


def test_the_filter_points_ffmpeg_at_our_fonts(tmp_path):
	ass = tmp_path / "x.ass"
	ass.write_text("[Script Info]\n", encoding="utf-8")
	built = video._ass_filter(str(ass))
	assert built.startswith("ass=f=")
	assert "fontsdir=" in built, "without this libass cannot see the bundled fonts"


def test_available_reports_what_is_actually_present():
	listing = {f["name"]: f for f in fonts.available()}
	assert listing["System"]["present"] is True
	assert listing["Noto Nastaliq Urdu"]["rtl"] is True
	assert listing["Anton"]["rtl"] is False
	assert all(f["label"] and f["note"] is not None for f in listing.values())


# ── the styles that grew ──────────────────────────────────────────────────────
def test_every_style_survives_being_asked_for(tmp_path):
	"""A style is only a row in a table, which is why there can be twenty. What
	that makes cheap is also what makes a typo in one of them easy to miss."""
	for name in captions.CAPTION_STYLES:
		path = captions.build_ass(
			[{"start": 0.0, "duration": 2.0, "text": "one two three"}],
			str(tmp_path / f"{name}.ass"), style=name, ratio="9:16",
		)
		assert path, name
		header = open(path, encoding="utf-8").read()
		assert "Style: Default" in header and "{name}" not in header


def test_the_old_style_names_still_render():
	"""Clip records name their style. The two renamed ones must keep working."""
	for old in captions.CAPTION_ALIASES:
		assert captions.caption_style(old) is not captions.CAPTION_STYLES["TikTok Bold"] \
			or captions.CAPTION_ALIASES[old] == "TikTok Bold"


def test_the_served_style_list_is_paintable():
	"""ASS stores colour backwards, with an alpha byte in front. Getting the
	conversion wrong does not fail — it swaps red and blue."""
	listing = captions.style_list()
	assert len(listing) == len(captions.CAPTION_STYLES)
	for entry in listing:
		for key in ("colour", "highlight", "outline"):
			assert entry[key].startswith("#") and len(entry[key]) == 7, entry


def test_ass_colours_convert_the_right_way_round():
	# &H00BBGGRR — this one is pure red in ASS and must not come back as blue.
	assert captions._ass_to_hex("&H000000FF") == "#ff0000"
	assert captions._ass_to_hex("&H00FF0000") == "#0000ff"
	assert captions._ass_to_hex("&H00FFFFFF") == "#ffffff"
	assert captions._ass_to_hex("nonsense") == "#ffffff"


# ── the part only ffmpeg can answer ───────────────────────────────────────────
@needs_ffmpeg
@pytest.mark.parametrize("text,font,family", [
	(URDU, "Noto Nastaliq Urdu", "NotoNastaliqUrdu"),
	(PASHTO, "Noto Naskh Arabic", "NotoNaskhArabic"),
	("hello world", "Anton", "Anton"),
])
def test_libass_really_uses_the_bundled_font(tmp_path, text, font, family):
	"""The decisive step, and the only one that proves any of this.

	ffmpeg exits 0 whether it drew the script or a row of empty boxes, so the
	assertion is on what libass says it chose. It logs every font it resolves;
	if the bundle were not reachable it would fall back to a system face and the
	name below would not appear.
	"""
	ass = captions.build_ass(
		[{"start": 0.0, "duration": 1.0, "text": text}],
		str(tmp_path / "c.ass"), ratio="9:16", font=font,
	)
	out = tmp_path / "out.mp4"
	r = subprocess.run(
		["ffmpeg", "-y", "-v", "verbose",
		 "-f", "lavfi", "-i", "color=c=black:size=1080x1920:rate=25:duration=1",
		 "-vf", video._ass_filter(ass), "-frames:v", "25", str(out)],
		capture_output=True, text=True,
	)
	assert r.returncode == 0, r.stderr[-800:]
	assert out.stat().st_size > 1000

	chosen = [l for l in r.stderr.splitlines() if "fontselect" in l]
	assert chosen, "libass logged no font selection at all"
	assert any(family in l for l in chosen), \
		f"libass did not use the bundled {family}:\n" + "\n".join(chosen[:5])


@needs_ffmpeg
def test_an_urdu_caption_is_not_a_row_of_empty_boxes(tmp_path):
	"""The failure this whole feature exists to prevent, asserted directly.

	libass reports a glyph it cannot draw. With the right font bundled there
	should be none — with Arial, every Nastaliq letter is one.
	"""
	ass = captions.build_ass(
		[{"start": 0.0, "duration": 1.0, "text": URDU}],
		str(tmp_path / "u.ass"), ratio="9:16", font="Noto Nastaliq Urdu",
	)
	r = subprocess.run(
		["ffmpeg", "-y", "-v", "verbose",
		 "-f", "lavfi", "-i", "color=c=black:size=1080x1920:rate=25:duration=1",
		 "-vf", video._ass_filter(ass), "-frames:v", "5", str(tmp_path / "o.mp4")],
		capture_output=True, text=True,
	)
	missing = [l for l in r.stderr.splitlines() if "not found" in l and "Glyph" in l]
	assert not missing, "Nastaliq glyphs were missing:\n" + "\n".join(missing[:5])
