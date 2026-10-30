"""Chunks in flight: the reads of several 5,000-file chunks are queued while the next one is listed. The result must be identical to one chunk at a
time, a crash with chunks in flight must resume to the same result (only the unsaved window is redone), and the overlap must really save time."""
import builtins, csv, io, os, shutil, sys, tempfile, time, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rows(z, n, drop=()):
    return [{k: v for k, v in r.items() if k not in drop} for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig")))]
def build(root, projects=4, files=60):
    for p in range(projects):
        for f in range(files):
            d = os.path.join(root, f"P{p}", f"d{f % 6}"); os.makedirs(d, exist_ok=True)
            ext = ("dxf", "txt", "pdf", "jpg", "dat")[f % 5]
            open(os.path.join(d, f"f{f:03d}.{ext}"), "wb").write((b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n  0\nEOF\n" if ext == "dxf" else b"hello") * 3)
def run(root, out, k, **kw):
    fp.PIPELINE_CHUNKS = k
    return fp.profile(root, out, log=lambda *_: None, resume=kw.pop("resume", False), system_info=False, workers=8, chunk_files=7, **kw)[0]
with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "data"); build(root)
    z1 = run(root, os.path.join(t, "k0"), 0); z4 = run(root, os.path.join(t, "k4"), 4)
    for n, drop in (("Files.csv", ("dir_id",)), ("Projects.csv", ()), ("Extensions.csv", ()), ("Links.csv", ()), ("Unreadable.csv", ())):
        check(f"{n} identical with none and with 4 chunks in flight", rows(z1, n, drop) == rows(z4, n, drop), True)
    check("file_no is continuous per project (nothing lost or duplicated)", all([int(r["file_no"]) for r in rows(z4, "Files.csv") if r["project"] == f"P{p}"] == list(range(1, 61)) for p in range(4)), True)
    # ---- crash with chunks in flight, then resume
    class Boom(Exception): pass
    def boom(ui, ch):
        if (ui, ch) == (2, 3): raise Boom("simulated crash")
    fp._TEST_HOOK = boom; out = os.path.join(t, "crash")
    try:
        run(root, out, 4); crashed = False
    except Boom:
        crashed = True
    fp._TEST_HOOK = None
    check("the run crashed with chunks in flight and left saved progress", (crashed, os.path.isdir(os.path.join(out, ".resume"))), (True, True))
    zr = run(root, out, 4, resume=True)
    for n, drop in (("Files.csv", ("dir_id",)), ("Projects.csv", ()), ("Extensions.csv", ()), ("Links.csv", ())):
        check(f"resumed {n} identical to the uninterrupted run", rows(zr, n, drop) == rows(z1, n, drop), True)
    # ---- the overlap saves time when listing and reading both take time
    slow = {"on": False}
    real_open, real_scandir = builtins.open, os.scandir
    def sopen(f, *a, **k):
        if slow["on"] and isinstance(f, str) and f.startswith(root): time.sleep(0.05)
        return real_open(f, *a, **k)
    def sscan(p, *a, **k):
        if slow["on"] and isinstance(p, str) and p.startswith(root): time.sleep(0.03)
        return real_scandir(p, *a, **k)
    builtins.open = sopen; os.scandir = sscan; slow["on"] = True
    try:
        t0 = time.time(); run(root, os.path.join(t, "s0"), 0); a = time.time() - t0
        t0 = time.time(); run(root, os.path.join(t, "s4"), 4); b = time.time() - t0
    finally:
        builtins.open = real_open; os.scandir = real_scandir; slow["on"] = False
    print(f"     with 30 ms per folder listing and 50 ms per file open: waiting for each chunk {a:.2f}s, 4 in flight {b:.2f}s")
    check("with chunks in flight the same run is clearly faster", b < a * 0.85, True)
sys.exit(1 if any(bad) else 0)
