"""The customer's own logo, stamped on every clip they export.

Not ours. Nothing this app produces carries Soft Clipper branding, and it never
should: these are people who have paid, and putting our mark on their work is
something you do to a free tier to make it annoying enough to upgrade from.

What this replaces is real drudgery. Someone posting thirty clips a week opens
every one of them again in Canva or CapCut, drops a logo in the corner, and
exports a second time — for a decoration that never changes. Set once here and
it is on every clip that comes out.

**Why a second pass.** The logo is burnt into the finished clip rather than
folded into the render that produced it. Folding it in would be one encode
instead of two and is the better answer on quality; it would also mean touching
every branch of render_clip, the dynamic smart-crop path and the concat path,
each of which builds its filter graph differently. Doing it here means one
implementation covers every one of them, and — the part that decided it — a
customer with no logo set gets a render that is byte for byte what it was
before, because none of that code is reached at all.

The cost is honest: one more encode of an already-short clip, no face tracking
involved, so seconds rather than the minutes the first pass takes. Audio is
copied rather than re-encoded. B-roll made the same trade for the same reasons.
"""
from __future__ import annotations

import os
import shutil

from . import proc

# Where the logo is kept once chosen, under the folder that belongs to one
# account. Per-user and not shared, for the obvious reason: this is somebody's
# branding, and two people using the same server must never end up wearing each
# other's logo.
LOGO_DIR = "brand"

# What a logo may be. PNG first and by a distance: it is the only one of these
# that carries transparency, and a logo on an opaque white square is not a
# watermark, it is a sticker.
ALLOWED = (".png", ".webp", ".jpg", ".jpeg", ".gif", ".bmp")

# Nine anchors, as fractions of the frame. ffmpeg is given the fraction and
# works the pixels out itself — see overlay_filter — so these stay true at any
# output size and for any logo shape.
POSITIONS = {
	"top-left": (0.04, 0.04),
	"top": (0.5, 0.04),
	"top-right": (0.96, 0.04),
	"left": (0.04, 0.5),
	"centre": (0.5, 0.5),
	"right": (0.96, 0.5),
	"bottom-left": (0.04, 0.96),
	"bottom": (0.5, 0.96),
	"bottom-right": (0.96, 0.96),
}
DEFAULT_POSITION = "top-right"

# Percentages of the frame width. 12% is about what a channel logo occupies on
# a clip that does not look like an advert for the logo.
DEFAULT_SCALE_PCT = 12
MIN_SCALE_PCT, MAX_SCALE_PCT = 2, 60

# Slightly transparent by default. A fully opaque logo competes with the
# footage; at 0.85 it reads as a mark rather than as part of the picture.
DEFAULT_OPACITY = 0.85


def logo_dir(user_root: str) -> str:
	return os.path.join(user_root, LOGO_DIR)


def store_logo(user_root: str, source_path: str) -> str:
	"""Copy a chosen image into our own folder and return where it landed.

	Raises ValueError with something a person can act on. The caller turns that
	into a message; nothing here knows about HTTP.
	"""
	source_path = (source_path or "").strip()
	if not source_path or not os.path.isfile(source_path):
		raise ValueError("That file is not there any more.")

	ext = os.path.splitext(source_path)[1].lower()
	if ext not in ALLOWED:
		raise ValueError(
			f"{ext or 'That'} is not an image we can use. Choose a PNG — or a "
			"WebP, JPG, GIF or BMP."
		)
	if os.path.getsize(source_path) > 20 * 1024 * 1024:
		raise ValueError("That image is over 20 MB. A logo does not need to be.")

	folder = logo_dir(user_root)
	os.makedirs(folder, exist_ok=True)
	# One logo, one name. Keeping the original filename would accumulate every
	# logo anyone ever tried, and leave the old ones on disk for good.
	dest = os.path.join(folder, f"logo{ext}")
	for stale in os.listdir(folder):
		if stale.startswith("logo") and os.path.join(folder, stale) != dest:
			try:
				os.remove(os.path.join(folder, stale))
			except OSError:
				pass
	if os.path.abspath(source_path) != os.path.abspath(dest):
		shutil.copyfile(source_path, dest)
	return dest


