"""Header reads are skipped for NO_HEAD_EXTS: every one of those extensions must be classified by NAME in the compiler's table (so the
missing tag changes nothing), none may be a CAD / drawing / neutral / simulation / EDA class, and no file of them may be opened."""
import os, sys, tempfile, zipfile, csv, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
m = {}
for l in open(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping", "extensions.csv"), encoding="utf-8").read().splitlines()[1:]:
    p = l.split(",", 4); m[p[0]] = p[1]
SAFE = {"IMG", "DOC", "DATA", "PDF", "ARCHIVE", "OTHER"}
check("every skipped extension is mapped by name", sorted(e for e in fp.NO_HEAD_EXTS if e not in m), [])
check("and only to media / document / data classes (skp is the one native exception: SketchUp has no content tag)",
      sorted(e for e in fp.NO_HEAD_EXTS if e in m and m[e] not in SAFE), ["skp"])
check("skipped extensions never overlap the ones whose content is read (sheets, references)", fp.NO_HEAD_EXTS & (fp.SHEET_EXTS | fp.LINK_READ_EXTS), set())
# end to end: a file of a skipped type is not opened, a CAD one is
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "r", "proj"); os.makedirs(src); out = os.path.join(t, "o")
    for n, data in (("a.jpg", b"\xff\xd8\xff" + b"0" * 50), ("b.pdf", b"%PDF-1.4" + b"0" * 50), ("c.step", b"ISO-10303-21;\nDATA;\nENDSEC;\n"), ("d.txt", b"hello"), ("e.png", b""), ("f.skp", b"1" * 40)):
        open(os.path.join(src, n), "wb").write(data)
    opened = []
    real_open = open
    import builtins
    def spy(file, *a, **k):
        if isinstance(file, str) and file.startswith(src): opened.append(os.path.basename(file))
        return real_open(file, *a, **k)
    builtins.open = spy
    try:
        z = fp.profile(os.path.join(t, "r"), out, resume=False, system_info=False, log=lambda *a: None)[0]
    finally:
        builtins.open = real_open
    rows = {r["rel_path"].split("/")[-1]: r for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z).read("Files.csv").decode("utf-8-sig")))}
    check("skipped types are not opened; the others are", sorted(set(opened)), ["c.step", "d.txt"])
    check("their tags: NOTAG for non-empty, EMPTY for an empty file, real tags for the rest", {k: v["header_signature"] for k, v in rows.items()},
          {"a.jpg": "NOTAG", "b.pdf": "NOTAG", "c.step": "STEP:AP:other", "d.txt": "TEXT", "e.png": "EMPTY", "f.skp": "NOTAG"})
    info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z).read("Run_info.csv").decode("utf-8-sig")))}
    check("Run_info says how many header reads were skipped", info["header_reads_skipped"], "3")
    os.environ["PROFILER_READ_ALL_HEADERS"] = "1"; opened.clear(); builtins.open = spy
    try:
        z2 = fp.profile(os.path.join(t, "r"), out + "2", resume=False, system_info=False, log=lambda *a: None)[0]
    finally:
        builtins.open = real_open; del os.environ["PROFILER_READ_ALL_HEADERS"]
    r2 = {r["rel_path"].split("/")[-1]: r["header_signature"] for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z2).read("Files.csv").decode("utf-8-sig")))}
    check("PROFILER_READ_ALL_HEADERS=1 reads them all again", (r2["a.jpg"], r2["b.pdf"]), ("JPEG", "PDF"))
sys.exit(1 if any(bad) else 0)
