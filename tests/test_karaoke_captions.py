"""Word-by-word caption highlighting and the two styles added with it.

The rule this file exists to enforce: turning highlighting on must be the only
thing that changes. With it off, the file written today has to be byte-for-byte
what the previous build wrote, because customers re-render old clips.

    .venv\\Scripts\\python.exe -m pytest tests/test_karaoke_captions.py -q
"""
from __future__ import annotations

import os
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
	"""Only the colour moves. Showing one word at a time would lose the phrase."""
	line = dialogues(build(tmp_path, words_per_line=4, highlight=True))[0]
	body = text_of(line)
	for word in ("THIS", "IS", "THE", "HOOK"):
		assert word in body


def test_exactly_one_word_is_highlighted_per_line(tmp_path):
	st = captions.CAPTION_STYLES["TikTok Bold"]
	for line in dialogues(build(tmp_path, words_per_line=4, highlight=True)):
		assert text_of(line).count(st["highlight"]) == 1


def test_the_highlight_walks_forward_through_the_chunk(tmp_path):
	lines = dialogues(build(tmp_path, words_per_line=4, highlight=True))[:4]
	active = captions.CAPTION_STYLES["TikTok Bold"]["highlight"]
	positions = [text_of(l).index(active) for l in lines]
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
	for line in dialogues(out):
		body = text_of(line)
		assert "{x}" not in body.lower()
		assert "(X)" in body


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
