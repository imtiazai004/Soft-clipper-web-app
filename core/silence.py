"""Cut a video into fixed-length clips that start and end in silence.

This is the path for people who do not want to involve an AI at all: no API
key, no transcript, no upload — just "give me 60-second clips" on a video that
has no speech to analyse, or where the whole thing is worth posting and only
the cutting is tedious.

The trick is not the fixed length, it is where the cut lands. Slicing every
exactly 60 seconds chops sentences in half. Slicing at the nearest pause makes
the same clip feel edited, and ffmpeg's own silencedetect filter finds those
pauses for free in one pass over the audio.
"""
from __future__ import annotations

import re

from . import proc

# Anything quieter than this, for at least this long, counts as a pause. -30 dB
# is quiet enough to ignore room tone and breathing but still catches the gap
# between two sentences; 0.35 s is about the shortest gap a listener reads as a
# break rather than as an edit.
NOISE_DB = -30
MIN_SILENCE = 0.35

# How far from the target length we are willing to move to land on a pause.
# Beyond this the clip stops being the length the user asked for.
SNAP_WINDOW = 6.0

LENGTHS = [30, 45, 60, 90, 120]


def detect_silences(path: str, noise_db: int = NOISE_DB, min_dur: float = MIN_SILENCE) -> list[tuple[float, float]]:
	"""Find quiet stretches as (start, end) seconds.

	Returns an empty list if ffmpeg is unhappy — the caller then falls back to
	plain fixed cuts, which is worse but never a failure.
	"""
	cmd = [
		"ffmpeg", "-hide_banner", "-nostats", "-i", path,
		"-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
		"-f", "null", "-",
	]
	try:
		# proc.run already captures both streams and honours job cancellation.
		result = proc.run(cmd, errors="ignore")
	except Exception:
		return []

	# silencedetect writes to stderr, one line per boundary:
	#   [silencedetect @ ...] silence_start: 12.345
	#   [silencedetect @ ...] silence_end: 13.001 | silence_duration: 0.656
	text = (result.stderr or "") + (result.stdout or "")
	starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", text)]
	ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", text)]

	pairs = []
	for i, start in enumerate(starts):
		end = ends[i] if i < len(ends) else None
		if end is not None and end > start:
			pairs.append((start, end))
	return pairs


def _snap(target: float, silences: list[tuple[float, float]], window: float = SNAP_WINDOW) -> float:
	"""Move `target` to the middle of the nearest pause, if one is close enough.

	The middle rather than the edge: cutting at the start of a pause clips the
	last word's tail, and cutting at the end opens the next clip on a hard
	consonant. The middle leaves a little air on both sides.
	"""
	best, best_gap = target, window
	for start, end in silences:
		middle = (start + end) / 2
		gap = abs(middle - target)
		if gap < best_gap:
			best, best_gap = middle, gap
	return best


def plan_fixed_clips(
	duration: float,
	target_len: int,
	silences: list[tuple[float, float]] | None = None,
	min_tail: float = 10.0,
) -> list[dict]:
	"""Split `duration` into clips of about `target_len`, cut at pauses.

	`min_tail` drops a final scrap shorter than this — a 4-second clip of
	someone saying goodbye is not worth an upload slot.
	"""
	silences = silences or []
	if duration <= 0 or target_len <= 0:
		return []
	if duration <= target_len:
		return [{"start": 0.0, "end": round(duration, 2), "hook_title": "Clip 1", "reason": "Whole video"}]

	clips: list[dict] = []
	position = 0.0
	while position < duration - 1:
		target_end = min(position + target_len, duration)
		# Never snap the last cut — it is the end of the video, not a pause.
		end = target_end if target_end >= duration else _snap(target_end, silences)
		end = min(max(end, position + 5.0), duration)

		if duration - end < min_tail:
			end = duration

		clips.append(
			{
				"start": round(position, 2),
				"end": round(end, 2),
				"hook_title": f"Clip {len(clips) + 1}",
				"reason": f"Fixed {target_len}s cut, trimmed to the nearest pause",
			}
		)
		position = end
		if end >= duration:
			break

	return clips


def split_video(path: str, duration: float, target_len: int) -> list[dict]:
	"""Everything together: find the pauses, then plan the clips."""
	return plan_fixed_clips(duration, target_len, detect_silences(path))
