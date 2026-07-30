"""B-roll: stock footage cut over a clip while the speaker keeps talking.

The reason this feature exists is retention. A single talking head for sixty
seconds loses people; the same audio with two short cutaways to relevant footage
does not. It is the one thing every viral-clip tool has that we did not.

Two sources, because between them they cover everything and both have a free
tier: Pexels Videos and Pixabay. Each needs the customer's own free key, entered
in Settings — no key of ours is shipped, so nothing here can be rate-limited or
revoked for everyone at once.

The insert is applied as a **second pass over the finished clip**, not folded
into the render. That is deliberate. render_clip already builds a filter graph
with reframing, effects and burnt captions in it, and threading an extra input
through every branch of that graph is how a working renderer stops working.
Overlaying afterwards costs one more encode of a short clip and cannot break the
path everything else depends on.

Audio is never touched: the cutaway replaces the picture and the speaker keeps
talking underneath, which is what a cutaway is.

Ported from the desktop build, with one difference throughout: the caller passes
the **settings dict and the destination folder** in rather than this module
reading a global config. On a server the config and the download folder both
belong to one user, and a module that reaches for a global would quietly spend
one account's Pexels quota on another's search.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import proc, utils

# Desktop-shaped fallback, used only when no folder is supplied.
BROLL_DIR = os.path.join("downloads", "broll")

# Where each source's free API lives. Both return JSON and both cap page size at
# a value near this, so there is no pagination to design around for a picker.
PEXELS_SEARCH = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH = "https://pixabay.com/api/videos/"

SOURCES = {
	"pexels": {
		"label": "Pexels",
		"config_key": "pexels_api_key",
		"signup": "https://www.pexels.com/api/new/",
	},
	"pixabay": {
		"label": "Pixabay",
		"config_key": "pixabay_api_key",
		"signup": "https://pixabay.com/api/docs/",
	},
}

MODES = ("cutaway", "pip")


class BrollError(RuntimeError):
	"""A message already written for the customer."""


def _opener(cfg: dict | None = None):
	"""urllib opener that honours this user's proxy.

	Same reasoning as the downloader: on a server the datacenter IP is exactly the
	kind YouTube and friends block, so the proxy that makes downloads work has to
	carry the stock-footage search too — otherwise the feature looks broken rather
	than blocked.
	"""
	proxy = str((cfg or {}).get("proxy", "")).strip()
	if proxy:
		return urllib.request.build_opener(
			urllib.request.ProxyHandler({"http": proxy, "https": proxy})
		)
	return urllib.request.build_opener()


def _get_json(url: str, headers: dict | None = None, timeout: int = 25,
              cfg: dict | None = None) -> dict:
	req = urllib.request.Request(url, headers=headers or {})
	try:
		with _opener(cfg).open(req, timeout=timeout) as r:
			return json.loads(r.read().decode("utf-8", "replace"))
	except urllib.error.HTTPError as e:
		if e.code in (401, 403):
			raise BrollError("That stock-footage key was rejected. Check it in Settings.")
		if e.code == 429:
			raise BrollError("The stock-footage service is rate-limiting you. Try again in a minute.")
		raise BrollError(f"Stock footage search failed (HTTP {e.code}).")
	except Exception as e:
		raise BrollError(f"Could not reach the stock-footage service: {e}")


def key_for(source: str, cfg: dict) -> str:
	meta = SOURCES.get(source)
	if not meta:
		raise BrollError(f"Unknown stock-footage source: {source}")
	return str((cfg or {}).get(meta["config_key"], "")).strip()


def configured(cfg: dict) -> list[str]:
	"""Which sources this user has a key for — the UI hides the rest."""
	return [sid for sid in SOURCES if key_for(sid, cfg)]


def _pick_pexels_file(files: list[dict]) -> dict | None:
	"""The smallest file at least 1080 tall, else the largest available.

	A 4K cutaway is scaled down to 1080 anyway, so downloading it wastes the
	customer's bandwidth and their patience for no visible gain.
	"""
	usable = [f for f in files if f.get("link") and (f.get("height") or 0) >= 1080]
	if usable:
		return min(usable, key=lambda f: f.get("height", 0))
	with_link = [f for f in files if f.get("link")]
	return max(with_link, key=lambda f: f.get("height", 0)) if with_link else None


def _pick_pixabay_file(videos: dict) -> dict | None:
	for size in ("large", "medium", "small", "tiny"):
		v = videos.get(size) or {}
		if v.get("url"):
			return v
	return None


def search(query: str, cfg: dict, source: str = "pexels", per_page: int = 15,
           orientation: str = "portrait") -> list[dict]:
	"""Search one source with this user's key. Returns a shape the picker renders.

	`orientation` defaults to portrait because the clips are vertical: a
	landscape cutaway has to be cropped to 9:16 and loses most of its frame.
	"""
	query = query.strip()
	if not query:
		raise BrollError("Type what footage you want first.")
	key = key_for(source, cfg)
	if not key:
		raise BrollError(
			f"Add your free {SOURCES[source]['label']} key in Settings to search stock footage."
		)
	per_page = max(1, min(int(per_page), 30))

	out: list[dict] = []
	if source == "pexels":
		url = f"{PEXELS_SEARCH}?" + urllib.parse.urlencode({
			"query": query, "per_page": per_page, "orientation": orientation,
		})
		data = _get_json(url, {"Authorization": key}, cfg=cfg)
		for v in data.get("videos", []):
			f = _pick_pexels_file(v.get("video_files", []))
			if not f:
				continue
			pictures = v.get("video_pictures") or []
			out.append({
				"id": f"pexels-{v.get('id')}",
				"source": "pexels",
				"thumb": (pictures[0].get("picture") if pictures else v.get("image")) or "",
				"url": f["link"],
				"width": f.get("width") or v.get("width") or 0,
				"height": f.get("height") or v.get("height") or 0,
				"duration": float(v.get("duration") or 0),
				"credit": f"{(v.get('user') or {}).get('name', 'Pexels')} / Pexels",
			})
	else:
		url = f"{PIXABAY_SEARCH}?" + urllib.parse.urlencode({
			"key": key, "q": query, "per_page": per_page, "video_type": "film",
		})
		data = _get_json(url, cfg=cfg)
		for v in data.get("hits", []):
			f = _pick_pixabay_file(v.get("videos", {}))
			if not f:
				continue
			out.append({
				"id": f"pixabay-{v.get('id')}",
				"source": "pixabay",
				# Pixabay does not return a still, but every video has a frame
				# grab at this predictable URL.
				"thumb": f"https://i.vimeocdn.com/video/{v.get('picture_id')}_295x166.jpg"
				if v.get("picture_id") else "",
				"url": f["url"],
				"width": f.get("width") or 0,
				"height": f.get("height") or 0,
				"duration": float(v.get("duration") or 0),
				"credit": f"{v.get('user', 'Pixabay')} / Pixabay",
			})
	return out


def fetch(url: str, dest_dir: str = "", name_hint: str = "broll",
          cfg: dict | None = None) -> str:
	"""Download one clip into this user's folder and return its path.

	Cached by name: picking the same footage for a second clip does not download
	it twice. Per-user rather than shared, because a shared cache would let one
	account's filename collide with another's.
	"""
	folder = dest_dir or BROLL_DIR
	os.makedirs(folder, exist_ok=True)
	safe = utils.sanitize(name_hint)[:50] or "broll"
	path = os.path.join(folder, f"{safe}.mp4")
	if os.path.isfile(path) and os.path.getsize(path) > 100_000:
		return path
	req = urllib.request.Request(url, headers={"User-Agent": "SoftClipper"})
	try:
		with _opener(cfg).open(req, timeout=120) as r, open(path, "wb") as f:
			while True:
				chunk = r.read(1 << 20)
				if not chunk:
					break
				f.write(chunk)
	except Exception as e:
		# A half-written file would be cached as if it were good.
		if os.path.exists(path):
			try:
				os.remove(path)
			except OSError:
				pass
		raise BrollError(f"Could not download that footage: {e}")
	return path


def _insert_chain(index: int, label_in: str, label_out: str, ins: dict,
                  width: int, height: int) -> str:
	"""One overlay stage: input `index` painted over `label_in` for its window."""
	at = max(0.0, float(ins.get("at", 0)))
	dur = max(0.3, float(ins.get("duration", 3)))
	mode = ins.get("mode", "cutaway")

	if mode == "pip":
		# Corner picture-in-picture: a quarter of the width, inset by 4%.
		pw = _even(width // 3)
		fit = f"scale={pw}:-2"
		place = f"x=W-w-{int(width * 0.04)}:y={int(height * 0.06)}"
	else:
		# Full-frame cutaway: fill the frame and crop, never letterbox.
		fit = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
		place = "x=0:y=0"

	# setpts shifts the footage onto the clip's clock so `enable` and the frames
	# agree; without it the overlay shows whatever frame the source happened to
	# be at when the window opened.
	return (
		f"[{index}:v]{fit},trim=0:{dur:.3f},setpts=PTS-STARTPTS+{at:.3f}/TB[b{index}];"
		f"[{label_in}][b{index}]overlay={place}:"
		f"enable='between(t,{at:.3f},{at + dur:.3f})':eof_action=pass[{label_out}];"
	)


def _even(v: int) -> int:
	return max(2, int(v) // 2 * 2)


def apply_inserts(clip_path: str, output_path: str, inserts: list[dict],
                  width: int, height: int) -> tuple[bool, str | None]:
	"""Burn B-roll into an already-rendered clip. Returns (ok, error).

	`inserts` = [{"path", "at", "duration", "mode"}] in clip time, seconds.
	The clip's own audio is mapped straight through untouched.
	"""
	usable = [i for i in inserts if i.get("path") and os.path.isfile(i["path"])]
	if not usable:
		return False, "None of the chosen footage is on disk any more."

	cmd = ["ffmpeg", "-y", "-i", clip_path]
	for ins in usable:
		cmd += ["-i", ins["path"]]

	chain = ""
	label = "0:v"
	for n, ins in enumerate(usable, start=1):
		out_label = f"v{n}"
		chain += _insert_chain(n, label, out_label, ins, _even(width), _even(height))
		label = out_label

	cmd += [
		"-filter_complex", chain.rstrip(";"),
		"-map", f"[{label}]", "-map", "0:a?",
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		# The audio is already the encode we want; copying it saves a generation
		# of loss as well as the time.
		"-c:a", "copy",
		"-movflags", "+faststart",
		output_path,
	]
	result = proc.run(cmd)
	if result.returncode != 0:
		return False, (result.stderr or "")[-300:]
	return True, None
