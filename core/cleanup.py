"""Age out downloaded videos and rendered clips so a small disk can't fill up.

Nothing here ever deleted itself before: a source video is 1-2 GB and every clip
is kept forever, so a 40 GB box dies after a couple of dozen videos — and the
failure shows up as a confusing download or render error, not as "disk full".

Two rules keep this from eating work in progress:

  * a file is only old enough if *nothing* in it has been touched inside the TTL
  * anything a live session still points at is protected regardless of age

The caller supplies that protected set, because only it knows what is loaded.
"""
import os
import shutil
import threading
import time

# how often the janitor wakes up; the TTL is what actually decides deletion
SWEEP_INTERVAL = 30 * 60


def _newest_mtime(path: str) -> float:
    """Most recent mtime among the files in a file or directory.

    A clip folder is one unit of work: judging it by its newest file stops a
    half-hour-old re-render from being thrown away because its siblings are old.

    The folder's *own* mtime is deliberately ignored — it moves whenever an entry
    is added or removed, so a re-render that deletes the previous version makes
    an otherwise ancient folder look brand new. Only content age is honest. An
    empty folder has no content to ask, so it falls back to its own timestamp.
    """
    try:
        if os.path.isfile(path):
            return os.path.getmtime(path)
        newest = None
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    mtime = os.path.getmtime(os.path.join(root, name))
                except OSError:
                    continue
                newest = mtime if newest is None else max(newest, mtime)
        return newest if newest is not None else os.path.getmtime(path)
    except OSError:
        return time.time()      # can't tell -> treat as fresh, never delete


def _size_of(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _protects(entry: str, protected: set[str]) -> bool:
    """True if a protected path is this entry or lives inside it."""
    entry_abs = os.path.abspath(entry)
    for p in protected:
        if not p:
            continue
        p_abs = os.path.abspath(p)
        if p_abs == entry_abs or p_abs.startswith(entry_abs + os.sep):
            return True
    return False


def sweep(roots, max_age_hours: float, protected=None) -> dict:
    """Delete entries directly under each root that are older than the TTL.

    Only one level down: `downloads/video.mp4` and `clips/<clip_folder>` are the
    units, so a clip's parts never get removed out from under it.
    Returns {"removed": n, "freed_mb": x, "kept_in_use": k}.
    """
    if not max_age_hours or max_age_hours <= 0:
        return {"removed": 0, "freed_mb": 0.0, "kept_in_use": 0}

    protected = set(protected or ())
    cutoff = time.time() - max_age_hours * 3600
    removed = freed = kept = 0

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            path = os.path.join(root, name)
            if _protects(path, protected):
                kept += 1
                continue
            if _newest_mtime(path) > cutoff:
                continue
            size = _size_of(path)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                else:
                    shutil.rmtree(path)
            except OSError:
                continue        # locked by a running ffmpeg; next sweep gets it
            removed += 1
            freed += size

    return {"removed": removed, "freed_mb": round(freed / 1e6, 1), "kept_in_use": kept}


def start_janitor(collect, max_age_hours: float, interval: float = SWEEP_INTERVAL,
                  on_sweep=None) -> threading.Thread | None:
    """Run sweep() forever in the background.

    collect() -> (roots, protected), called fresh each pass so newly loaded
    videos are protected and new users' folders are included.
    """
    if not max_age_hours or max_age_hours <= 0:
        return None

    def loop():
        while True:
            time.sleep(interval)
            try:
                roots, protected = collect()
                stats = sweep(roots, max_age_hours, protected)
                if stats["removed"] and on_sweep:
                    on_sweep(stats)
            except Exception:
                pass            # a janitor must never take the app down with it

    thread = threading.Thread(target=loop, daemon=True, name="cleanup-janitor")
    thread.start()
    return thread
