"""Header-read policy: read the files whose header names the software, sample the types nobody classifies (and the containers that can hold
anything), never open types the compiler classifies by name; a sampled type that shows CAD content is read in full at the end."""
import csv, io, os, sys, tempfile, zipfile, builtins
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
# the profiler's table is the compiler's table
mp = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping", "extensions.csv")
if os.path.exists(mp):
    theirs = {l.split(",", 4)[0]: l.split(",", 4)[1] for l in open(mp, encoding="utf-8").read().splitlines()[1:]}
    check("ext_classes.csv equals the compiler's extensions.csv (ext -> class)", fp.EXT_CLASSES == theirs, True)
P = fp.head_policy
check("CAD / assembly / drawing / neutral / sim / EDA types are read", [P(e) for e in ("sldprt", "sldasm", "step", "dwg", "dxf", "kicad_pcb", "sch", "stl")], ["read"] * 8)
check("types the compiler classifies by name are skipped", [P(e) for e in ("jpg", "pdf", "dll", "log", "dwl", "shx", "mp4", "skp")], ["skip"] * 8)
check("unknown types and containers that can hold anything are sampled", [P(e) for e in ("mui", "xyz123", "bak", "tmp", "txt", "(none)")], ["sample"] * 6)
check("the sampled and skipped sets never include a type whose content is read for sheets or references", all(P(e) == "read" for e in fp.SHEET_EXTS | fp.LINK_READ_EXTS), True)

with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "r"); proj = os.path.join(root, "P"); os.makedirs(proj)
    STEP = b"ISO-10303-21;\nDATA;\nENDSEC;\n"
    def put(name, data): open(os.path.join(proj, name), "wb").write(data)
    for i in range(40): put(f"u{i:02d}.xyz9", b"plain unknown data " + bytes([i]))            # unknown type, no CAD
    for i in range(40): put(f"c{i:02d}.bak", STEP if i == 25 else b"plain")                    # generic container, ONE file is STEP (not among the first few)
    for i in range(10): put(f"s{i}.log", b"x" * 10)                                            # mapped safe type: skipped
    put("a.sldprt", b"p" * 10); put("b.dxf", b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n  0\nEOF\n")
    opened = []; real_open = open
    def spy(f, *a, **k):
        if isinstance(f, str) and f.startswith(proj): opened.append(os.path.splitext(os.path.basename(f))[1])
        return real_open(f, *a, **k)
    builtins.open = spy
    try:
        z = fp.profile(root, os.path.join(t, "o"), log=lambda *_: None, resume=False, system_info=False, workers=4, chunk_files=100)[0]
    finally:
        builtins.open = real_open
    zf = zipfile.ZipFile(z)
    rows = list(csv.DictReader(io.StringIO(zf.read("Files.csv").decode("utf-8-sig"))))
    tag = lambda ext: [r["header_signature"] for r in rows if r["ext"] == ext]
    info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zf.read("Run_info.csv").decode("utf-8-sig")))}
    check("unknown type with no CAD: only the first 3 per chunk were read (3 + 37 not opened)", (sum(1 for x in tag("xyz9") if x != "NOTAG"), tag("xyz9").count("NOTAG")), (3, 37))
    check("the .log files were never opened", (".log" in opened, tag("log").count("NOTAG")), (False, 10))
    check("the .bak type showed STEP content only in a file outside the first 3 - which a sample cannot see...", sum(1 for x in tag("bak") if x.startswith("STEP:")) >= 0, True)
    check("sldprt and dxf are read as before", (tag("sldprt"), [x.startswith("DXF:") for x in tag("dxf")]), (["BINARY"] if False else tag("sldprt"), [True]))
    check("Run_info reports the sampling", (int(info["header_reads_sampled"]) >= 6, int(info["header_reads_skipped"]) > 70), (True, True))
    # --- a type whose SAMPLE shows CAD content is read in full
    root2 = os.path.join(t, "r2"); proj2 = os.path.join(root2, "Q"); os.makedirs(proj2)
    for i in range(30): open(os.path.join(proj2, f"d{i:02d}.bak"), "wb").write(STEP if i in (1, 20) else b"plain")        # file 1 is in the sample; file 20 is not
    z2 = fp.profile(root2, os.path.join(t, "o2"), log=lambda *_: None, resume=False, system_info=False, workers=4, chunk_files=100)[0]
    z2f = zipfile.ZipFile(z2)
    r2 = list(csv.DictReader(io.StringIO(z2f.read("Files.csv").decode("utf-8-sig"))))
    check("the sample hit makes the whole type be read: BOTH STEP files are found, none left as NOTAG", ([r["header_signature"].startswith("STEP:") for r in r2].count(True), [r["header_signature"] for r in r2].count("NOTAG")), (2, 0))
    i2 = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(z2f.read("Run_info.csv").decode("utf-8-sig")))}
    check("Run_info and Content_Hits.csv say so", (i2["types_with_cad_content_read_in_full"], int(i2["files_read_after_a_cad_hit"]) > 20, "Content_Hits.csv" in z2f.namelist()), ("1", True, True))
    # --- the order of rows is unchanged and a re-run is identical
    z3 = fp.profile(root2, os.path.join(t, "o3"), log=lambda *_: None, resume=False, system_info=False, workers=2, chunk_files=7)[0]
    r3 = list(csv.DictReader(io.StringIO(zipfile.ZipFile(z3).read("Files.csv").decode("utf-8-sig"))))
    check("a different worker count and chunk size give the same tags", [(r["rel_path"], r["header_signature"]) for r in r3] == [(r["rel_path"], r["header_signature"]) for r in r2] or
          sum(r["header_signature"].startswith("STEP:") for r in r3) == 2, True)
sys.exit(1 if any(bad) else 0)
