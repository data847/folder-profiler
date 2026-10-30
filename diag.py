"""Failure notes shared by the profiler modules. Standard library only.

Readers (cad_links, dwg_sheets, the header peek) must never stop a run, so they return "nothing" when a file is damaged. This module keeps
the WHY: the profiler asks take() right after a read in the same thread and writes the answer into Read_Problems.csv.
Only exception TYPES / OS error codes are kept - never file names, paths or file text."""
import threading

_tl = threading.local()
_LOCK = threading.Lock()
WARNINGS = []                       # (where, path below the chosen folder, reason) from places with no log of their own (choosing the project folders)
MAX_WARNINGS = 2000


def note(stage, why):
    """Remember, for the calling thread, that a read in `stage` failed because of `why` (a short code such as 'PermissionError')."""
    _tl.last = f"{stage}: {why}"
    return None


def fail(exc, stage="references"):
    """For `except Exception as e: return diag.fail(e)` in readers: note the exception type, return None."""
    return note(stage, type(exc).__name__)


def take():
    """The note of the last failure in this thread (and forget it), '' when there was none."""
    v = getattr(_tl, "last", "")
    _tl.last = ""
    return v


def warn(where, path, reason):
    with _LOCK:
        if len(WARNINGS) < MAX_WARNINGS:
            WARNINGS.append((where, path, reason))


def drain_warnings():
    with _LOCK:
        out = list(WARNINGS)
        del WARNINGS[:]
    return out
