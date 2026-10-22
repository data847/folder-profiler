"""Resume: a run that is interrupted (crash, Ctrl+C, mid-chunk failure, mid-archive) continues from the saved point, never re-reads a
finished file, and ends with exactly the output an uninterrupted run gives.   Run: python tests/test_resume.py"""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import file_profiler as fp

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

def build(src):
    for p in range(3):
        for i in range(14):
            d = os.path.join(src, f"P{p}", f"sub{i % 3}"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"f{i:02d}.sldprt"), "wb").write(b"x" * (i + 1 + p))
    for k in range(3):                                              # three zips in P1 -> archives are resumable units too
        with zipfile.ZipFile(os.path.join(src, "P1", f"bk{k}.zip"), "w") as z:
            z.writestr(f"old/a{k}.sldprt", b"a" * (k + 2)); z.writestr(f"old/b{k}.par", b"b" * (k + 3))

CALLS = [0]
_orig_sig = fp.signature
def counting_sig(*a, **k):
    CALLS[0] += 1
    return _orig_sig(*a, **k)
fp.signature = counting_sig

def rows_of(zip_path):
    r = list(csv.DictReader(io.StringIO(zipfile.ZipFile(zip_path).read("Files.csv").decode("utf-8-sig"))))
    return [{k: v for k, v in x.items() if k != "dir_id"} for x in r]          # dir_id differs between runs (random salt)

def info_of(zip_path):
    return {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zipfile.ZipFile(zip_path).read("Run_info.csv").decode("utf-8-sig")))}

def run(src, out, **kw):
    z, *_ = fp.profile(src, out, log=lambda *_: None, workers=2, chunk_files=5, system_info=False, tier=1, metrics_interval=1.0, **kw)
    return z

class Boom(Exception): pass

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data"); build(src)
    # ---- baseline: one uninterrupted run
    CALLS[0] = 0
    base = rows_of(run(src, os.path.join(t, "base"))); total_calls = CALLS[0]
    check("baseline read every loose file once", total_calls, 3 * 14 + 3 * 0 + 3)      # 42 files in P0..P2 + 3 zip files (zips are files too)
    # ---- 1. crash right after a chunk was saved
    out = os.path.join(t, "o1")
    def boom_after_chunk(ui, ch):
        if (ui, ch) == (1, 1): raise Boom("simulated crash")
    fp._TEST_HOOK = boom_after_chunk; CALLS[0] = 0
    try:
        run(src, out); crashed = False
    except Boom:
        crashed = True
    fp._TEST_HOOK = None; c1 = CALLS[0]
    check("the run stopped and left saved progress behind", (crashed, os.path.isdir(os.path.join(out, ".resume")), [f for f in os.listdir(out) if f.startswith("profile_")]), (True, True, []))
    CALLS[0] = 0
    z = run(src, out); c2 = CALLS[0]
    check("resumed run did not re-read anything that was already saved", c1 + c2, total_calls)
    check("resumed output is identical to the uninterrupted output", rows_of(z) == base, True)
    log = zipfile.ZipFile(z).read("Run_log.csv").decode("utf-8")
    check("log tells the story (resumed, finished projects skipped)", ("RESUMED" in log, "already done in an earlier run" in log, info_of(z)["restarts_resumed"]), (True, True, "1"))
    check("saved progress is removed after a successful run", os.path.exists(os.path.join(out, ".resume")), False)
    # ---- 2. crash in the middle of a chunk (nothing of that chunk was saved)
    out = os.path.join(t, "o2"); seen = [0]
    def flaky_sig(*a, **k):
        seen[0] += 1
        if seen[0] == 23: raise Boom("simulated crash mid-chunk")
        return _orig_sig(*a, **k)
    fp.signature = flaky_sig
    try:
        run(src, out); crashed = False
    except Boom:
        crashed = True
    fp.signature = counting_sig
    z = run(src, out)
    check("mid-chunk crash: resume redoes only the unsaved chunk and the result is still identical", (crashed, rows_of(z) == base), (True, True))
    # ---- 3. crash between archives of one project
    out = os.path.join(t, "o3")
    def boom_after_archive(ui, ch):
        if ch == "a0": raise Boom("simulated crash after the first archive")
    fp._TEST_HOOK = boom_after_archive
    try:
        run(src, out); crashed = False
    except Boom:
        crashed = True
    fp._TEST_HOOK = None
    z = run(src, out); r = rows_of(z)
    check("archive crash: resume continues at the next archive, no entry lost or doubled", (crashed, r == base, sum(1 for x in r if x["archive"])), (True, True, 6))
    # ---- 4. Ctrl+C is saved too
    out = os.path.join(t, "o4")
    def interrupt(ui, ch):
        if (ui, ch) == (0, 0): raise KeyboardInterrupt
    fp._TEST_HOOK = interrupt
    try:
        run(src, out); stopped = False
    except KeyboardInterrupt:
        stopped = True
    fp._TEST_HOOK = None
    z = run(src, out)
    check("Ctrl+C: progress kept, resume gives identical output", (stopped, rows_of(z) == base), (True, True))
    # ---- 5. --fresh ignores saved progress; different settings never reuse another run's progress
    out = os.path.join(t, "o5")
    fp._TEST_HOOK = boom_after_chunk
    try: run(src, out)
    except Boom: pass
    fp._TEST_HOOK = None
    z = run(src, out, resume=False)
    log = zipfile.ZipFile(z).read("Run_log.csv").decode("utf-8")
    check("fresh start (resume off) does not resume", ("RESUMED" in log, rows_of(z) == base), (False, True))
    out = os.path.join(t, "o6")
    fp._TEST_HOOK = boom_after_chunk
    try: run(src, out)
    except Boom: pass
    fp._TEST_HOOK = None
    z0, *_ = fp.profile(src, out, log=lambda *_: None, workers=2, chunk_files=5, system_info=False, tier=0)
    check("different settings (level 0) start their own run instead of reusing level-1 progress",
          ("RESUMED" in zipfile.ZipFile(z0).read("Run_log.csv").decode("utf-8"), "rel_path" in rows_of(z0)[0]), (False, False))
    # ---- 6. the folder changes while the run is down: the result is not silently trusted - it is flagged
    out = os.path.join(t, "o7")
    fp._TEST_HOOK = boom_after_chunk
    try: run(src, out)
    except Boom: pass
    fp._TEST_HOOK = None
    extra = os.path.join(src, "P1", "sub0", "zz_new.sldprt"); open(extra, "wb").write(b"new")
    z = run(src, out); os.remove(extra)
    log = zipfile.ZipFile(z).read("Run_log.csv").decode("utf-8")
    check("a folder that changed between restarts is flagged (WARN in the log, counted in Run_info)", ("changed while profiling" in log, info_of(z)["projects_changed_during_run"]), (True, "1"))
fp.signature = _orig_sig
sys.exit(1 if any(bad) else 0)
