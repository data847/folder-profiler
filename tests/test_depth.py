"""No depth limit: very deep folders, projects at any depth, and directory links that must not loop.   Run: python tests/test_depth.py"""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import file_profiler as fp

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)
rd = lambda z, n: list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))

with tempfile.TemporaryDirectory() as t:
    deep = os.path.join(t, "data", "Proj")
    levels = 150
    for i in range(levels):
        deep = os.path.join(deep, f"d{i:03d}")
    os.makedirs(deep)
    open(os.path.join(deep, "bottom.sldprt"), "wb").write(b"b")
    mid = os.path.join(t, "data", "Proj", *[f"d{i:03d}" for i in range(75)])
    open(os.path.join(mid, "middle.sldprt"), "wb").write(b"mm")
    z, *_ = fp.profile(os.path.join(t, "data"), os.path.join(t, "o"), log=lambda *_: None, workers=1, tier=1)
    f = {r["rel_path"].rsplit("/", 1)[-1]: r for r in rd(z, "Files.csv")}
    check(f"a file {levels} folders deep is found, with its depth and full path", (f["bottom.sldprt"]["depth"], f["bottom.sldprt"]["rel_path"].count("/")), (str(levels), levels))
    check("a file in the middle is found too", f["middle.sldprt"]["depth"], "75")
    dirs = rd(z, "Dirs.csv"); ids = {d["dir_id"] for d in dirs}
    check(f"the folder tree has all {levels + 1} folders, each with a parent that exists", (len(dirs), all(d["parent_dir_id"] in ids for d in dirs if d["parent_dir_id"])), (levels + 1, True))
    # projects at ANY depth (the old cap was 6)
    base = os.path.join(t, "wide")
    for name in ("a", "b"):
        p = os.path.join(base, *list("lvl12345"), name); os.makedirs(p)       # 8 levels of containers, then two real projects
        open(os.path.join(p, "x.sldprt"), "wb").write(b"x")
    z, *_ = fp.profile(base, os.path.join(t, "o2"), log=lambda *_: None, workers=1, tier=1, project_depth=9)
    check("project depth 9 (beyond the old limit of 6) gives the two deep projects", sorted({r["project"] for r in rd(z, "Files.csv")}),
          ["l/v/l/1/2/3/4/5/a".replace("l/v/l/1/2/3/4/5", "/".join("lvl12345")) , "/".join("lvl12345") + "/b"])
    # directory links must not be followed (loops)
    loop = os.path.join(t, "loopy", "P"); os.makedirs(os.path.join(loop, "sub"))
    open(os.path.join(loop, "sub", "f.sldprt"), "wb").write(b"f")
    try:
        os.symlink(loop, os.path.join(loop, "sub", "back_to_P"))
        z, *_ = fp.profile(os.path.join(t, "loopy"), os.path.join(t, "o3"), log=lambda *_: None, workers=1, tier=1)
        info = {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
        check("a folder link that points back up is skipped, counted, and does not loop", (info["files"], info["linked_folders_skipped"]), ("1", "1"))
    except (OSError, NotImplementedError):
        print("skip - symlinks not available here")
sys.exit(1 if any(bad) else 0)
