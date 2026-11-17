"""CAD scan (step 2) on a synthetic supplier folder: every Field Logic rule has a file that must land in the right bucket.
Run: python tests/test_cad_scan.py"""
import io
import os
import struct
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cad_scan as cs  # noqa: E402
import file_profiler as fp  # noqa: E402

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def cfb(streams):
    """Minimal OLE compound file: streams = {name: bytes}; every stream is padded to 4096 so no mini-stream is needed."""
    names = list(streams)
    data = {n: streams[n].ljust(4096, b"\x00") if len(streams[n]) < 4096 else streams[n] for n in names}
    sizes = {n: len(streams[n]) if len(streams[n]) >= 4096 else 4096 for n in names}
    secs_per = {n: (len(data[n]) + 511) // 512 for n in names}
    ndir = 1 + len(names)
    dir_secs = (ndir * 128 + 511) // 512
    nsec = 1 + dir_secs + sum(secs_per.values())
    fat = [0xFFFFFFFD] + [0xFFFFFFFE] * 0
    FREE, END = 0xFFFFFFFF, 0xFFFFFFFE
    fat = [FREE] * 128
    fat[0] = 0xFFFFFFFD
    for i in range(dir_secs):
        fat[1 + i] = 1 + i + 1 if i < dir_secs - 1 else END
    nxt, starts = 1 + dir_secs, {}
    for n in names:
        starts[n] = nxt
        for i in range(secs_per[n]):
            fat[nxt + i] = nxt + i + 1 if i < secs_per[n] - 1 else END
        nxt += secs_per[n]
    assert nsec <= 128

    def entry(name, typ, start=0, size=0, left=FREE, right=FREE, child=FREE):
        nb = name.encode("utf-16le") + b"\x00\x00"
        e = bytearray(128)
        e[:len(nb)] = nb
        struct.pack_into("<H", e, 0x40, len(nb))
        e[0x42] = typ
        struct.pack_into("<III", e, 0x44, left, right, child)
        struct.pack_into("<IQ", e, 0x74, start if typ else END, size)
        return bytes(e)
    ents = [entry("Root Entry", 5, child=1 if names else FREE)]
    for i, n in enumerate(names):
        ents.append(entry(n, 2, starts[n], sizes[n], right=(i + 2) if i + 1 < len(names) else FREE))
    dirb = b"".join(ents).ljust(dir_secs * 512, b"\x00")
    hdr = bytearray(512)
    hdr[:8] = MAGIC
    struct.pack_into("<HH", hdr, 0x18, 0x3E, 3)
    struct.pack_into("<H", hdr, 0x1C, 0xFFFE)
    struct.pack_into("<HH", hdr, 0x1E, 9, 6)
    struct.pack_into("<8I", hdr, 0x2C, 1, 1, 0, 4096, END, 0, END, 0)
    difat = [0] + [FREE] * 108
    struct.pack_into("<109I", hdr, 0x4C, *difat)
    body = b"".join(data[n].ljust(secs_per[n] * 512, b"\x00") for n in names)
    return bytes(hdr) + struct.pack("<128I", *fat) + dirb + body


def utf16(*names):
    return b"".join(n.encode("utf-16le") + b"\x00\x00\x00\x00" for n in names)


def se_part(body):
    return cfb({"Contents": b"x" * 100, "STREAM0.P_B": body})


def se_empty():
    return cfb({"Contents": b"x" * 100})


def se_asm(*comps):
    return cfb({"Components": utf16(*comps)})


def body(seed, n=20000):
    return bytes((seed * 31 + i * 7) % 251 for i in range(n))


def fuzz(b, pct):
    b = bytearray(b)
    for i in range(0, len(b), int(100 / pct)):
        b[i] ^= 0xFF
    return bytes(b)


STEP_OK = b"ISO-10303-21;\nDATA;\n#1=MANIFOLD_SOLID_BREP('a',#2);\nENDSEC;\n"
STEP_NO = b"ISO-10303-21;\nDATA;\n#1=PRODUCT('a','a','',(#2));\nENDSEC;\n"


def put(base, rel, content=b"x", mtime=None):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(content if isinstance(content, bytes) else content.encode())
    if mtime:
        os.utime(p, (mtime, mtime))


bad = []


def check(label, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})"))
    bad.append(not ok)