def settings(cfg: dict) -> dict:
	"""The brand settings as the renderer wants them, defaults filled in.

	Everything is clamped here rather than trusted. These arrive from a saved
	config file and from request bodies, and an opacity of 40 or a scale of 900
	should produce a sane clip rather than a filter graph ffmpeg rejects.
	"""
	cfg = cfg or {}
	path = str(cfg.get("brand_logo", "") or "")
	position = str(cfg.get("brand_position", "") or DEFAULT_POSITION)
	if position not in POSITIONS:
		position = DEFAULT_POSITION

	def number(key, default, low, high):
		try:
			value = float(cfg.get(key, default))
		except (TypeError, ValueError):
			return default
		return max(low, min(high, value))

	# x/y are fractions of the frame, the same convention the caption and text
	# overlays use, so the same draggable chip in the preview drives all three.
	# Absent means "wherever the chosen anchor is" rather than a hard 0.
	anchor = POSITIONS[position]
	return {
		"enabled": bool(cfg.get("brand_enabled", False)) and bool(path),
		"path": path,
		"position": position,
		"x": number("brand_x", anchor[0], 0.0, 1.0),
		"y": number("brand_y", anchor[1], 0.0, 1.0),
		"scale_pct": int(number("brand_scale_pct", DEFAULT_SCALE_PCT, MIN_SCALE_PCT, MAX_SCALE_PCT)),
		"opacity": number("brand_opacity", DEFAULT_OPACITY, 0.05, 1.0),
	}


def _escape(path: str) -> str:
	"""A path ffmpeg's filter parser will accept.

	The same problem the subtitle filter has, and the same fix: backslashes are
	escapes inside a filter argument, and a Windows drive letter's colon reads
	as an option separator. C:\\Users\\... arrives as an unknown option.
	"""
	return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def overlay_filter(logo: dict, frame_width: int) -> str:
	"""The filter graph that puts the logo on `[0:v]`, ending in `[out]`.

	Pure and separate from running anything, because this is the part that is
	worth checking: an off-by-one in the position expression is a logo half off
	the frame, and the only other way to find that out is to watch a render.

	Position is handed to ffmpeg as a fraction of the *free* space — (W-w)*x —
	so 0 is flush left, 1 is flush right, and the logo can never hang over the
	edge whatever size it ends up. Working the pixels out here instead would
	need the logo's own dimensions, which means decoding it first.
	"""
	width = max(2, int(frame_width * logo["scale_pct"] / 100))
	# -1 keeps the aspect ratio; forcing it to an even number avoids the
	# "width not divisible by 2" refusals that some scalers raise.
	scale = f"scale={width - width % 2}:-1"
	# format=rgba first: colorchannelmixer cannot touch an alpha channel that is
	# not there, so on a JPEG the opacity would silently do nothing.
	fade = f"format=rgba,colorchannelmixer=aa={logo['opacity']:.3f}"
	place = f"overlay=x='(W-w)*{logo['x']:.4f}':y='(H-h)*{logo['y']:.4f}'"
	return (
		f"movie='{_escape(logo['path'])}',{scale},{fade}[wm];"
		f"[0:v][wm]{place}[out]"
	)


def apply_logo(clip_path: str, output_path: str, logo: dict,
               frame_width: int) -> tuple[bool, str | None]:
	"""Burn the logo into an already-rendered clip. Returns (ok, error)."""
	if not logo.get("enabled"):
		return False, "No logo is set."
	if not os.path.isfile(logo["path"]):
		# The one failure worth naming precisely: the setting is on, so the
		# customer believes every clip carries their logo, and the honest
		# outcome is to say the file went rather than quietly ship it without.
		return False, "The logo image is no longer where it was saved."

	cmd = [
		"ffmpeg", "-y", "-i", clip_path,
		"-filter_complex", overlay_filter(logo, frame_width),
		"-map", "[out]", "-map", "0:a?",
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		# Already the encode we want; copying saves a generation of loss and
		# most of the time this pass costs.
		"-c:a", "copy",
		"-movflags", "+faststart",
		output_path,
	]
	result = proc.run(cmd)
	if result.returncode != 0:
		return False, (result.stderr or "")[-300:]
	return True, None
