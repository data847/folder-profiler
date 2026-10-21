"""Archives: CAD files inside zip / tar / nested zip / 7z become rows; fingerprints match loose copies; references inside archived
assemblies are read; off / list / full modes; budget; damaged archives.   Run: python tests/test_archives.py"""
import csv, hashlib, io, os, struct, subprocess, sys, tarfile, tempfile, zipfile, zlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import file_profiler as fp, archives as ar

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

def sw_file(refs):
    payload = b"".join(("D:\\old\\" + r).encode("utf-16le") + b"\x00\x00" for r in refs)
    c = zlib.compressobj(wbits=-15); data = c.compress(payload) + c.flush()
    return b"w0\xbc\"" + b"\x00" * 40 + b"\x14\x00\x06\x00\x08\x00" + struct.pack("<IIII", 0, len(data), len(payload), 0) + data

def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(body if isinstance(body, bytes) else body.encode())

def rows(z):
    import zipfile as zf
    r = zf.ZipFile(z)
    rd = lambda n: list(csv.DictReader(io.StringIO(r.read(n).decode("utf-8-sig"))))
    return rd("Files.csv"), {x["key"]: x["value"] for x in rd("Run_info.csv")}, (rd("Links.csv") if "Links.csv" in r.namelist() else []), rd("Dirs.csv")

PART = b"part data " * 40
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "Proj/loose/p1.sldprt", PART)                              # loose copy of a part that is ALSO inside the zip
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("deep/n.par", b"nested part " * 30)
    with zipfile.ZipFile(os.path.join(src, "Proj/backup.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("old/Main.sldasm", sw_file(["p1.sldprt", "p2.sldprt"]))
        z.writestr("old/p1.sldprt", PART); z.writestr("old/p2.sldprt", b"second part " * 33)
        z.writestr("old/readme.txt", "hi"); z.writestr("old/.DS_Store", "junk"); z.writestr("old/", "")
        z.writestr("nested.zip", inner.getvalue())
    with tarfile.open(os.path.join(src, "Proj/t.tar.gz"), "w:gz") as tf:
        b = b"tar drawing " * 20; ti = tarfile.TarInfo("d/x.slddrw"); ti.size = len(b); tf.addfile(ti, io.BytesIO(b))
    open(os.path.join(src, "Proj/bad.zip"), "wb").write(b"not really a zip")
    sp = os.path.join(t, "stage"); os.makedirs(sp); open(os.path.join(sp, "s.step"), "w").write("ISO-10303-21;\nEND-ISO-10303-21;\n")
    sevenz = True
    try:
        subprocess.run(["bsdtar", "--format", "7zip", "-cf", os.path.join(src, "Proj/c.7z"), "-C", sp, "."], check=True, capture_output=True)
    except Exception:
        sevenz = False

    def run(**kw):
        z, *_ = fp.profile(src, os.path.join(t, "out" + str(len(os.listdir(t)))), log=lambda *_: None, workers=1, hash_mode="all", **kw)
        return rows(z)

    f, info, links, dirs = run(tier=2, archive_mode="full")
    arch = [r for r in f if r["archive"]]
    names = sorted(r["rel_path"] for r in arch)
    check("entries from zip, nested zip and tar.gz appear as rows (junk and directory entries skipped)",
          [n for n in names if not n.endswith(".step")],
          ["backup.zip!/nested.zip", "backup.zip!/nested.zip!/deep/n.par", "backup.zip!/old/Main.sldasm",
           "backup.zip!/old/p1.sldprt", "backup.zip!/old/p2.sldprt", "backup.zip!/old/readme.txt",
           "t.tar.gz!/d/x.slddrw"])
    check("archive column says which kind", sorted({r["archive"] for r in arch}), ["7z", "tar", "zip"] if sevenz else ["tar", "zip"])
    loose = next(r for r in f if r["rel_path"] == "loose/p1.sldprt")
    inz = next(r for r in f if r["rel_path"].endswith("backup.zip!/old/p1.sldprt"))
    check("fingerprint of a file inside the zip equals its loose copy (sha256 + format)", (inz["sha256"] == loose["sha256"] != "", inz["header_signature"] == loose["header_signature"]), (True, True))
    check("sha256 is the real content hash", inz["sha256"], hashlib.sha256(PART).hexdigest())
    check("level 2: references read from an assembly INSIDE the zip", sorted(l["child"] for l in links if l["ext"] == "sldasm"), ["p1.sldprt", "p2.sldprt"])
    check("archive folders exist in the folder tree, chained under the folder holding the archive",
          any(d["name"] == "backup.zip" for d in dirs) and any(d["name"] == "nested.zip" for d in dirs), True)
    check("damaged archive is counted, not fatal", (info["archives_unreadable"], int(info["archives_opened"]) >= 3), ("1", True))
    check("Run_info reports the archive work", (info["archives_mode"], int(info["archive_entries"]) >= 8), ("full", True))
    if sevenz:
        s7 = next(r for r in f if r["rel_path"].endswith("c.7z!/s.step"))
        check("7z: listed (names/sizes) but not read", (s7["archive"], s7["sha256"], s7["header_signature"]), ("7z", "", "ARCHIVED"))

    f0, info0, *_ = run(tier=0, archive_mode="full")
    check("level 0: entries present but no names recorded", ("rel_path" in f0[0], sum(1 for r in f0 if r["archive"]) >= 8), (False, True))
    fl, infol, *_ = run(tier=1, archive_mode="list")
    check("list mode: entries listed, nothing decompressed (no sha256)", ({r["sha256"] for r in fl if r["archive"] and r["archive"] != "7z" and r["ext"] != "txt"}, any(r["archive"] for r in fl)), ({""}, True))
    foff, infooff, *_ = run(tier=1, archive_mode="off")
    check("off mode: archives are just files again", (sum(1 for r in foff if r["archive"]), infooff["archives_mode"]), (0, "off"))
    fb, infob, *_ = run(tier=2, archive_mode="full", archive_budget_gb=0.0000001)
    check("tiny budget: entries still listed, read ones stop and are reported", (int(infob["archive_budget_hit"]) > 0, sum(1 for r in fb if r["archive"]) == sum(1 for r in f if r["archive"])), (True, True))
# optional: a real .rar of your own, listed through bsdtar (no sample ships with the tests; export PROFILER_TEST_RAR=/path/to/file.rar)
rar = os.environ.get("PROFILER_TEST_RAR", "")
if rar and os.path.exists(rar) and ar.bsdtar():
    e = ar.list_archive(rar); check("real .rar listed through bsdtar", (len(e) >= 1, e[0]["readable"]), (True, False))
sys.exit(1 if any(bad) else 0)
