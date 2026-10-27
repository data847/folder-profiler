"""Categorisation: extensions from real-world data, classification by file content for unknown extensions, Creo version suffixes,
an honest report of what is still unmapped, and an Assembly Breakdown that is never empty.   Run: python tests/test_categorisation.py"""
import csv, io, os, subprocess, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp
try:
    import compile_profiles as cp
except ImportError:
    print("SKIP - compiler not found next to this package"); sys.exit(0)

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)
def put(base, rel, body=b"x"):
    p = os.path.join(base, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(body if isinstance(body, bytes) else body.encode())
MAPDIR = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping")
SCRIPT = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py")
em, sf = cp.load_mapping(MAPDIR)

# --- the profiler: Creo versions and plain names
check("Creo 'part.prt.12' is a part, not a '.12' file", [fp.ext_of(n) for n in ("bracket.prt.12", "top.asm.3", "sheet.drw.2", "x.12", "a.b.c", ".gitignore", "noext")],
      ["prt", "asm", "drw", "12", "c", "(none)", "(none)"])
# --- the table covers what real data showed
want = {"pwd": "ASSEMBLY", "jt": "NEUTRAL3D", "sat": "NEUTRAL3D", "3dxml": "NEUTRAL3D", "cgr": "NEUTRAL3D", "dgn": "DRAWING", "cfg": "DATA", "bak": "DATA",
        "dwl": "DATA", "dwt": "DATA", "ctb": "DATA", "ipj": "DATA", "dll": "DATA", "exe": "DATA", "mp4": "OTHER", "par": "PART", "asm": "ASSEMBLY", "dft": "DRAWING"}
check("extension table classes for formats seen in real CAD folders", {e: em[e]["class"] for e in want if e in em}, want)
check("fallback by content: STEP/DXF/PDF/ZIP starts classify unknown extensions", [cp.class_from_signature(s) for s in ("STEP:AP214:SolidWorks", "DXF:AC1027:3D", "DWG:AC1032", "PDF", "ZIP", "TEXT", "BINARY")],
      ["NEUTRAL3D", "DRAWING", "DRAWING", "DOC", "ARCHIVE", None, None])
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data"); C = lambda r, b=b"x": put(src, "C/" + r, b)
    for r in ("a.jt", "b.sat", "c.3dxml"): C(r, r.encode())
    C("d.dgn", b"d"); C("e.pwd", b"weldment"); C("bracket.prt.12", b"part!")
    for r in ("f.cfg", "g.bak", "h.dwl", "k.mp4"): C(r, r.encode())
    C("weird.xyz", "ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\n")           # unknown extension, STEP content
    C("drawing.zzz", "0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nLINE\n")
    C("slides.abc", b"%PDF-1.4 ...")                                                               # PDF content, unknown extension -> a document
    C("notes.qq", b"just some text")                                                               # genuinely unknown
    put(src, "OnlyParts/p1.sldprt", b"1"); put(src, "OnlyParts/p2.sldprt", b"22")                  # project without any assembly file
    put(src, "OnlyDrawings/d.slddrw", b"d")
    put(src, "Asm/Main.sldasm", b"a"); put(src, "Asm/q.sldprt", b"qq")
    put(src, "NoCad/readme.txt", b"hello")                                                         # nothing to break down
    z, *_ = fp.profile(src, os.path.join(t, "o"), log=lambda *_: None, workers=1, tier=1)
    out = os.path.join(t, "c"); subprocess.run([sys.executable, SCRIPT, z, "--out", out, "--min-project-files", "1"], capture_output=True, text=True)
    rd = lambda n: list(csv.DictReader(open(os.path.join(out, n), encoding="utf-8-sig")))
    pc = {r["Project Name"]: r for r in rd("Project_Compiled.csv")}; c = pc["C"]
    check("C: 4 neutral 3D (jt, sat, 3dxml + a STEP-content file with an unknown extension)", c["Neutral 3D Files"], "4")
    check("C: 2 native drawings (dgn + a DXF-content file)", c["Native 2D Drawings"], "2")
    check("C: the weldment is an assembly, the Creo version is a part", (c["Assembly Files"], c["Part Files"]), ("1", "1"))
    rev = [r for r in rd("Review.csv") if r["topic"] == "categorisation"][0]["detail"]
    check("Review reports how many files were classified by content and how many stay unmapped", ("by file content" in rev and "3 by file content" in rev, "1 unmapped" in rev), (True, True))
    um = rd("Unmapped_Extensions.csv"); check("Unmapped_Extensions lists only the genuinely unknown one", [r["ext"] for r in um], ["qq"])
    ab = rd("Assembly_Breakdown_ESTIMATE.csv")
    byp = {}
    for r in ab: byp.setdefault(r["Project Name"], []).append(r)
    cad = [p for p, r in pc.items() if int(r["Part Files"]) + int(r["Assembly Files"]) + int(r["Native 2D Drawings"]) + int(r["Neutral 3D Files"]) > 0]
    check("EVERY project with CAD files has at least one Assembly Breakdown row", sorted(p for p in cad if p not in byp), [])
    check("a project with no assembly file gets one clearly named summary row holding its totals",
          [(r["Assembly File Name"], r["Part Files"]) for r in byp["OnlyParts"]], [("(no assembly file in this project)", "2")])
    check("same for a drawings-only project", [(r["Assembly File Name"], r["Native 2D Drawings"]) for r in byp["OnlyDrawings"]], [("(no assembly file in this project)", "1")])
    check("a project with a real assembly keeps its real row", [r["Assembly File Name"] for r in byp["Asm"]], ["Main.sldasm"])
    sums = lambda col: sum(int(r[col]) for r in ab)
    tot = lambda col: sum(int(r[col]) for r in pc.values())
    check("breakdown rows add up to the Project Compiled totals (parts, 2D, PDF, neutral)",
          [sums("Part Files"), sums("Native 2D Drawings"), sums("PDF Drawings"), sums("Neutral 3D Files")],
          [tot("Part Files"), tot("Native 2D Drawings"), tot("PDF Drawings"), tot("Neutral 3D Files")])
    check("a project with no CAD files at all adds no row (nothing to break down)", "NoCad" in byp, False)
# --- an OLD profile (Creo versions recorded as '.7', names available): the compiler recovers the real type
with tempfile.TemporaryDirectory() as t:
    zp = os.path.join(t, "old.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Files.csv", "project,file_no,ext,size_bytes,modified_utc,depth,header_signature,dir_id,asm_name,rel_path,archive\r\n"
                                "P,1,7,100,2026-01-01T00:00:00Z,0,BINARY,aaaa,,x.prt.7,\r\nP,2,3,100,2026-01-01T00:00:00Z,0,BINARY,aaaa,,y.asm.3,\r\n")
        z.writestr("Run_info.csv", "key,value\r\nfolder_name,t\r\n")
    c2 = cp.compile_runs(cp.load_profiles([zp]), em, sf, False, "auto", False)[0]
    check("old profile: 'x.prt.7' counts as a part, 'y.asm.3' as an assembly", (c2[0][4], c2[0][5]), (1, 1))
sys.exit(1 if any(bad) else 0)
