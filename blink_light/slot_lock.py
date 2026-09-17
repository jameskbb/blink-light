"""One cross-process lock, so "is this slot due?" and "claim it" are one step.

Three drivers can fire the same effect - the hourly scheduled task, the chime
loop, and the watcher. Each used to read the dedupe file, see an unfired slot,
and start playing without knowing another had just decided the same thing. Two
processes writing pattern lines to one blink(1) interleave, and the loser's
half-written pattern can leave the light holding a colour with nothing left to
fade it back down: the chime that "flashed and then stayed on".

The lock is an OS lock on an open handle, not a file whose mere existence means
"held". Windows and POSIX both drop it when the handle closes or the process
dies, so a killed loop leaves nothing stale behind and the next run has nothing
to break. Everything here is built so a caller cannot be stuck waiting:

* acquisition is non-blocking behind a short deadline, and a caller that misses
  it is told so rather than queued - whoever holds it is firing the same slot,
  so standing down is the wanted behaviour;
* it is held across a state-file read and write only, never across the play,
  which for the daily show runs a full minute;
* a re-entry from the same thread is a no-op, because re-locking our own file
  from a second handle fails on Windows and would burn the whole timeout.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import time
from typing import Iterator

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


# Long enough for a state-file read and an atomic replace on a busy disk, short
# enough that a caller who cannot get in gives up while its slot is still the
# current one.
LOCK_TIMEOUT_SECONDS = 2.0
RETRY_SECONDS = 0.02

_held = threading.local()


def _try_acquire(handle) -> bool:
    if msvcrt is not None:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    # No locking primitive at all: an unserialised fire beats no fire.
    return True


def _release(handle) -> None:
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # Closing the handle releases it anyway; nothing left to salvage.
        pass


@contextmanager
def slot_lock(path: Path, timeout_seconds: float | None = None) -> Iterator[bool]:
    """Hold the claim lock, yielding whether it was actually acquired.

    Yields False instead of raising or waiting: a caller that cannot get in is
    racing another driver over the same slot, and that driver is about to fire
    it.

    The default deadline is read at call time, not bound at import, so a test
    can shorten it without every caller passing one through.
    """
    timeout = LOCK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if getattr(_held, "depth", 0):
        yield True
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode creates the file without ever truncating it; the bytes
        # are irrelevant, only the handle is.
        handle = open(path, "a+b")
    except OSError:
        # The lock is a collision guard, not a correctness requirement for a
        # single driver. A runtime dir we cannot write must not cost the chime.
        yield True
        return

    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            handle.seek(0)
            acquired = _try_acquire(handle)
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(RETRY_SECONDS)
        if acquired:
            _held.depth = getattr(_held, "depth", 0) + 1
        yield acquired
    finally:
        if acquired:
            _held.depth -= 1
            _release(handle)
        handle.close()
