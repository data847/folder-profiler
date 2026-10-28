"""A run must not get slower as it goes: the time per project stays flat when the number of projects grows (it used to double: the progress file
was rewritten in full after every chunk, and per-project scans walked every folder / every chunk file of the whole run)."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def per_project(n, t):
    root = os.path.join(t, f"t{n}")
    for p in range(n):
        d = os.path.join(root, f"P{p:05d}", "a", "b"); os.makedirs(d)
        for i in range(20): open(os.path.join(d, f"f{i}.txt"), "w").write("x")
    best = 1e9
    for k in range(2):
        t0 = time.time(); fp.profile(root, os.path.join(t, f"o{n}_{k}"), log=lambda *_: None, resume=False, system_info=False, workers=8)
        best = min(best, time.time() - t0)
    return best / n * 1000
with tempfile.TemporaryDirectory() as t:
    small, big = per_project(250, t), per_project(1500, t)
    print(f"     ms per project: 250 projects {small:.1f}, 1500 projects {big:.1f}")
    check("6x the projects costs less than 1.7x per project (it was 2.6x and growing)", big < small * 1.7, True)
sys.exit(1 if any(bad) else 0)
