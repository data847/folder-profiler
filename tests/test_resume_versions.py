"""A new version of the tool carries on from the saved progress of an older one (nothing finished is redone), the output says which versions worked
on the run and which projects each profiled, progress in another STATE_FORMAT is not reused, and a changed setting still starts afresh."""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
def rows_of(z): return [{k: v for k, v in r.items() if k != "dir_id"} for r in rd(z, "Files.csv")]
def info_of(z): return {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
CALLS = [0]; _sig = fp.signature
def counting(*a, **k): CALLS[0] += 1; return _sig(*a, **k)
fp.signature = counting
fp.PIPELINE_CHUNKS = 0
class Boom(Exception): pass
def run(src, out, **kw):
    z, *_ = fp.profile(src, out, log=lambda *_: None, workers=2, chunk_files=5, system_info=False, tier=1, **kw); return z
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data")
    for p in range(4):
        for i in range(14):
            d = os.path.join(src, f"P{p}", f"sub{i % 3}"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"f{i:02d}.sldprt"), "wb").write(b"x" * (i + 1 + p))
    CALLS[0] = 0; base = run(src, os.path.join(t, "base")); total = CALLS[0]; base_rows = rows_of(base)
    check("an ordinary run says which version made it", (info_of(base)["versions_used"], info_of(base)["projects_by_version"]), (fp.VERSION, f"{fp.VERSION}: 4"))
    # ---- run under "1.7", crash after unit 2 chunk 1
    real = fp.VERSION; out = os.path.join(t, "o")
    fp.VERSION = "1.7"
    def boom(ui, ch):
        if (ui, ch) == (2, 1): raise Boom("crash")
    fp._TEST_HOOK = boom; CALLS[0] = 0
    try: run(src, out); crashed = False
    except Boom: crashed = True
    fp._TEST_HOOK = None; c1 = CALLS[0]
    # ---- the new version carries on
    fp.VERSION = "1.8"; CALLS[0] = 0
    z = run(src, out); c2 = CALLS[0]
    info = info_of(z)
    check("the 1.7 run crashed", crashed, True)
    check("1.8 continued: only the unread files were read (nothing redone)", (c1 + c2, c2 < total), (total, True))
    check("final rows equal an uninterrupted run's", rows_of(z) == base_rows, True)
    check("Run_info names both versions in order", info["versions_used"], "1.7 -> 1.8")
    check("projects_by_version splits the work", info["projects_by_version"], "1.7: 2; 1.7+1.8: 1; 1.8: 1")
    check("tool_version is the one that finished", info["tool_version"], "1.8")
    check("the run log says the progress was carried over", any("carrying on from it with v1.8" in r["message"] for r in rd(z, "Run_log.csv")) if "message" in rd(z, "Run_log.csv")[0] else any("carrying on" in str(r) for r in rd(z, "Run_log.csv")), True)
    # ---- a different STATE_FORMAT is not reused; a changed setting starts afresh
    out2 = os.path.join(t, "o2"); fp.VERSION = "1.7"
    fp._TEST_HOOK = boom
    try: run(src, out2)
    except Boom: pass
    fp._TEST_HOOK = None; fp.VERSION = "1.8"; fp.STATE_FORMAT = 2; CALLS[0] = 0
    run(src, out2); check("progress saved in another state format is not trusted: everything is read again", CALLS[0], total)
    fp.STATE_FORMAT = 1
    out3 = os.path.join(t, "o3"); fp.VERSION = "1.7"; fp._TEST_HOOK = boom
    try: run(src, out3)
    except Boom: pass
    fp._TEST_HOOK = None; fp.VERSION = "1.8"; CALLS[0] = 0
    fp.profile(src, out3, log=lambda *_: None, workers=2, chunk_files=5, system_info=False, tier=0)         # another detail level = another run
    check("a changed setting (detail level) starts afresh", CALLS[0], total)
    # ---- progress saved by the REAL 1.7 / 1.8 (identity had "v", no "fmt", no version history, a folder named by that identity)
    import json, shutil, zlib
    out4 = os.path.join(t, "o4"); fp.VERSION = "1.8"; fp._TEST_HOOK = boom
    try: run(src, out4)
    except Boom: pass
    fp._TEST_HOOK = None
    rdir = os.path.join(out4, ".resume"); (old_id,) = os.listdir(rdir); mp = os.path.join(rdir, old_id, "manifest.json")
    man = json.load(open(mp)); cfg = json.loads(man["cfg"]); cfg.pop("fmt"); cfg["v"] = "1.7"
    man["cfg"] = json.dumps(cfg, sort_keys=True); man.pop("versions")
    for u in man["units"].values(): u.pop("vs", None)
    json.dump(man, open(mp, "w")); legacy = "legacy0000000000"; os.rename(os.path.join(rdir, old_id), os.path.join(rdir, legacy))
    CALLS[0] = 0; z = run(src, out4); info = info_of(z)
    check("progress saved by the real 1.7 (old identity) is picked up and carried on", (CALLS[0] < total, rows_of(z) == base_rows), (True, True))
    check("its versions are recovered from the old identity", info["versions_used"], "1.7 -> 1.8")
    check("projects it had started are credited to 1.7", info["projects_by_version"].startswith("1.7"), True)
    fp.VERSION = real
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
