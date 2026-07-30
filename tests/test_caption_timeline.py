"""The caption timeline the live preview draws from.

The whole value of the preview is that it shows what the export will show, and
the only thing making that true is that both come out of this one function. So
what is worth testing is not that the timeline looks sensible — it is that it
still *is* what build_ass burns in.
"""
import pytest

from core import captions, fonts

SEGS = [
	{"start": 0.0, "duration": 3.0, "text": "the quick brown fox jumps over the lazy dog"},
	{"start": 4.0, "duration": 2.5, "text": "and then it says something else entirely"},
]


def _dialogue(path):
	return [l for l in open(path, encoding="utf-8") if l.startswith("Dialogue: 0,")]


def test_the_timeline_is_what_the_renderer_burns_in(tmp_path):
	"""The promise the preview rests on. If these ever diverge, the preview is
	showing a clip that will not be exported — worse than showing nothing,
	because someone will trust it."""
	path = captions.build_ass(SEGS, str(tmp_path / "a.ass"), ratio="9:16", words_per_line=3)
	timeline = captions.caption_timeline(SEGS, words_per_line=3, ratio="9:16")

	assert len(timeline) == len(_dialogue(path))
	for line, dialogue in zip(timeline, _dialogue(path)):
		assert line["text"].upper() in dialogue.upper()
		assert captions._ass_time(line["start"]) in dialogue
		assert captions._ass_time(line["end"]) in dialogue


@pytest.mark.parametrize("style", list(captions.CAPTION_STYLES)[:6])
@pytest.mark.parametrize("ratio", ["9:16", "1:1", "16:9"])
def test_they_agree_across_styles_and_ratios(tmp_path, style, ratio):
	"""Chunking depends on the style's size and the canvas shape, so a count that
	matches for one combination proves very little on its own."""
	path = captions.build_ass(SEGS, str(tmp_path / "b.ass"), style=style, ratio=ratio)
	assert len(captions.caption_timeline(SEGS, style=style, ratio=ratio)) == len(_dialogue(path))


def test_they_agree_for_a_script_with_different_metrics(tmp_path):
	"""Urdu fits about six times more characters on a line. If the preview used
	the Latin figure it would break lines the export does not."""
	urdu = [{"start": 0.0, "duration": 3.0, "text": "یہ ایک اردو جملہ ہے جو کافی لمبا ہے"}]
	path = captions.build_ass(urdu, str(tmp_path / "u.ass"), ratio="9:16", font="Noto Nastaliq Urdu")
	timeline = captions.caption_timeline(urdu, ratio="9:16", font="Noto Nastaliq Urdu")
	assert len(timeline) == len(_dialogue(path))
	assert timeline, "an Urdu caption must produce something to draw"


def test_word_fixes_reach_the_preview_too(tmp_path):
	"""Someone correcting a name wants to see the correction, not find out after
	the render that only the export got it."""
	segs = [{"start": 0.0, "duration": 2.0, "text": "hello imtiaz"}]
	timeline = captions.caption_timeline(segs, overrides={"imtiaz": "Imtiaz Ahmad"})
	assert "Imtiaz Ahmad" in " ".join(l["text"] for l in timeline)


def test_the_times_are_ordered_and_do_not_overlap():
	"""Two captions on screen at once is what the preview would show if these
	ran together, and it would be right to — so they must not."""
	timeline = captions.caption_timeline(SEGS, words_per_line=2)
	for a, b in zip(timeline, timeline[1:]):
		assert a["end"] <= b["start"] + 1e-6, (a, b)
		assert a["end"] > a["start"]


def test_no_captions_is_an_empty_timeline_not_a_failure():
	assert captions.caption_timeline([]) == []
	assert captions.caption_timeline(None) == []


# ── the metrics the canvas draws with ─────────────────────────────────────────
def test_the_metrics_describe_the_style_the_renderer_will_use():
	m = captions.caption_metrics("Boxed", "9:16", None)
	assert m["boxed"] is True
	assert m["family"] == "Arial"
	assert m["play_x"] == 162 and m["play_y"] == 288
	assert m["colour"].startswith("#") and m["outline"].startswith("#")


def test_the_metrics_follow_the_font_the_way_the_ass_file_does(tmp_path):
	"""Size and family both change with the font, and the preview has to change
	with them or Urdu is drawn a quarter of the size it exports at."""
	m = captions.caption_metrics("TikTok Bold", "9:16", "Noto Nastaliq Urdu")
	path = captions.build_ass(SEGS, str(tmp_path / "c.ass"), ratio="9:16", font="Noto Nastaliq Urdu")
	style_line = [l for l in open(path, encoding="utf-8") if l.startswith("Style: Default")][0]
	assert style_line.split(",")[1] == m["family"]
	assert int(style_line.split(",")[2]) == m["fontsize"]
	assert m["rtl"] is True


def test_the_ratio_changes_the_canvas_the_preview_maps_onto():
	assert captions.caption_metrics(ratio="1:1")["play_x"] == 288
	assert captions.caption_metrics(ratio="16:9")["play_x"] == 512
	assert captions.caption_metrics(ratio=None)["play_x"] == 162


def test_an_unknown_font_still_produces_drawable_metrics():
	m = captions.caption_metrics("TikTok Bold", "9:16", "Nonexistent Face")
	assert m["family"] == "Arial"
	assert m["fontsize"] > 0
	assert fonts.glyph_width("Nonexistent Face") == fonts.DEFAULT_GLYPH_WIDTH
