"""The customer's own logo on their clips — the web app's copy.

Same feature as the desktop, with one difference that matters: the logo belongs
to an account rather than to the machine, so nothing here may reach another
user's folder.

Original notes follow.

The customer's own logo on their clips.

The filter graph is the part worth checking hardest: an off-by-one in the
position expression is a logo hanging off the frame, and the only other way to
find that out is to sit through a render and look at it. So the string is
asserted directly, and then ffmpeg is asked whether it will actually accept it.
"""
import os
import shutil
import subprocess

import pytest

from core import brand

needs_ffmpeg = pytest.mark.skipif(
	not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
	reason="needs ffmpeg and ffprobe on PATH",
)


@pytest.fixture
def client(tmp_path, monkeypatch):
	"""A logged-in client whose account folder is a throwaway.

	Every /api/brand endpoint is behind auth, which is right — a logo belongs to
	one account. These tests are about whether the feature works, not about
	logging in, so they are handed a session rather than making one.
	"""
	from fastapi.testclient import TestClient

	from backend import main

	monkeypatch.setattr(main.auth, "current_user", lambda: "tester")
	main.app.dependency_overrides[main.auth.current_user] = lambda: "tester"
	monkeypatch.setattr(main, "user_root", lambda user: str(tmp_path / "users" / user))
	yield TestClient(main.app)
	main.app.dependency_overrides.clear()


@pytest.fixture
def in_tmp(tmp_path):
	"""A throwaway account folder to keep every logo these tests write inside.

	store_logo clears its own directory of anything named logo*, so pointing it
	at a real account's folder would delete the logo that account had chosen.
	"""
	return tmp_path


def _png(path, size=(64, 64)):
	"""A real PNG, written by hand so the tests need no image library."""
	import struct
	import zlib

	w, h = size
	raw = b"".join(b"\x00" + bytes([255, 40, 90, 255] * w) for _ in range(h))

	def chunk(tag, data):
		body = tag + data
		return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

	png = (b"\x89PNG\r\n\x1a\n"
	       + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
	       + chunk(b"IDAT", zlib.compress(raw))
	       + chunk(b"IEND", b""))
	with open(path, "wb") as f:
		f.write(png)
	return str(path)


# ── the defaults, which decide what happens to everyone who changes nothing ───
def test_no_logo_means_the_feature_is_off():
	"""The promise that nothing already rendered changes. A config with none of
	these keys is every config that exists today."""
	assert brand.settings({})["enabled"] is False
	assert brand.settings({"brand_enabled": True})["enabled"] is False, "on, but no image"


def test_a_logo_on_disk_is_not_enough_on_its_own():
	assert brand.settings({"brand_logo": "/some/logo.png"})["enabled"] is False


def test_the_defaults_are_a_logo_and_not_a_billboard():
	settings = brand.settings({"brand_logo": "x.png", "brand_enabled": True})
	assert settings["enabled"] is True
	assert 5 <= settings["scale_pct"] <= 20
	assert 0.5 <= settings["opacity"] < 1.0, "fully opaque competes with the footage"
	assert settings["position"] == brand.DEFAULT_POSITION


# ── values arriving from a config file and from request bodies ────────────────
def test_nonsense_is_clamped_rather_than_passed_to_ffmpeg():
	"""These come from a saved file and from request bodies. A scale of 900 or an
	opacity of 40 should give a sane clip, not a filter graph ffmpeg rejects."""
	settings = brand.settings({
		"brand_logo": "x.png", "brand_enabled": True,
		"brand_scale_pct": 900, "brand_opacity": 40, "brand_x": 5, "brand_y": -2,
	})
	assert settings["scale_pct"] == brand.MAX_SCALE_PCT
	assert settings["opacity"] == 1.0
	assert settings["x"] == 1.0 and settings["y"] == 0.0


