"""The ported features, with the part that is different from the desktop tested hardest.

Most of this logic is shared with the desktop build and is covered there. What is
*not* shared is the thing worth the most tests here: on a server every project,
every stock-footage key and every downloaded cutaway belongs to one account, and
one account must never be able to reach another's. That is what most of this file
checks.

    .venv\\Scripts\\python.exe -m pytest tests/test_projects_broll_web.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from core import broll, captions, projects, transcript, updates

needs_ffmpeg = pytest.mark.skipif(
	not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
	reason="needs ffmpeg and ffprobe on PATH",
)


@pytest.fixture
def two_users(tmp_path):
	"""Two user roots, the way backend.main.user_root lays them out."""
	a = tmp_path / "users" / "alice"
	b = tmp_path / "users" / "bob"
	a.mkdir(parents=True)
	b.mkdir(parents=True)
	return str(a), str(b)


# ── per-user isolation: the whole reason this is a port and not a copy ─────────
def test_one_users_library_does_not_show_anothers(two_users):
	alice, bob = two_users
	projects.create(alice, "Alice's interview")
	projects.create(bob, "Bob's podcast")

	assert [p["title"] for p in projects.listing(alice)] == ["Alice's interview"]
	assert [p["title"] for p in projects.listing(bob)] == ["Bob's podcast"]


def test_a_valid_id_from_another_user_is_not_loadable(two_users):
	"""The id is well-formed, so the hex check passes — ownership is what stops it."""
	alice, bob = two_users
	rec = projects.create(alice, "Alice's interview")

	assert projects.exists(alice, rec["id"])
	assert not projects.exists(bob, rec["id"])
	with pytest.raises(OSError):
		projects.load(bob, rec["id"])


def test_deleting_moves_clips_into_the_owners_folder_only(two_users):
	alice, bob = two_users
	rec = projects.create(alice, "Talk")
	with open(os.path.join(projects.clips_dir(alice, rec["id"]), "one.mp4"), "wb") as f:
		f.write(b"x")

	result = projects.delete(alice, rec["id"])
	assert result["clips_kept"] == 1
	assert os.path.isfile(os.path.join(alice, "clips", "one.mp4"))
	assert not os.path.exists(os.path.join(bob, "clips", "one.mp4"))


@pytest.mark.parametrize("bad", ["", "..", "../..", "not-hex-here", "abc", "0123456789abc"])
def test_a_project_id_from_a_url_cannot_escape_the_folder(two_users, bad):
	alice, _ = two_users
	with pytest.raises(ValueError):
		projects.path_for(alice, bad)


def test_a_corrupt_manifest_does_not_break_the_library(two_users):
	alice, _ = two_users
	good = projects.create(alice, "Good")
	broken = os.path.join(projects.root_for(alice), "0123456789ab")
	os.makedirs(broken)
	with open(os.path.join(broken, "project.json"), "w", encoding="utf-8") as f:
		f.write("{not json")

	assert [p["id"] for p in projects.listing(alice)] == [good["id"]]


def test_save_is_atomic_and_leaves_no_temp_file(two_users):
	alice, _ = two_users
	rec = projects.create(alice, "Talk")
	rec["transcript"] = [{"start": 0, "duration": 1, "text": "hi"}]
	projects.save(alice, rec)

	folder = projects.path_for(alice, rec["id"])
	assert not [n for n in os.listdir(folder) if n.endswith(".tmp")]
	assert projects.load(alice, rec["id"])["transcript"][0]["text"] == "hi"


# ── B-roll: keys and folders come from the caller, never from a global ─────────
def test_the_key_comes_from_the_config_passed_in():
	assert broll.key_for("pexels", {"pexels_api_key": "px"}) == "px"
	assert broll.key_for("pexels", {}) == ""


def test_configured_reports_only_this_users_sources():
	assert broll.configured({"pexels_api_key": "px"}) == ["pexels"]
	assert broll.configured({}) == []


def test_search_without_a_key_names_the_service():
	with pytest.raises(broll.BrollError) as e:
		broll.search("city", {}, source="pexels")
	assert "Pexels" in str(e.value)


def test_search_rejects_an_empty_query():
	with pytest.raises(broll.BrollError):
		broll.search("   ", {"pexels_api_key": "px"})


def test_footage_already_downloaded_is_not_fetched_again(tmp_path, monkeypatch):
	"""Picking the same footage for a second clip must not download it twice.

	The opener is replaced with one that fails if it is used at all, so a cache
	miss cannot pass this test quietly.
	"""
	folder = tmp_path / "alice" / "broll"
	folder.mkdir(parents=True)
	cached = folder / "clip-1.mp4"
	cached.write_bytes(b"x" * 200_000)      # over the 100 KB "looks complete" bar

	def no_network(cfg=None):
		raise AssertionError("fetch went to the network for a file it already had")

	monkeypatch.setattr(broll, "_opener", no_network)
	assert broll.fetch("https://example.test/a.mp4", dest_dir=str(folder),
	                   name_hint="clip-1") == str(cached)


def test_a_truncated_cache_file_is_downloaded_again(tmp_path, monkeypatch):
	"""A half-written file must not be served as if it were good."""
	folder = tmp_path / "alice" / "broll"
	folder.mkdir(parents=True)
	(folder / "clip-1.mp4").write_bytes(b"x" * 500)       # far too small

	used = []

	def fake_opener(cfg=None):
		used.append(True)
		raise OSError("network down")

	monkeypatch.setattr(broll, "_opener", fake_opener)
	with pytest.raises(broll.BrollError):
		broll.fetch("https://example.test/a.mp4", dest_dir=str(folder), name_hint="clip-1")
	assert used, "a truncated file should have triggered a fresh download"


def test_footage_lands_in_the_folder_the_caller_names(tmp_path, monkeypatch):
	"""A shared folder would let one account's filename collide with another's."""
	folder = tmp_path / "bob" / "broll"

	class OneChunk:
		def __enter__(self):
			return self

		def __exit__(self, *a):
			return False

		def __init__(self):
			self.left = 1

		def read(self, n):
			if not self.left:
				return b""
			self.left = 0
			return b"y" * 150_000

	monkeypatch.setattr(broll, "_opener",
	                    lambda cfg=None: type("O", (), {"open": lambda s, r, timeout=0: OneChunk()})())
	path = broll.fetch("https://example.test/a.mp4", dest_dir=str(folder), name_hint="clip-2")
	assert path == str(folder / "clip-2.mp4")
	assert os.path.getsize(path) == 150_000


def test_cutaway_fills_the_frame_and_is_shifted_onto_the_clip_clock():
	chain = broll._insert_chain(1, "0:v", "v1",
	                            {"at": 2.0, "duration": 3.0, "mode": "cutaway"}, 1080, 1920)
	assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in chain
	assert "setpts=PTS-STARTPTS+2.000/TB" in chain
	assert "enable='between(t,2.000,5.000)'" in chain


def test_missing_footage_is_reported_not_rendered(tmp_path):
	ok, err = broll.apply_inserts(
		str(tmp_path / "clip.mp4"), str(tmp_path / "out.mp4"),
		[{"path": str(tmp_path / "gone.mp4"), "at": 0, "duration": 1}], 1080, 1920,
	)
	assert not ok
	assert "on disk" in err


# ── captions, transcript, versions: shared logic, guarded against drift ───────
def test_no_position_keeps_the_old_anchor():
	assert captions.caption_anchor(None, 162) == "\\an2"
	assert captions.caption_anchor({"x": None, "y": None}, 162) == "\\an2"


def test_position_anchors_on_the_centre():
	assert captions.caption_anchor({"x": 0.5, "y": 0.25}, 162) == "\\an5\\pos(81,72)"


def test_word_fix_replaces_whole_words_only():
	assert captions.apply_overrides("the app is an application", {"app": "clip"}) == \
		"the clip is an application"


def test_srt_uses_comma_milliseconds():
	out = transcript.to_srt([{"start": 0.0, "duration": 2.5, "text": "line"}])
	assert out.startswith("1\n00:00:00,000 --> 00:00:02,500\nline")


def test_txt_with_and_without_timestamps():
	segs = [{"start": 65.0, "duration": 1.0, "text": "line"}]
	assert transcript.to_txt(segs).startswith("[1:05] line")
	assert transcript.to_txt(segs, timestamps=False) == "line\n"


def test_versions_compare_numerically_not_as_strings():
	assert updates.is_newer("2026.1.10", "2026.1.9")
	assert not updates.is_newer("2026.1.9", "2026.1.10")


def test_the_server_never_claims_it_can_upgrade_ytdlp_itself(monkeypatch):
	"""The container is immutable; the fix is a redeploy. A button that seemed to
	work would be silently undone by the next deploy."""
	monkeypatch.setattr(updates, "_read_state", lambda: {"ytdlp_checked_at": 9e18,
	                                                     "ytdlp_latest": "2099.1.1"})
	result = updates.ytdlp_check()
	assert result["can_upgrade"] is False
	assert "redeploy" in result["how"].lower()


# ── the real renderer ─────────────────────────────────────────────────────────
def _make(path: str, seconds: int, size: str, colour: str = "testsrc") -> None:
	subprocess.run(
		["ffmpeg", "-y", "-f", "lavfi", "-i", f"{colour}=size={size}:rate=25:duration={seconds}",
		 "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
		 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
		 "-c:a", "aac", "-shortest", path],
		capture_output=True, check=True,
	)


@needs_ffmpeg
def test_broll_survives_a_real_ffmpeg_render(tmp_path):
	"""A filter graph that only exists as a string is not a feature."""
	clip, stock, out = (str(tmp_path / n) for n in ("clip.mp4", "stock.mp4", "out.mp4"))
	_make(clip, 6, "540x960")
	_make(stock, 4, "1280x720", colour="smptebars")

	ok, err = broll.apply_inserts(
		clip, out, [{"path": stock, "at": 1.0, "duration": 2.0, "mode": "cutaway"}], 540, 960)
	assert ok, err
	assert os.path.getsize(out) > 10_000


@needs_ffmpeg
def test_a_thumbnail_comes_out_as_a_real_jpeg(tmp_path):
	clip = str(tmp_path / "clip.mp4")
	thumb = str(tmp_path / "thumb.jpg")
	_make(clip, 4, "640x360")

	assert projects.make_thumbnail(clip, thumb, at=1.0, width=320)
	with open(thumb, "rb") as f:
		assert f.read(2) == b"\xff\xd8"          # JPEG SOI
