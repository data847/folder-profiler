"""A file with a corrupt modified date (year 60369, before 1970, NaN) must not stop a run: it is listed with no date."""
import math, os, sys, tempfile, zipfile, csv, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
check("a normal date is unchanged", fp.iso(1_700_000_000), "2023-11-14T22:13:20Z")
check("year 60369 -> no date, no crash", fp.iso(1.2e12), "")
check("absurdly large / small / infinite / NaN -> no date", [fp.iso(x) for x in (2 ** 62, -2 ** 62, float("inf"), -float("inf"), float("nan"))], [""] * 5)
# end to end: a file whose stat() reports an impossible date
with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "r", "P"); os.makedirs(root)
    for n in ("a.txt", "b.txt", "c.txt"): open(os.path.join(root, n), "w").write("x")
    real_iso = fp.iso; calls = []
    import time as _t
    orig_fromts = fp.datetime.fromtimestamp
    class FakeDT:                                   # make exactly one file look like the year 60369
        @staticmethod
        def fromtimestamp(ts, tz=None):
            if os.path.exists(os.path.join(root, "b.txt")) and abs(ts - os.stat(os.path.join(root, "b.txt")).st_mtime) < 1e-3: raise ValueError("year 60369 is out of range")
            return orig_fromts(ts, tz)
    fp.datetime = type("D", (), {"fromtimestamp": staticmethod(FakeDT.fromtimestamp), "now": staticmethod(orig_fromts.__self__.now) if hasattr(orig_fromts, "__self__") else None})
    try:
        z = fp.profile(os.path.join(t, "r"), os.path.join(t, "o"), log=lambda *_: None, resume=False, system_info=False, workers=2)[0]
    except Exception as e:
        z = None; print("run crashed:", type(e).__name__, e)
    finally:
        pass
    check("the run finishes", z is not None, True)
sys.exit(1 if any(bad) else 0)
