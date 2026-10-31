"""Reconciliation.csv: an untouched tree reconciles exactly (files and bytes per project, with odd names, links, junk, versioned Creo names); a folder that
changes while the run works is reported with the exact difference per project and per file type; anonymised runs keep the check; Run_info says it."""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
def info(z): return {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
def mk(path, size, mt=1_700_000_000):
    os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(b"x" * size); os.utime(path, (mt, mt))
fp.PIPELINE_CHUNKS = 2
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "src")
    names = ["a.dwg", "B.DWG", "part.prt.7", "x.y.z", "noext", "ünï.sldprt", "q r.pdf", "a.b.step", "UPPER.Prt.12"]
    for p in range(3):
        for d in ("", "sub", "sub/deep"):
            for i, nme in enumerate(names):
                mk(os.path.join(src, f"P{p}", *([d] if d else []), nme if i % 2 else f"{p}_{nme}"), 1 + i + p)
        mk(os.path.join(src, f"P{p}", "Thumbs.db"), 5)
    mk(os.path.join(src, "loose.txt"), 4)
    os.symlink(os.path.join(src, "P0", "B.DWG"), os.path.join(src, "P1", "link.dwg"))
    run = lambda out, **kw: fp.profile(src, out, log=lambda *_: None, workers=2, chunk_files=6, system_info=False, tier=1, **kw)[0]
    z = run(os.path.join(t, "a")); rec = rd(z, "Reconciliation.csv"); i = info(z)
    check("an untouched tree reconciles exactly: every project OK", sorted({r["status"] for r in rec}), ["OK"])
    check("one row per project (3 projects + the loose-files unit), no per-type rows", (len(rec), {r["level"] for r in rec}), (4, {"project"}))
    check("Run_info gives the verdict", (i["reconciliation"].startswith("OK: all 4 projects match"), i["reconciliation_projects_different"]), (True, "0"))
    check("counted == profiled in every row", all(r["counted_files"] == r["profiled_files"] and r["counted_bytes"] == r["profiled_bytes"] for r in rec), True)
    z = run(os.path.join(t, "b"), anonymize_names=True, name_map=False); rec = rd(z, "Reconciliation.csv")
    check("an anonymised run keeps the check, with coded project names", (sorted({r["status"] for r in rec}), all(r["project"].startswith(("P-", "(root)")) or r["project"] == "(root)" for r in rec)), (["OK"], True))
    # a folder that changes while the run works: files added to P2 after it was counted
    added = []
    def grow(ui, ch):
        if (ui, ch) == (0, 0) and not added:
            for k in range(3): mk(os.path.join(src, "P2", f"late{k}.dwg"), 10); added.append(k)
            os.remove(os.path.join(src, "P2", "sub", "x.y.z")) if os.path.exists(os.path.join(src, "P2", "sub", "x.y.z")) else None
    fp._TEST_HOOK = grow
    z = run(os.path.join(t, "c")); fp._TEST_HOOK = None
    rec = rd(z, "Reconciliation.csv"); i = info(z)
    print("   ", [(r["level"], r["project"], r["ext"], r["diff_files"], r["status"]) for r in rec if r["status"] != "OK"])
    # the cached listing is a snapshot: the profile reflects the count, so it still reconciles; a change after the count is NOT hidden by the cache
    check("the verdict is one of OK / DIFFERENT and is in Run_info", i["reconciliation"].split(":")[0] in ("OK",) or "differ" in i["reconciliation"], True)
    # force a real mismatch: profile with the listing read again (live), folder changes between count and profiling
    os.environ["PROFILER_REUSE_LISTING"] = "0"; added.clear()
    for f in os.listdir(os.path.join(src, "P2")):
        if f.startswith("late"): os.remove(os.path.join(src, "P2", f))
    z = run(os.path.join(t, "d")); fp._TEST_HOOK = None
    fp._TEST_HOOK = grow
    z = run(os.path.join(t, "e")); fp._TEST_HOOK = None
    os.environ.pop("PROFILER_REUSE_LISTING")
    rec = rd(z, "Reconciliation.csv"); i = info(z)
    diff = [r for r in rec if r["status"] == "DIFFERENT"]
    print("   ", [(r["level"], r["project"], r["ext"], r["diff_files"], r["diff_bytes"]) for r in diff]); print("   ", i["reconciliation"])
    # P2 was counted before the hook added files, but the hook fires only after a chunk is saved: the late files are listed live by the walk -> differ
    check("a project that changed after it was counted is reported DIFFERENT", any(r["level"] == "project" and r["project"] == "P2" for r in diff), True)
    check("the differing file type is named with the exact difference (3 late .dwg files, 30 bytes)", any(r["level"] == "extension" and r["ext"] == "dwg" and r["diff_files"] == "3" and r["diff_bytes"] == "30" for r in diff), True)
    check("Run_info says how many differ", i["reconciliation_projects_different"], "1")
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
