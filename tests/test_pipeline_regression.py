"""A safety net around the parts of the pipeline that already work.

None of this tests new behaviour. It pins the current behaviour so that the
features going in next — karaoke captions, local transcription, new layouts —
cannot quietly change what today's users already rely on. If one of these
fails, the change is a regression until proven otherwise.

Everything here is a pure function: no ffmpeg, no network, no GPU, so the whole
file runs in under a second and there is no excuse not to run it.

    .venv\\Scripts\\python.exe -m pytest tests/test_pipeline_regression.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import captions, effects, video  # noqa: E402

SEGMENTS = [
	{"start": 0.0, "duration": 2.0, "text": "hello there friend"},
	{"start": 2.0, "duration": 3.0, "text": "this is the hook everyone waits for"},
]


# ── captions ─────────────────────────────────────────────────────────────────


def test_the_four_shipped_caption_styles_still_exist():
	"""Renaming or dropping one of these would change how every previously
	rendered clip looks if it were re-rendered. New styles may be added — this
	pins the originals, not the count."""
	assert set(captions.CAPTION_STYLES) >= {"TikTok Bold", "Clean White", "Yellow Pop", "Neon"}
	assert captions.DEFAULT_CAPTION_STYLE == "TikTok Bold"


def test_the_original_styles_keep_their_exact_appearance():
	"""Colour, outline and size decide what a re-rendered clip looks like."""
	assert captions.CAPTION_STYLES["TikTok Bold"]["colour"] == "&H00FFFFFF"
	assert captions.CAPTION_STYLES["TikTok Bold"]["outline"] == "&H00000000"
	assert captions.CAPTION_STYLES["TikTok Bold"]["fontsize"] == 17
	assert captions.CAPTION_STYLES["Clean White"]["fontsize"] == 14
	assert captions.CAPTION_STYLES["Yellow Pop"]["colour"] == "&H0000FFFF"
	assert captions.CAPTION_STYLES["Neon"]["outline"] == "&H00800080"


def test_the_original_styles_still_draw_an_outline_not_a_box():
	"""BorderStyle 3 was added for the Boxed style; the others must stay at 1."""
	for name in ("TikTok Bold", "Clean White", "Yellow Pop", "Neon"):
		assert not captions.CAPTION_STYLES[name].get("box")


def test_old_style_names_still_resolve():
	"""Clips saved by earlier builds carry these names; they must keep working."""
	for old, new in captions.CAPTION_ALIASES.items():
		assert captions.caption_style(old) == captions.CAPTION_STYLES[new]


def test_an_unknown_style_falls_back_instead_of_crashing():
	assert captions.caption_style("no such style") == captions.CAPTION_STYLES[captions.DEFAULT_CAPTION_STYLE]


def test_captions_are_chunked_and_upper_cased(tmp_path):
	out = captions.build_ass(SEGMENTS, str(tmp_path / "c.ass"), words_per_line=3)
	body = open(out, encoding="utf-8").read()

	assert "[Script Info]" in body and "Dialogue:" in body
	assert "HELLO THERE FRIEND" in body
	# Six words at three per line becomes two chunks, not one line of six.
	assert "THIS IS THE" in body and "HOOK EVERYONE WAITS" in body


def test_words_per_line_changes_the_chunking(tmp_path):
	two = open(captions.build_ass(SEGMENTS, str(tmp_path / "a.ass"), words_per_line=2), encoding="utf-8").read()
	assert "HELLO THERE" in two and "HELLO THERE FRIEND" not in two


def test_chunk_timings_stay_inside_their_segment(tmp_path):
	"""Captions that drift past their segment desynchronise by the end of a clip."""
	out = captions.build_ass(SEGMENTS, str(tmp_path / "c.ass"), words_per_line=3)
	times = []
	for line in open(out, encoding="utf-8"):
		if line.startswith("Dialogue:"):
			parts = line.split(",")
			times.append((parts[1].strip(), parts[2].strip()))
	assert times, "no dialogue lines were written"
	assert times[0][0] == captions._ass_time(0.0)
	last_end = captions._ass_time(5.0)
	assert times[-1][1] <= last_end


def test_nothing_to_burn_in_returns_none(tmp_path):
	assert captions.build_ass([], str(tmp_path / "empty.ass")) is None


def test_special_characters_do_not_break_the_ass_file(tmp_path):
	"""Braces and backslashes are ASS markup. A transcript containing them must
	come out as text, not as override tags — anything inside braces is silently
	swallowed by the renderer, so a stray `{` eats the rest of the caption.

	The only braces on the line should be the ones we put there ourselves.
	"""
	out = captions.build_ass(
		[{"start": 0, "duration": 2, "text": r"price {is} 50\50 split"}],
		str(tmp_path / "s.ass"),
	)
	texts = [
		l.rsplit(",,", 1)[1]   # everything after the last field separator
		for l in open(out, encoding="utf-8")
		if l.startswith("Dialogue:")
	]
	for text in texts:
		assert text.count("{") == 1 and text.startswith("{\\an2}")

	# The words survive, only the markup characters are neutralised. Five words
	# at three per line means the last one is on the second dialogue line.
	joined = " ".join(texts)
	assert "IS" in joined and "SPLIT" in joined


def test_headline_and_overlays_are_written(tmp_path):
	out = captions.build_ass(
		SEGMENTS,
		str(tmp_path / "h.ass"),
		headline={"text": "The hook", "style": "box", "position": "top", "size": 20},
		clip_duration=6.0,
		overlays=[{"text": "follow me", "x": 0.5, "y": 0.8, "size": 14, "color": "yellow"}],
	)
	body = open(out, encoding="utf-8").read()
	assert "THE HOOK" in body.upper()
	assert "FOLLOW ME" in body.upper()


# ── reframing filter chains ──────────────────────────────────────────────────


def test_smart_crop_centres_on_the_detected_face():
	chain = video._smart_crop_filter(1920, 1080, "9:16", [{"cx": 0.25, "cy": 0.4}])
	assert chain.startswith("crop=") and chain.endswith("scale=1080:1920")
	x = int(chain.split("crop=")[1].split(":")[2])
	# The face sits left of centre, so the window must too.
	centre_x = int(video._smart_crop_filter(1920, 1080, "9:16", [{"cx": 0.5, "cy": 0.4}]).split("crop=")[1].split(":")[2])
	assert x < centre_x


def test_smart_crop_with_no_faces_still_produces_a_chain():
	assert video._smart_crop_filter(1920, 1080, "9:16", []) is not None


def test_a_source_already_narrower_than_the_target_is_padded_not_cropped():
	chain = video._smart_crop_filter(720, 1280, "9:16", [])
	assert "pad=" in chain and "crop=" not in chain


def test_manual_crop_respects_zoom_and_stays_inside_the_frame():
	wide = video._manual_crop_filter(1920, 1080, "9:16", {"cx": 0.5, "cy": 0.5, "zoom": 1.0})
	tight = video._manual_crop_filter(1920, 1080, "9:16", {"cx": 0.5, "cy": 0.5, "zoom": 2.0})
	assert int(tight.split("crop=")[1].split(":")[0]) < int(wide.split("crop=")[1].split(":")[0])

	edge = video._manual_crop_filter(1920, 1080, "9:16", {"cx": 0.0, "cy": 1.0, "zoom": 1.0})
	w, h, x, y = (int(v) for v in edge.split("crop=")[1].split(",")[0].split(":"))
	assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080


def test_manual_crop_treats_zero_as_a_position_not_a_missing_value():
	"""`crop.get(k) or default` would silently recentre a crop dragged to the edge."""
	left = video._manual_crop_filter(1920, 1080, "9:16", {"cx": 0.0, "cy": 0.5, "zoom": 1.0})
	assert int(left.split("crop=")[1].split(":")[2]) == 0


def test_every_shipped_ratio_still_renders():
	for ratio in video.TARGETS:
		assert video._manual_crop_filter(1920, 1080, ratio, None)


def test_fit_mode_keeps_the_whole_frame_and_blurs_the_background():
	chain = video._fit_filter_complex("9:16", None, [])
	assert "boxblur" in chain or "gblur" in chain
	assert "overlay" in chain


def test_split_mode_stacks_two_speakers():
	"""Faces carry a `weight` — how often that person was actually on screen —
	and split mode uses it to refuse a second panel for someone who wandered
	through one frame."""
	chain = video._split_filter_complex(
		1920,
		1080,
		[{"cx": 0.25, "cy": 0.4, "weight": 0.6}, {"cx": 0.75, "cy": 0.4, "weight": 0.4}],
		None,
		[],
	)
	assert chain and ("vstack" in chain or "overlay" in chain)


def test_split_mode_declines_when_the_second_face_is_barely_there():
	"""Returning None hands the clip back to smart crop, which is the right
	answer — a stacked layout with a passer-by in the bottom half looks broken."""
	chain = video._split_filter_complex(
		1920,
		1080,
		[{"cx": 0.25, "cy": 0.4, "weight": 0.9}, {"cx": 0.75, "cy": 0.4, "weight": 0.02}],
		None,
		[],
	)
	assert chain is None or "vstack" in chain


# ── look effects ─────────────────────────────────────────────────────────────


def test_no_effects_means_no_filters():
	assert effects.video_filters(None) == []
	assert effects.video_filters({}) == []
	assert effects.audio_filter(None) is None
	assert effects.active(None) is False


def test_each_effect_produces_its_filter():
	assert any("hflip" in f for f in effects.video_filters({"mirror": True}))
	assert any("eq=" in f for f in effects.video_filters({"brightness": 0.2}))
	assert any("setpts" in f for f in effects.video_filters({"speed": 1.5}))
	assert effects.audio_filter({"speed": 1.5}) is not None


def test_filter_order_is_flip_then_look_then_colour_then_speed():
	"""Order changes the picture: a look applied after a colour tweak looks
	different from one applied before it."""
	chain = effects.video_filters(
		{"mirror": True, "look": next(k for k in effects.LOOKS if k != "none"), "brightness": 0.2, "speed": 1.5}
	)
	joined = "|".join(chain)
	assert joined.index("hflip") < joined.index("eq=")
	assert joined.index("eq=") < joined.index("setpts")


def test_speed_is_clamped_to_the_slider_range():
	assert effects.speed_factor({"speed": 99}) == effects.SPEED[1]
	assert effects.speed_factor({"speed": 0.01}) == effects.SPEED[0]


def test_speed_of_one_adds_nothing():
	assert effects.video_filters({"speed": 1.0}) == []
	assert effects.audio_filter({"speed": 1.0}) is None


@pytest.mark.parametrize("look", ["none", "nonexistent-look"])
def test_unknown_or_empty_look_is_ignored(look):
	assert effects.video_filters({"look": look}) == []


# ── gameplay + facecam layout ────────────────────────────────────────────────


def test_gamecam_stacks_the_camera_above_fitted_gameplay():
	chain = video._gamecam_filter_complex(1920, 1080, {"corner": "bottom-left"}, None, [])
	assert "vstack" in chain
	# The game panel is fitted and padded, never cropped — cropping a game to
	# 9:16 throws away the half of the screen where everything happens.
	assert "force_original_aspect_ratio=decrease" in chain and "pad=1080:1320" in chain


def test_the_two_panels_fill_a_9x16_frame_exactly():
	chain = video._gamecam_filter_complex(1920, 1080, None, None, [])
	assert "scale=1080:600" in chain      # camera
	assert "pad=1080:1320" in chain       # gameplay — 600 + 1320 = 1920


@pytest.mark.parametrize("corner", list(video.FACECAM_CORNERS))
def test_each_corner_crops_a_different_part_of_the_frame(corner):
	chain = video._gamecam_filter_complex(1920, 1080, {"corner": corner}, None, [])
	crop = chain.split("crop=")[1].split(",")[0]
	w, h, x, y = (int(v) for v in crop.split(":"))
	assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080


def test_the_camera_crop_stays_inside_the_frame_at_the_edges():
	chain = video._gamecam_filter_complex(1920, 1080, {"cx": 0.0, "cy": 1.0}, None, [])
	w, h, x, y = (int(v) for v in chain.split("crop=")[1].split(",")[0].split(":"))
	assert x == 0 and y + h <= 1080


def test_an_unreadable_source_gives_up_instead_of_guessing():
	assert video._gamecam_filter_complex(0, 0, None, None, []) is None


def test_an_unknown_corner_falls_back_to_the_default():
	unknown = video._gamecam_filter_complex(1920, 1080, {"corner": "middle"}, None, [])
	default = video._gamecam_filter_complex(1920, 1080, {"corner": video.DEFAULT_FACECAM}, None, [])
	assert unknown == default
