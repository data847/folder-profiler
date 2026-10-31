"""Resume check in parallel: the threaded project fingerprint equals the sequential one on an awkward tree; a resumed run skips unchanged finished
projects with the fast check, catches every kind of change (size, date, added, removed, renamed), falls back to the walk for projects with links and
for progress saved without a fingerprint (and saves one), and still ends with exactly an uninterrupted run's output."""
import csv, io, json, os, sys, tempfile, time, zipfile, collections
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
def rows_of(z): return [{k: v for k, v in r.items() if k != "dir_id"} for r in rd(z, "Files.csv")]
fp.PIPELINE_CHUNKS = 0
class Boom(Exception): pass
def mk(path, size, mt=1_700_000_000):
    os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(b"x" * size); os.utime(path, (mt, mt))

with tempfile.TemporaryDirectory() as t:
    # ---- 1. parity of the two fingerprints on an awkward tree
    root = os.path.join(t, "par")
    for rel, size in [("A/a.dwg", 5), ("A/sub/deep/er/b.pdf", 7), ("A/ünï cödé.txt", 3), ("A/Thumbs.db", 9), ("A/._junk", 2), ("A/empty.txt", 0),
                      ("B/x, y \"q\".dwg", 4), ("B/n/n/n/n/n/z.sldprt", 6), ("loose.txt", 8)]:
        mk(os.path.join(root, *rel.split("/")), size)
    os.makedirs(os.path.join(root, "B", "emptydir"))
    units, label, _ = fp.resolve_units(root, None, None, 1)
    res = fp.scan_tree(units, 3, None, fingerprint=set(range(len(units))))
    same = []
    for ui, (name, base, recurse) in enumerate(units):
        acc = [0, 0, 0]; fp.chunk_fingerprints(base, recurse, 50, acc)
        same.append((name, res["fps"][ui] == acc, acc[1]))
    print("   ", same)
    check("threaded fingerprint == sequential fingerprint for every project (incl. junk, unicode, empty files, loose files)", all(x[1] for x in same), True)
    check("junk files are not in the fingerprint (A holds 4 counted files)", [x[2] for x in same if x[0] == "A"], [4])
    # ---- 2. resume behaviour
    src = os.path.join(t, "src")
    for p in range(5):
        for i in range(12):
            mk(os.path.join(src, f"P{p}", f"s{i % 3}", f"f{i:02d}.sldprt"), i + 1 + p)
    os.symlink(os.path.join(src, "P2", "s0", "f00.sldprt"), os.path.join(src, "P0", "link.sldprt"))        # P0 (finished before the crash) holds a link: no fast check for it
    msgs = []
    def run(out, **kw):
        z, *_ = fp.profile(src, out, log=lambda m: msgs.append(m), workers=2, chunk_files=5, system_info=False, tier=1, **kw); return z
    base = rows_of(run(os.path.join(t, "base")))
    def crash_then(out, mutate=None, manifest=None):
        def boom(ui, ch):
            if (ui, ch) == (4, 0): raise Boom("x")
        fp._TEST_HOOK = boom
        try: run(out)
        except Boom: pass
        fp._TEST_HOOK = None
        if manifest:
            mp = [os.path.join(out, ".resume", d, "manifest.json") for d in os.listdir(os.path.join(out, ".resume"))][0]
            m = json.load(open(mp)); manifest(m); json.dump(m, open(mp, "w"))
        if mutate: mutate()
        del msgs[:]
        return run(out)
    z = crash_then(os.path.join(t, "r1"))
    fast = [m for m in msgs if "parallel check" in m and "unchanged" in m]
    check("unchanged: 3 finished projects pass the parallel check (P1-P3)", len(fast), 3)
    check("the project with a link is checked by the walk and is still skipped as unchanged", any("P0" in m and "unchanged since an earlier run (check)" in m for m in msgs), True)
    check("a resume check line says how long the parallel pass took", any("resume check: 3 finished project(s)" in m for m in msgs), True)
    check("output equals an uninterrupted run's", rows_of(z) == base, True)
    # changes between crash and resume
    def edit(kind):
        f = os.path.join(src, "P1", "s1", "f01.sldprt")
        if kind == "size": open(f, "ab").write(b"more")
        if kind == "date": os.utime(f, (1_800_000_000, 1_800_000_000))
        if kind == "add": mk(os.path.join(src, "P2", "new.sldprt"), 3)
        if kind == "remove": os.remove(f)
        if kind == "rename": os.rename(f, os.path.join(src, "P1", "s1", "renamed.sldprt"))
    for kind in ("size", "date", "add", "remove", "rename"):
        z = crash_then(os.path.join(t, f"c_{kind}"), mutate=lambda k=kind: edit(k))
        changed = [m for m in msgs if "changed since it was saved" in m]
        n_fast = len([m for m in msgs if "parallel check" in m and "unchanged" in m])
        check(f"{kind}: the changed project is noticed, the others pass the fast check", (len(changed) >= 1, n_fast), (True, 2))
        edit_back = None
        # the result must reflect the disk as it is NOW
        fresh = rows_of(fp.profile(src, os.path.join(t, f"fresh_{kind}"), log=lambda *_: None, workers=2, chunk_files=5, system_info=False, tier=1)[0])
        check(f"{kind}: resumed output equals a fresh run on the changed disk", rows_of(z) == fresh, True)
        # undo for the next case
        f = os.path.join(src, "P1", "s1", "f01.sldprt")
        for pth in (os.path.join(src, "P1", "s1", "renamed.sldprt"), os.path.join(src, "P2", "new.sldprt")):
            if os.path.exists(pth): os.remove(pth)
        mk(f, 3, 1_700_000_000)                              # i + 1 + p = 3: the file as it was
    # old progress without a fingerprint: walked, still skipped, and a fingerprint is saved for next time
    def strip(m):
        for u in m["units"].values(): u.pop("ufp", None)
    z = crash_then(os.path.join(t, "old"), manifest=strip)
    check("no saved fingerprint: finished projects are walked (no parallel check) yet skipped as unchanged", (len([m for m in msgs if "parallel check" in m]), len([m for m in msgs if "unchanged since an earlier run (check)" in m])), (0, 4))
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