with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "Supplier")
    A = "JobA"
    real = body(1)
    put(src, f"{A}/3D/BRACKET.par", se_part(real), 1_700_000_000)
    put(src, f"{A}/3D/BRACKET COPY.par", se_part(real), 1_700_100_000)               # same body: duplicate, the newer file is kept
    put(src, f"{A}/3D/BRACKET RESAVE.par", se_part(fuzz(real, 1.0)), 1_600_000_000)  # same length, 1% differs: duplicate
    put(src, f"{A}/3D/PLATE.par", se_part(body(2)))
    put(src, f"{A}/3D/BLANK.par", se_empty())                                         # no body stream: blank
    put(src, f"{A}/3D/ZERO.par", b"")                                                 # 0-byte: blank
    put(src, f"{A}/3D/Fillet 3_oa_1.par", se_part(body(3)))                           # import debris
    put(src, f"{A}/3D/BASE.sldprt", b"SW" * 50)                                       # other format: byte hash only
    put(src, f"{A}/3D/SUB.asm", se_asm("PLATE.par", "BLANK.par"))
    put(src, f"{A}/3D/TOP FINAL.asm", se_asm("BRACKET.par", "BLANK.par", "SUB.asm", "GHOST.par", "Fillet 3_oa_1.par"))
    put(src, f"{A}/3D/TOP FINAL COPY.asm", se_asm("BRACKET.par", "BLANK.par", "SUB.asm", "GHOST.par", "Fillet 3_oa_1.par"))   # same component list
    put(src, f"{A}/3D/EMPTY.asm", se_asm())                                           # no components: blank
    put(src, f"{A}/3D/HOLLOW.asm", se_asm("BLANK.par", "Fillet 3_oa_1.par"))          # only placeholders / debris: blank
    put(src, f"{A}/DRAFTS/TOP FINAL.dft", cfb({"Refs": utf16("TOP FINAL.asm")}))      # references the main assembly -> Y
    put(src, f"{A}/DRAFTS/zero.dwg", b"")
    put(src, f"{A}/DRAFTS/ok.dwg", b"AC1027" + b"\x00" * 100)
    put(src, f"{A}/DRAFTS/ok copy.dwg", b"AC1027" + b"\x00" * 100)                    # byte-identical: duplicate
    put(src, f"{A}/DRAFTS/hollow.dxf", b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
    put(src, f"{A}/3D/m.stp", STEP_OK)
    put(src, f"{A}/3D/e.stp", STEP_NO)
    put(src, f"{A}/notes.txt", b"hello")
    put(src, f"{A}/notes copy.txt", b"hello")
    put(src, f"{A}/Thumbs.db", b"junk")
    put(src, f"{A}/3D/BRACKET.cfg", b"cfg")                                           # side file next to BRACKET.par: junk
    put(src, f"{A}/standalone.cfg", b"machine config")                                # no same-named CAD file: kept
    put(src, f"{A}/empty.txt", b"")                                                   # 0-byte other: junk
    with zipfile.ZipFile(os.path.join(src, A, "DRAFTS", "pack.zip"), "w") as z:
        z.writestr(zipfile.ZipInfo("inner/BRACKET.par", (2020, 1, 1, 0, 0, 0)), se_part(real))                                # copy of a loose file: duplicate
        z.writestr(zipfile.ZipInfo("inner/EXTRA.par", (2020, 1, 1, 0, 0, 0)), se_part(body(9)))                               # only in the zip: real
        z.writestr("readme.txt", b"zip readme")
    with zipfile.ZipFile(os.path.join(src, A, "outer.zip"), "w") as z:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zi:
            zi.writestr("deep.txt", b"deep file")
        z.writestr("nested.zip", buf.getvalue())
    put(src, f"{A}/bad.zip", b"not a zip at all")                                     # corrupt archive: Not Checked
    # a second project: a different job folder, with the same role-folder structure
    B = "JobB"
    put(src, f"{B}/3D/GEAR.par", se_part(body(5)))
    put(src, f"{B}/3D/GEAR ASM.asm", se_asm("GEAR.par"))
    put(src, f"{B}/DRAFTS/g.dxf", b"0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n0\nENDSEC\n0\nEOF\n")
    # a library folder
    put(src, "Standard Parts/bolt.par", se_part(body(7)))
    # a standalone folder with no CAD at all
    put(src, "Misc/readme.txt", b"no cad here")

    out = os.path.join(t, "out")
    xlsx, nproj, nfiles, problems = cs.scan(src, out, log=lambda *_: None, workers=4, host=fp, checkpoint=lambda: None)
    import zipfile as zf
    if os.environ.get('SCAN_TEST_KEEP'):
        import shutil; shutil.copy(xlsx, os.environ['SCAN_TEST_KEEP'])
    z = zf.ZipFile(xlsx)
    check("workbook has the 7 sheets of the template", sorted(n for n in z.namelist() if n.startswith("xl/worksheets/")), [f"xl/worksheets/sheet{i}.xml" for i in range(1, 8)])
    wbx = z.read("xl/workbook.xml").decode()
    check("sheet names in order", [s.split('"')[0] for s in wbx.split('<sheet name="')[1:]],
          ["Read Me", "Field Logic", "Projects", "Real Files", "Excluded Files", "Project Map", "Not Checked"])

    # read the workbook back with a tiny parser: sheet -> rows of cell text
    import re
    def sheet(n):
        xml = z.read(f"xl/worksheets/sheet{n}.xml").decode()
        rows = []
        for r in re.findall(r"<row [^>]*>(.*?)</row>", xml, re.S):
            cells = {}
            for m in re.finditer(r'<c r="([A-Z]+)\d+"[^>]*?(?:/>|>(.*?)</c>)', r, re.S):
                inner = m.group(2) or ""
                t_ = re.search(r"<t[^>]*>(.*?)</t>", inner, re.S)
                v = re.search(r"<v>(.*?)</v>", inner, re.S)
                cells[m.group(1)] = (t_.group(1) if t_ else (v.group(1) if v else "")).replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<")
            rows.append(cells)
        return rows
    proj = sheet(3)
    byname = {r.get("B"): r for r in proj[3:] if r.get("B")}
    print("projects:", [(r.get("A"), r.get("B"), r.get("C")) for r in proj[3:]])
    check("header row 3 has the 43 template columns", len(proj[2]), 43)
    a = byname.get("JobA")
    check("JobA is a project", a is not None, True)
    if a:
        g = lambda col: int(float(a.get(col) or 0))
        # parts: BRACKET, COPY, RESAVE, PLATE, BLANK, ZERO, Fillet.., BASE.sldprt, zip BRACKET, zip EXTRA = 10
        check("parts: total", g("J"), 10)
        check("parts: blank (no body + 0-byte)", g("M"), 2)
        check("parts: import debris", g("N"), 1)
        check("parts: duplicates (copy, re-save, zip copy)", g("L"), 3)
        check("parts: real distinct", g("K"), 4)           # BRACKET COPY (kept), PLATE, BASE.sldprt, EXTRA
        check("assemblies: total", g("F"), 5)
        check("assemblies: blank (empty + hollow)", g("I"), 2)
        check("assemblies: duplicates (same component list)", g("H"), 1)
        check("assemblies: real distinct", g("G"), 2)       # TOP FINAL (or its copy), SUB
        check("drawings: total", g("O"), 5)
        check("drawings: blank (0-byte + empty dxf)", g("R"), 2)
        check("drawings: duplicates", g("Q"), 1)
        check("exchange: total / blank / real", (g("S"), g("V"), g("T")), (2, 1, 1))
        check("other: junk counts Thumbs.db, empty.txt, BRACKET.cfg", g("Z"), 3)
        check("other: duplicates (notes copy, zip readme is different)", g("Y"), 1)
        check("not checked: bad.zip", g("AA"), 1)
        check("main assembly", a.get("AG", "").upper().startswith("TOP FINAL"), True)
        check("final drawing present (the .dft references the main assembly)", a.get("AN"), "Y")
        check("check column is 0", g("AQ"), 0)
        check("content level names the non-Solid-Edge format", a.get("AO", "").startswith("Partial"), True)
        refd, inp, oth, emp, mis = (g(c) for c in ("AH", "AI", "AJ", "AK", "AL"))
        # main assembly tree: BRACKET (real, in project), BLANK (placeholder only), GHOST (missing), Fillet 3_oa_1 (import body, in project),
        # SUB -> PLATE (real), BLANK again
        check("parts referenced", refd, 5)
        check("found real in project (BRACKET, Fillet body, PLATE)", inp, 3)
        check("found empty placeholder only (BLANK)", emp, 1)
        check("missing (GHOST)", mis, 1)
        check("part linkage % = in project / (referenced - placeholders)", round(float(a.get("AM") or 0), 3), round(3 / 4, 3))
    check("JobB is its own project", "JobB" in byname, True)
    check("Standard Parts is a Library", byname.get("Standard Parts", {}).get("C"), "Library")
    tot = proj[-1]
    check("All projects row sums the total files", int(float(tot.get("E") or 0)), sum(int(float(r.get("E") or 0)) for r in proj[3:-1]))
    real_rows = sheet(4)
    names = [r.get("D") for r in real_rows[1:]]
    check("Real Files lists the kept (newer) copy, not the older one", ("BRACKET COPY.par" in names, "BRACKET.par" in names), (True, False))
    excl = sheet(5)
    kinds = {}
    for r in excl[1:]:
        kinds.setdefault(r.get("F"), []).append(r.get("C"))
    check("Excluded: debris row", "Fillet 3_oa_1.par" in kinds.get("Import Debris", []), True)
    check("Excluded: duplicate rows name the kept file", any(r.get("F") == "Duplicate" and r.get("H") for r in excl[1:]), True)
    nc = sheet(7)
    check("Not Checked lists bad.zip with a reason", any("bad.zip" in (r.get("B") or "") and r.get("E") for r in nc[1:]), True)
    deep = [r for r in real_rows[1:] if r.get("D") == "deep.txt"]
    check("nested archive member is found and shows its archive chain", len(deep) == 1 and "outer.zip > nested.zip" in deep[0].get("F", ""), True)

print("\nFAILED" if any(bad) else "\nall ok")
sys.exit(1 if any(bad) else 0)