def test_text_where_a_number_should_be_falls_back(in_tmp):
	settings = brand.settings({
		"brand_logo": "x.png", "brand_enabled": True,
		"brand_scale_pct": "big", "brand_opacity": None,
	})
	assert settings["scale_pct"] == brand.DEFAULT_SCALE_PCT
	assert settings["opacity"] == brand.DEFAULT_OPACITY


def test_an_unknown_position_falls_back_to_the_default():
	assert brand.settings({"brand_position": "somewhere"})["position"] == brand.DEFAULT_POSITION


def test_the_position_follows_the_chosen_anchor():
	top_left = brand.settings({"brand_position": "top-left"})
	bottom_right = brand.settings({"brand_position": "bottom-right"})
	assert top_left["x"] < 0.5 and top_left["y"] < 0.5
	assert bottom_right["x"] > 0.5 and bottom_right["y"] > 0.5


def test_a_dragged_position_beats_the_anchor():
	"""Dragging the chip in the preview is the more specific instruction."""
	settings = brand.settings({"brand_position": "top-left", "brand_x": 0.9, "brand_y": 0.8})
	assert (settings["x"], settings["y"]) == (0.9, 0.8)


# ── storing the file ──────────────────────────────────────────────────────────
def test_the_logo_is_copied_not_merely_remembered(in_tmp, tmp_path):
	"""Remembering a path means a clip that renders without the logo, silently,
	because someone tidied their Downloads folder."""
	source = _png(tmp_path / "mylogo.png")
	stored = brand.store_logo(str(in_tmp), source)
	assert os.path.isfile(stored)
	assert os.path.abspath(stored) != os.path.abspath(source)

	os.remove(source)
	assert os.path.isfile(stored), "the stored copy must survive the original"


def test_choosing_a_second_logo_does_not_leave_the_first(in_tmp, tmp_path):
	brand.store_logo(str(in_tmp), _png(tmp_path / "one.png"))
	brand.store_logo(str(in_tmp), _png(tmp_path / "two.webp"))
	left = [f for f in os.listdir(brand.logo_dir(str(in_tmp))) if f.startswith("logo")]
	assert len(left) == 1, f"old logos were left behind: {left}"


def test_something_that_is_not_an_image_is_refused(in_tmp, tmp_path):
	doc = tmp_path / "notes.txt"
	doc.write_text("hello", encoding="utf-8")
	with pytest.raises(ValueError, match="not an image"):
		brand.store_logo(str(in_tmp), str(doc))


def test_a_file_that_is_not_there_is_refused(in_tmp):
	with pytest.raises(ValueError, match="not there"):
		brand.store_logo(str(in_tmp), "/nowhere/logo.png")


# ── the filter graph ──────────────────────────────────────────────────────────
def _logo(**over):
	base = {"enabled": True, "path": "/tmp/logo.png", "position": "top-right",
	        "x": 0.96, "y": 0.04, "scale_pct": 12, "opacity": 0.85}
	base.update(over)
	return base


def test_the_position_is_a_fraction_of_the_free_space():
	"""(W-w) rather than W, so the logo can never hang over the edge whatever
	size it ends up — which is the whole reason the maths is ffmpeg's and not
	ours."""
	graph = brand.overlay_filter(_logo(x=1.0, y=1.0), 1080)
	assert "(W-w)*1.0000" in graph and "(H-h)*1.0000" in graph


def test_the_logo_is_sized_against_the_frame_not_itself():
	assert "scale=108:-1" in brand.overlay_filter(_logo(scale_pct=10), 1080)
	assert "scale=216:-1" in brand.overlay_filter(_logo(scale_pct=10), 2160)


def test_the_scaled_width_is_even():
	"""Odd widths are what some scalers refuse outright."""
	for pct in range(2, 61):
		graph = brand.overlay_filter(_logo(scale_pct=pct), 1080)
		width = int(graph.split("scale=")[1].split(":")[0])
		assert width % 2 == 0, (pct, width)


