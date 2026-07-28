"""Word-by-word caption highlighting and the two styles added with it.

The rule this file exists to enforce: turning highlighting on must be the only
thing that changes. With it off, the file written today has to be byte-for-byte
what the previous build wrote, because customers re-render old clips.

    .venv\\Scripts\\python.exe -m pytest tests/test_karaoke_captions.py -q
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import captions  # noqa: E402

SEGMENTS = [
	{"start": 0.0, "duration": 3.0, "text": "this is the hook"},
	{"start": 3.0, "duration": 3.0, "text": "and this is the payoff line"},
]


def dialogues(path: str) -> list[str]:
	return [l.rstrip("\n") for l in open(path, encoding="utf-8") if l.startswith("Dialogue:")]


def text_of(line: str) -> str:
	return line.rsplit(",,", 1)[1]


def build(tmp_path, name="c.ass", **kwargs) -> str:
	return captions.build_ass(SEGMENTS, str(tmp_path / name), **kwargs)


# ── nothing changes when it is off ───────────────────────────────────────────


def test_highlighting_off_is_the_default(tmp_path):
	explicit = open(build(tmp_path, "a.ass", highlight=False), encoding="utf-8").read()
	implicit = open(build(tmp_path, "b.ass"), encoding="utf-8").read()
	assert explicit == implicit


def test_with_highlighting_off_each_chunk_is_still_one_line(tmp_path):
	lines = dialogues(build(tmp_path, words_per_line=2))
	assert len(lines) == 5      # 4 words -> 2 chunks, 6 words -> 3 chunks
	assert all("\\c&H" not in text_of(l) for l in lines), "no colour overrides when off"


# ── what it does when it is on ───────────────────────────────────────────────


def test_highlighting_produces_one_line_per_word(tmp_path):
	off = dialogues(build(tmp_path, "off.ass", words_per_line=4))
	on = dialogues(build(tmp_path, "on.ass", words_per_line=4, highlight=True))
	assert len(on) == 10        # every word in both segments gets its own step
	assert len(on) > len(off)


def test_the_whole_phrase_stays_on_screen(tmp_path):
	"""Only the colour moves. Showing one word at a time would lose the phrase.

	"Phrase" means the chunk, not the whole segment — chunks are limited by how
	much fits on one line, so a four-word segment may be two chunks.
	"""
	line = dialogues(build(tmp_path, words_per_line=4, highlight=True))[0]
	body = text_of(line)
	assert len([w for w in ("THIS", "IS", "THE", "HOOK") if w in body]) >= 2


def test_exactly_one_word_is_highlighted_per_line(tmp_path):
	st = captions.CAPTION_STYLES["TikTok Bold"]
	for line in dialogues(build(tmp_path, words_per_line=4, highlight=True)):
		assert text_of(line).count(st["highlight"]) == 1


def test_the_highlight_walks_forward_through_the_chunk(tmp_path):
	"""Within one chunk the lit word moves left to right. It resets at the start
	of the next chunk, so compare only lines that share the same words."""
	lines = dialogues(build(tmp_path, words_per_line=4, highlight=True))
	active = captions.CAPTION_STYLES["TikTok Bold"]["highlight"]

	def words_of(line):
		# Strip every override block, not just the colour, or two lines from the
		# same chunk never compare equal — the reset tag differs.
		return re.sub(r"\{[^}]*\}", "", text_of(line)).split()

	first_chunk = [l for l in lines if words_of(l) == words_of(lines[0])]
	positions = [text_of(l).index(active) for l in first_chunk]
	assert len(positions) > 1
	assert positions == sorted(positions), "the highlight jumped backwards"


def test_word_timings_stay_inside_the_chunk(tmp_path):
	"""Every step must fit in the segment it belongs to, or the captions drift."""
	lines = dialogues(build(tmp_path, words_per_line=4, highlight=True))
	starts = [l.split(",")[1] for l in lines]
	ends = [l.split(",")[2] for l in lines]
	assert starts == sorted(starts)
	assert ends[-1] <= captions._ass_time(6.0)
	for i in range(len(lines) - 1):
		assert ends[i] <= starts[i + 1] or ends[i] == starts[i + 1]


def test_a_longer_word_gets_more_time(tmp_path):
	"""Timings are split by character count, which tracks speech better than an
	even split — 'extraordinarily' is not as quick to say as 'a'."""
	lines = captions.build_ass(
		[{"start": 0.0, "duration": 4.0, "text": "a extraordinarily"}],
		str(tmp_path / "w.ass"),
		words_per_line=2,
		highlight=True,
	)
	rows = dialogues(lines)
	first = rows[0].split(",")[1:3]
	second = rows[1].split(",")[1:3]
	assert second[1] > second[0]
	# The second word's window must be the longer of the two.
	assert (second[1] > first[1]) and (second[0] == first[1])


def test_special_characters_are_escaped_in_highlighted_lines(tmp_path):
	"""The same trap as the plain path: a `{` in the transcript would swallow
	the rest of the caption."""
	out = captions.build_ass(
		[{"start": 0, "duration": 2, "text": r"cost {x} 50\50"}],
		str(tmp_path / "s.ass"),
		highlight=True,
	)
	bodies = [text_of(l) for l in dialogues(out)]
	for body in bodies:
		assert "{x}" not in body.lower()
	# The escaped brace survives somewhere — which chunk depends on line width.
	assert any("(X)" in b for b in bodies)


# ── the two new styles ───────────────────────────────────────────────────────


def test_the_new_styles_exist_and_carry_a_highlight_colour():
	for name in ("Bounce", "Boxed"):
		assert name in captions.CAPTION_STYLES
		assert captions.CAPTION_STYLES[name]["highlight"].startswith("&H")


def border_style_of(path: str) -> str:
	"""Read the Default style's BorderStyle by name rather than by a magic index
	— the ASS Format line says where each field is, so use it."""
	body = open(path, encoding="utf-8").read().splitlines()
	fields = [f.strip() for f in [l for l in body if l.startswith("Format:") ][0][len("Format:"):].split(",")]
	values = [l for l in body if l.startswith("Style: Default")][0][len("Style:"):].split(",")
	return values[fields.index("BorderStyle")].strip()


def test_boxed_draws_an_opaque_box_behind_the_words(tmp_path):
	"""BorderStyle 3 — the only thing that keeps captions readable over a
	bright, busy frame."""
	assert border_style_of(build(tmp_path, style="Boxed")) == "3"


def test_the_original_styles_still_draw_an_outline(tmp_path):
	assert border_style_of(build(tmp_path, style="TikTok Bold")) == "1"


def test_bounce_scales_the_spoken_word(tmp_path):
	body = open(build(tmp_path, style="Bounce", highlight=True), encoding="utf-8").read()
	assert "\\fscx112" in body


def test_styles_without_pop_do_not_scale(tmp_path):
	body = open(build(tmp_path, style="TikTok Bold", highlight=True), encoding="utf-8").read()
	assert "\\fscx112" not in body


@pytest.mark.parametrize("style", list(captions.CAPTION_STYLES))
def test_every_style_renders_both_with_and_without_highlighting(tmp_path, style):
	assert build(tmp_path, f"{style}-off.ass", style=style)
	assert build(tmp_path, f"{style}-on.ass", style=style, highlight=True)


# ── tidying real transcripts ─────────────────────────────────────────────────
# Every case here came out of one rendered clip that looked broken on screen.


def test_overlapping_segments_are_cut_at_the_next_one(tmp_path):
	"""YouTube's captions roll: the next line starts before the last one ends.
	Burned in as-is, two captions sit on screen at once, stacked, climbing into
	the middle of the picture. That is exactly what the first test render did."""
	segs = [
		{"start": 0.0, "duration": 5.0, "text": "and then you get"},
		{"start": 2.0, "duration": 5.0, "text": "laser on the wall"},
	]
	tidy = captions.tidy_segments(segs)
	assert tidy[0]["start"] + tidy[0]["duration"] <= tidy[1]["start"] + 1e-6


def test_no_two_captions_are_ever_on_screen_together(tmp_path):
	segs = [
		{"start": 0.0, "duration": 4.0, "text": "one two three"},
		{"start": 1.0, "duration": 4.0, "text": "four five six"},
		{"start": 2.0, "duration": 4.0, "text": "seven eight nine"},
	]
	out = captions.build_ass(segs, str(tmp_path / "o.ass"), words_per_line=4)
	spans = []
	for line in open(out, encoding="utf-8"):
		if line.startswith("Dialogue: 0"):
			parts = line.split(",")
			spans.append((parts[1].strip(), parts[2].strip()))
	spans.sort()
	for (s1, e1), (s2, _) in zip(spans, spans[1:]):
		assert e1 <= s2, f"caption {s1}-{e1} was still on screen when {s2} began"


def test_youtube_markup_is_not_burned_into_the_video():
	"""'>>' means a speaker change and '[Music]' is a sound event. Both were
	being rendered onto the picture."""
	segs = [
		{"start": 0, "duration": 2, "text": ">> you can see wall."},
		{"start": 3, "duration": 2, "text": "[Music] the next bit"},
		{"start": 6, "duration": 2, "text": "(laughs) and then this"},
	]
	texts = [s["text"] for s in captions.tidy_segments(segs)]
	assert texts == ["you can see wall.", "the next bit", "and then this"]


def test_a_rolling_caption_does_not_repeat_itself():
	"""Rolling captions restate the previous line before adding new words."""
	segs = [
		{"start": 0, "duration": 2, "text": "and then you get"},
		{"start": 2, "duration": 2, "text": "and then you get laser on the wall"},
	]
	assert [s["text"] for s in captions.tidy_segments(segs)] == [
		"and then you get",
		"laser on the wall",
	]


def test_a_segment_left_with_nothing_is_dropped():
	segs = [
		{"start": 0, "duration": 2, "text": "the same words"},
		{"start": 2, "duration": 2, "text": "the same words"},
	]
	assert len(captions.tidy_segments(segs)) == 1


def test_segments_are_sorted_before_anything_else():
	segs = [
		{"start": 5, "duration": 2, "text": "second"},
		{"start": 0, "duration": 2, "text": "first"},
	]
	assert [s["text"] for s in captions.tidy_segments(segs)] == ["first", "second"]


def test_chunks_never_exceed_a_readable_line_width():
	"""'LASER ON / THE WALL.' — four words, but wide ones, so libass wrapped it
	and the caption became two lines tall in the middle of the frame."""
	budget = captions.line_budget(17, 162)
	for chunk in captions._chunk_words("laser on the wall. through the wall.".split(), 4, budget):
		assert len(" ".join(chunk)) <= budget


def test_one_very_long_word_still_gets_a_chunk():
	"""Nothing sensible can be done with a word wider than the line, so it gets
	its own chunk rather than being dropped or split mid-word."""
	assert captions._chunk_words(["antidisestablishmentarianism"], 4, 12) == [
		["antidisestablishmentarianism"]
	]


def test_the_caption_canvas_matches_the_video_shape(tmp_path):
	"""A 4:3 PlayRes over a 9:16 frame stretches every glyph sideways."""
	def play_x(ratio):
		out = captions.build_ass(SEGMENTS, str(tmp_path / f"{ratio.replace(':','-')}.ass"), ratio=ratio)
		line = [l for l in open(out, encoding="utf-8") if l.startswith("PlayResX")][0]
		return int(line.split(":")[1])

	assert play_x("9:16") / 288 == pytest.approx(9 / 16, abs=0.02)
	assert play_x("1:1") / 288 == pytest.approx(1.0, abs=0.02)
	assert play_x("16:9") / 288 == pytest.approx(16 / 9, abs=0.03)


def test_the_line_budget_comes_from_the_font_and_canvas():
	"""A fixed character limit was the first attempt and it was wrong — at
	fontsize 17 on a 9:16 canvas only about thirteen characters fit, so a
	twenty-character chunk still wrapped."""
	narrow = captions.line_budget(17, 162)     # 9:16
	square = captions.line_budget(17, 288)     # 1:1
	wide = captions.line_budget(17, 512)       # 16:9
	assert narrow < square < wide
	assert 10 <= narrow <= 16

	# A smaller style fits more on the same canvas.
	assert captions.line_budget(14, 162) > narrow


def test_no_chunk_is_wide_enough_to_wrap_on_a_vertical_clip(tmp_path):
	"""Wrapping is what put captions two lines tall in the middle of the frame."""
	budget = captions.line_budget(captions.CAPTION_STYLES["TikTok Bold"]["fontsize"], 162)
	words = "and then you get laser on the wall you can see through the wall".split()
	for chunk in captions._chunk_words(words, 4, budget):
		assert len(" ".join(chunk)) <= budget
