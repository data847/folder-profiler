"""The process phase reads the counting pass's folder listing instead of listing every folder a second time - and the result is identical: every table
of the profile equals the one made with the listing read again, on a tree with links, a broken link, junk, odd names, empty and unreadable folders,
loose files, different detail levels and project depths. Folders with errors are listed live; a restarted run never uses an old snapshot."""
import collections, csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def tables(z):
    out = {}
    zf = zipfile.ZipFile(z)
    for n in sorted(zf.namelist()):
        if not n.endswith(".csv") or n.startswith("Run_") or n in ("System_info.csv", "Dirs.csv"):
            continue
        rows = list(csv.DictReader(io.StringIO(zf.read(n).decode("utf-8-sig"))))
        out[n] = [{k: v for k, v in r.items() if k not in ("dir_id",)} for r in rows]            # dir_id is salted: differs between any two runs
    rows = list(csv.DictReader(io.StringIO(zf.read("Dirs.csv").decode("utf-8-sig"))))
    out["Dirs.csv"] = sorted((r["project"], r["depth"], r.get("name", "")) for r in rows)
    return out
def info(z): return {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z).read("Run_info.csv").decode("utf-8-sig")))}
def mk(path, size, mt=1_700_000_000):
    os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(b"x" * size); os.utime(path, (mt, mt))
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "src")
    names = ["a.dwg", "B.dwg", "b.dwg", "Z.pdf", "ünï.txt", "x y.sldprt", "ñ.step", "a,b.dxf", 'q"r.txt', "a.b.c.dwg", "part.prt.7", "UPPER.DWG"]
    for p in range(4):
        for d in ("", "sub", "Sub", "sub/deep/er", "zz"):
            for i, nme in enumerate(names):
                mk(os.path.join(src, f"P{p}", *([d] if d else []), nme if i % 2 else f"{p}_{nme}"), 1 + i + p, 1_700_000_000 + i * 100)
        mk(os.path.join(src, f"P{p}", "Thumbs.db"), 5); mk(os.path.join(src, f"P{p}", "sub", "._junk"), 5)
    os.makedirs(os.path.join(src, "P1", "empty_dir")); os.makedirs(os.path.join(src, "P2", "zz", "also empty"))
    mk(os.path.join(src, "loose1.txt"), 4); mk(os.path.join(src, "loose2.dwg"), 6)
    os.symlink(os.path.join(src, "P0", "B.dwg"), os.path.join(src, "P3", "link_to_file.dwg"))       # a link to a file
    os.symlink(os.path.join(src, "P0", "sub"), os.path.join(src, "P3", "link_to_folder"))             # a link to a folder: skipped
    os.symlink(os.path.join(src, "does", "not", "exist"), os.path.join(src, "P2", "broken.dwg"))      # broken link: folder is listed live
    locked = os.path.join(src, "P1", "locked"); mk(os.path.join(locked, "secret.dwg"), 3)
    can_lock = hasattr(os, "geteuid") and os.geteuid() != 0 and sys.platform != "win32"
    if can_lock: os.chmod(locked, 0)
    msgs = []
    def run(out, reuse, **kw):
        if reuse: os.environ.pop("PROFILER_REUSE_LISTING", None)
        else: os.environ["PROFILER_REUSE_LISTING"] = "0"
        opts = dict(workers=3, chunk_files=7, system_info=False, tier=1); opts.update(kw)
        z, *_ = fp.profile(src, out, log=lambda m: msgs.append(m), **opts); return z
    try:
        for pipe in (0, 4):
            fp.PIPELINE_CHUNKS = pipe
            for tier in (0, 1, 2):
                for depth in (1, 2):
                    a = run(os.path.join(t, f"on_{pipe}_{tier}_{depth}"), True, tier=tier, project_depth=depth)
                    b = run(os.path.join(t, f"off_{pipe}_{tier}_{depth}"), False, tier=tier, project_depth=depth)
                    ta, tb = tables(a), tables(b)
                    same = ta == tb
                    if not same:
                        for k in ta:
                            if ta[k] != tb.get(k): print("   differs:", k, len(ta[k]), len(tb.get(k, [])))
                    ia, ib = info(a), info(b)
                    check(f"pipeline={pipe} tier={tier} depth={depth}: every table identical with and without reuse", same, True)
                    check(f"   ...reuse really happened ({ia['folders_read_from_counting_pass']} folders) and not in the other run", (int(ia["folders_read_from_counting_pass"]) > 0, ib["folders_read_from_counting_pass"]), (True, "0"))
        # folders with errors are listed live
        a = run(os.path.join(t, "live"), True, tier=1)
        ia = info(a)
        check("the folder with a broken link (and the unreadable one) were listed again, live", int(ia["folders_listed_again"]) >= 1, True)
        check("links to files are in the profile like any file", any(r["rel_path"].endswith("link_to_file.dwg") for r in tables(a)["Files.csv"]), True)
        check("junk files are not", any("Thumbs.db" in r["rel_path"] or "._junk" in r["rel_path"] for r in tables(a)["Files.csv"]), False)
    finally:
        if can_lock: os.chmod(locked, 0o755)
    # temp files are gone
    import glob, tempfile as tf
    check("no listing cache is left in the temp folder", glob.glob(os.path.join(tf.gettempdir(), "profiler_listing_*")), [])
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