def test_opacity_is_applied_after_forcing_an_alpha_channel():
	"""colorchannelmixer cannot touch an alpha channel that is not there, so on
	a JPEG the opacity would silently do nothing."""
	graph = brand.overlay_filter(_logo(opacity=0.5), 1080)
	assert graph.index("format=rgba") < graph.index("colorchannelmixer=aa=0.500")


def test_a_windows_path_is_escaped_for_the_filter_parser():
	"""A drive letter's colon reads as an option separator, and the whole thing
	arrives at ffmpeg as an unknown option."""
	graph = brand.overlay_filter(_logo(path=r"C:\Users\Me\logo.png"), 1080)
	assert "C\\:/Users/Me/logo.png" in graph
	assert "\\Users" not in graph


def test_the_graph_ends_where_the_caller_maps_from():
	assert brand.overlay_filter(_logo(), 1080).endswith("[out]")


# ── refusing to do the wrong thing ────────────────────────────────────────────
def test_a_missing_logo_file_is_reported_rather_than_skipped(tmp_path):
	"""The setting being on means the customer believes every clip carries their
	mark. Shipping one without it quietly is the wrong kind of resilient."""
	ok, err = brand.apply_logo(str(tmp_path / "clip.mp4"), str(tmp_path / "out.mp4"),
	                           _logo(path=str(tmp_path / "gone.png")), 1080)
	assert ok is False and "no longer where" in err


def test_it_does_nothing_when_it_is_switched_off(tmp_path):
	ok, err = brand.apply_logo("in.mp4", "out.mp4", _logo(enabled=False), 1080)
	assert ok is False and "No logo" in err


# ── the part only ffmpeg can answer ───────────────────────────────────────────
@needs_ffmpeg
def test_ffmpeg_accepts_the_graph_and_the_clip_survives(in_tmp, tmp_path):
	"""A filter graph that reads correctly and that ffmpeg rejects is still a
	failed render. Only ffmpeg knows which it is."""
	clip = tmp_path / "clip.mp4"
	subprocess.run(
		["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=navy:size=1080x1920:rate=25:duration=2",
		 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
		 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip)],
		capture_output=True, check=True,
	)
	logo = _logo(path=_png(tmp_path / "logo.png"))
	out = tmp_path / "out.mp4"

	ok, err = brand.apply_logo(str(clip), str(out), logo, 1080)
	assert ok, err
	assert out.stat().st_size > 1000

	# The frame must come out the size it went in — an overlay that resizes the
	# clip is a clip that no longer fits the ratio someone chose.
	probe = subprocess.run(
		["ffprobe", "-v", "quiet", "-select_streams", "v:0",
		 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
		capture_output=True, text=True, check=True,
	)
	assert probe.stdout.strip().startswith("1080,1920")


# ── the whole way through, as the app actually calls it ───────────────────────
def test_uploading_a_logo_turns_the_feature_on_and_reaches_the_renderer(client, in_tmp, tmp_path):
	"""The decisive path. Every piece above can be right while the settings never
	reach the code that draws — which is a feature that tests green and does
	nothing.

	Choosing a logo also switches it on. Making someone find a separate toggle
	afterwards has one outcome: wondering why their logo is not showing up.
	"""
	png = open(_png(tmp_path / "logo.png"), "rb").read()

	assert client.get("/api/config").json()["brand"]["enabled"] is False

	r = client.post("/api/brand/logo", files={"file": ("logo.png", png, "image/png")})
	assert r.status_code == 200, r.text
	assert r.json()["brand"]["enabled"] is True

	# and it is there on the next page load, not just in that response
	served = client.get("/api/config").json()["brand"]
	assert served["enabled"] is True and os.path.isfile(served["path"])
	assert client.get("/api/brand/logo").status_code == 200

	# the renderer reads the same settings, so what was saved is what gets drawn
	from backend import main
	assert brand.settings(main.load_user_config("tester"))["enabled"] is True


def test_a_setting_saved_from_the_ui_survives_and_is_clamped(client, in_tmp, tmp_path):
	client.post("/api/brand/logo", files={"file": ("l.png", open(_png(tmp_path / "l.png"), "rb").read(), "image/png")})

	assert client.post("/api/config", json={"brand_scale_pct": 900, "brand_position": "bottom-left"}).status_code == 200
	saved = client.get("/api/config").json()["brand"]
	assert saved["scale_pct"] == brand.MAX_SCALE_PCT
	assert saved["position"] == "bottom-left"


def test_removing_the_logo_takes_the_file_with_it(client, in_tmp, tmp_path):
	client.post("/api/brand/logo", files={"file": ("l.png", open(_png(tmp_path / "l.png"), "rb").read(), "image/png")})
	path = client.get("/api/config").json()["brand"]["path"]
	assert os.path.isfile(path)

	assert client.delete("/api/brand/logo").status_code == 200
	assert not os.path.exists(path), "the image is ours and nothing else uses it"
	assert client.get("/api/config").json()["brand"]["enabled"] is False
	assert client.get("/api/brand/logo").status_code == 404


def test_a_document_uploaded_as_a_logo_is_refused(client, in_tmp):
	r = client.post("/api/brand/logo", files={"file": ("notes.txt", b"hello", "text/plain")})
	assert r.status_code == 400
	assert client.get("/api/config").json()["brand"]["enabled"] is False


@needs_ffmpeg
def test_the_audio_is_copied_not_re_encoded(in_tmp, tmp_path):
	"""This pass exists to draw a logo. Re-encoding the audio to do it would cost
	a generation of quality for nothing."""
	clip = tmp_path / "clip.mp4"
	subprocess.run(
		["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:size=640x640:rate=25:duration=2",
		 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
		 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip)],
		capture_output=True, check=True,
	)
	out = tmp_path / "out.mp4"
	ok, err = brand.apply_logo(str(clip), str(out), _logo(path=_png(tmp_path / "l.png")), 640)
	assert ok, err

	def audio_of(path):
		r = subprocess.run(
			["ffprobe", "-v", "quiet", "-select_streams", "a:0",
			 "-show_entries", "stream=codec_name,sample_rate", "-of", "csv=p=0", str(path)],
			capture_output=True, text=True, check=True,
		)
		return r.stdout.strip()

	assert audio_of(out) == audio_of(clip)


