"""Projects: the work survives closing the tab.

Same feature as the desktop build, ported rather than copied — and the difference
matters. On the desktop there is one person and one `projects/` folder. Here every
function takes the **root** of the user whose projects these are, so one account's
library can never be listed, opened or deleted by another. That is the same rule
the clip endpoints already follow, for the same reason.

A project is one folder plus one JSON file:

    <user>/projects/<id>/project.json   title, source, transcript, clips
    <user>/projects/<id>/clips/         the rendered mp4s for this project
    <user>/projects/<id>/thumb.jpg      one frame, for the library grid

JSON on disk rather than a table, for two reasons. It is repairable — support can
read it over a screen share — and it survives the process being killed mid-write,
because every save goes to a temp file and is renamed over the old one. On a free
instance that spins down without warning, that is not a theoretical concern.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid

from . import proc, utils

MANIFEST = "project.json"
THUMB = "thumb.jpg"


def root_for(user_root: str) -> str:
	path = os.path.join(user_root, "projects")
	os.makedirs(path, exist_ok=True)
	return path


def path_for(user_root: str, project_id: str, *parts: str) -> str:
	"""A path inside one user's project, with the id checked first.

	The id arrives in a URL, so it is treated as hostile: only the hex ids this
	module hands out are accepted, which makes "../.." and absolute paths
	impossible rather than merely unlikely. The user root comes from the
	authenticated session, never from the request.
	"""
	pid = str(project_id or "").strip()
	if not pid or len(pid) != 12 or not all(c in "0123456789abcdef" for c in pid):
		raise ValueError("Unknown project")
	return os.path.join(root_for(user_root), pid, *parts)


def create(user_root: str, title: str, source_path: str = "", source_url: str = "",
           duration: float = 0.0) -> dict:
	"""Start a project and return its manifest."""
	pid = uuid.uuid4().hex[:12]
	folder = os.path.join(root_for(user_root), pid)
	os.makedirs(os.path.join(folder, "clips"), exist_ok=True)
	rec = {
		"id": pid,
		"title": (title or "Untitled").strip()[:120],
		"source_path": source_path,
		"source_url": source_url,
		"duration": float(duration or 0),
		"created_at": time.time(),
		"updated_at": time.time(),
		"transcript": None,
		"transcript_source": None,
		"moments": [],
		"clips": [],
	}
	save(user_root, rec)
	if source_path and os.path.isfile(source_path):
		make_thumbnail(source_path, os.path.join(folder, THUMB),
		               at=min(3.0, duration / 2 or 1))
	return rec


def save(user_root: str, rec: dict) -> dict:
	"""Write the manifest atomically. Losing this file loses the project."""
	rec["updated_at"] = time.time()
	folder = path_for(user_root, rec["id"])
	os.makedirs(folder, exist_ok=True)
	final = os.path.join(folder, MANIFEST)
	tmp = final + ".tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump(rec, f, indent=2)
	os.replace(tmp, final)
	return rec


def load(user_root: str, project_id: str) -> dict:
	with open(path_for(user_root, project_id, MANIFEST), encoding="utf-8") as f:
		return json.load(f)


def exists(user_root: str, project_id: str) -> bool:
	try:
		return os.path.isfile(path_for(user_root, project_id, MANIFEST))
	except ValueError:
		return False


def clips_dir(user_root: str, project_id: str) -> str:
	d = path_for(user_root, project_id, "clips")
	os.makedirs(d, exist_ok=True)
	return d


def listing(user_root: str) -> list[dict]:
	"""One user's projects, newest first, in the shape the library grid needs.

	A project whose manifest will not parse is skipped rather than raised on: one
	corrupt folder must not take down the whole library screen.
	"""
	base = root_for(user_root)
	out = []
	for name in os.listdir(base):
		folder = os.path.join(base, name)
		manifest = os.path.join(folder, MANIFEST)
		if not os.path.isfile(manifest):
			continue
		try:
			with open(manifest, encoding="utf-8") as f:
				rec = json.load(f)
		except (json.JSONDecodeError, OSError):
			continue
		out.append({
			"id": rec.get("id", name),
			"title": rec.get("title", "Untitled"),
			"duration": rec.get("duration", 0),
			"created_at": rec.get("created_at", 0),
			"updated_at": rec.get("updated_at", 0),
			"clip_count": len(rec.get("clips") or []),
			"has_transcript": bool(rec.get("transcript")),
			"has_thumb": os.path.isfile(os.path.join(folder, THUMB)),
			"source_missing": bool(rec.get("source_path")) and not os.path.isfile(rec["source_path"]),
		})
	out.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
	return out


def delete(user_root: str, project_id: str, keep_clips: bool = True) -> dict:
	"""Remove a project. By default the rendered clips are moved, not deleted.

	Deleting a project should not delete work the customer exported and may have
	scheduled to post. The mp4s move into that user's own clips folder, so they
	are still downloadable afterwards.
	"""
	folder = path_for(user_root, project_id)
	flat = os.path.join(user_root, "clips")
	moved = 0
	if keep_clips:
		src = os.path.join(folder, "clips")
		if os.path.isdir(src):
			os.makedirs(flat, exist_ok=True)
			for name in os.listdir(src):
				a = os.path.join(src, name)
				if not os.path.isfile(a):
					continue
				b = os.path.join(flat, name)
				stem, ext = os.path.splitext(name)
				n = 2
				while os.path.exists(b):
					b = os.path.join(flat, f"{stem}_{n}{ext}")
					n += 1
				try:
					shutil.move(a, b)
					moved += 1
				except OSError:
					pass
	shutil.rmtree(folder, ignore_errors=True)
	return {"ok": True, "clips_kept": moved}


def make_thumbnail(video_path: str, output_path: str, at: float = 3.0,
                   width: int = 480) -> bool:
	"""One frame as a jpg. False if it could not be made — never fatal.

	A missing thumbnail costs a grey tile in the library. Failing the operation
	that asked for it would cost the project.
	"""
	if not os.path.isfile(video_path):
		return False
	os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
	cmd = [
		"ffmpeg", "-y", "-ss", f"{max(0.0, at):.2f}", "-i", video_path,
		"-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
		output_path,
	]
	try:
		result = proc.run(cmd)
	except Exception:
		return False
	return result.returncode == 0 and os.path.isfile(output_path)


def clip_thumb_path(user_root: str, project_id: str, clip_name: str) -> str:
	return path_for(user_root, project_id, "thumbs",
	                f"{utils.sanitize(clip_name)[:60] or 'clip'}.jpg")