def test_one_account_cannot_reach_another_ones_logo(tmp_path, monkeypatch):
	"""The difference from the desktop, and the one worth a test of its own.

	A logo is somebody's branding. Two people sharing this server must never end
	up wearing each other's, and must not be able to fetch each other's file. The
	path is never taken from the request — it is read out of the caller's own
	config — so there is nothing to point somewhere else.
	"""
	from fastapi.testclient import TestClient

	from backend import main

	# The mode the server actually runs in. With MULTI_USER off the app is
	# deliberately a single-user desktop app and everyone shares one config —
	# correct there, and not what this test is about.
	monkeypatch.setattr(main.auth, "MULTI_USER", True)
	monkeypatch.setattr(main, "user_root", lambda user: str(tmp_path / "users" / user))
	main.user_configs.clear()
	png = open(_png(tmp_path / "l.png"), "rb").read()

	def as_user(name):
		main.app.dependency_overrides[main.auth.current_user] = lambda: name
		return TestClient(main.app)

	try:
		ali = as_user("ali")
		assert ali.post("/api/brand/logo", files={"file": ("l.png", png, "image/png")}).status_code == 200
		ali_logo = ali.get("/api/config").json()["brand"]["path"]

		sara = as_user("sara")
		assert sara.get("/api/config").json()["brand"]["enabled"] is False, "a logo must not leak across accounts"
		assert sara.get("/api/brand/logo").status_code == 404

		# and one deleting theirs must not touch the other's file
		assert sara.delete("/api/brand/logo").status_code == 200
		assert os.path.isfile(ali_logo)

		assert as_user("ali").get("/api/config").json()["brand"]["enabled"] is True
	finally:
		main.app.dependency_overrides.clear()
		main.user_configs.clear()
