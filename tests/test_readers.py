"""Level-2 readers: the fast STEP parser equals the line parser, one open gives tag + references, the string-scan reader for CATIA/Creo/NX,
and the application tag of OLE files.   Run: python tests/test_readers.py
(The CATIA / Creo / NX files here are SYNTHETIC - the point is the mechanics; no real file of those programs was available.)"""
import io, os, struct, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cad_links as cl
import file_profiler as fp

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

def step_text(n_products=30, filler=2000, multiline=True):
    out = ["ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"]
    n, pds = 1, []
    for i in range(n_products):
        p, f, pd = n, n + 1, n + 2
        n += 3
        out.append(f"#{p} = PRODUCT('part{i}','Part; {i}','',(#900));\n")
        out.append(f"#{f} = PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE('','',#{p},\n   .NOT_KNOWN.);\n" if multiline else f"#{f} = PRODUCT_DEFINITION_FORMATION('','',#{p});\n")
        out.append(f"#{pd}=PRODUCT_DEFINITION('design','',#{f},#901);\n")
        pds.append(pd)
    for i in range(1, n_products):
        out.append(f"#{n} = NEXT_ASSEMBLY_USAGE_OCCURRENCE('{i}','','',#{pds[0] if i < 10 else pds[3]},#{pds[i]},$);\n")
        n += 1
    out += [f"#{n + i} = CARTESIAN_POINT('',({i}.0,2.0,3.0));\n" for i in range(filler)]
    out.append("ENDSEC;\nEND-ISO-10303-21;\n")
    return "".join(out)

txt = step_text()
ref = cl._step_from_lines(io.StringIO(txt))
check("fast STEP parser == line parser", cl.step_structure_bytes(txt.encode()) == ref, True)
check("the answer is not empty", len(ref["parents"]) > 0, True)
for blk in (64, 97, 1000):                                      # statements cut by block boundaries
    old = cl.STEP_BLOCK; cl.STEP_BLOCK = blk
    check(f"block size {blk}: same result", cl.step_structure_bytes(txt.encode()) == ref, True)
    cl.STEP_BLOCK = old
check("a STEP with no product structure gives {} (single part)", cl.step_structure_bytes(b"ISO-10303-21;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;"), {"parents": {}})

with tempfile.TemporaryDirectory() as t:
    # ---- one open: tag + references together
    p = os.path.join(t, "top.step"); open(p, "w").write(txt)
    opens = []
    real_open = open
    import builtins
    def counting_open(path, *a, **k):
        if str(path) == p: opens.append(1)
        return real_open(path, *a, **k)
    builtins.open = counting_open
    try:
        with open(p, "rb") as fh:
            sig = fp.signature_stream(fh, "step", os.path.getsize(p)); fh.seek(0)
            res = cl.read_links_file(fh, "step", "top.step", os.path.getsize(p))
    finally:
        builtins.open = real_open
    check("one open gives the STEP tag and the product tree", (sig.startswith("STEP:"), res[0], len(res[1]) > 0, len(opens)), (True, "step", True, 1))

    # ---- string-scan reader (CATIA V5 / Creo / NX): synthetic files
    cat = b"V5_CFV2\x00\x01\x02" + b"\x00" * 50 + b"C:\\work\\parts\\Bolt.CATPart\x00junk" + "Nut M6.CATPart".encode("utf-16le") + b"\x00\x00" + b"readme.txt\x00" + b"Sub\\Washer.CATProduct\x00" + os.urandom(4000)
    pc = os.path.join(t, "Top.CATProduct"); open(pc, "wb").write(cat)
    r = cl.read_links(pc, "catproduct")
    check("CATProduct (scan): kind and the referenced parts / sub-assembly", (r[0], r[1]), ("scan", ["bolt.catpart", "nut m6.catpart", "washer.catproduct"]))
    check("CATIA V5 tag from the first bytes", fp.signature(pc, "catproduct", len(cat)), "CATIA:V5")
    creo = b"#UGC:2 PRT_ASSEMBLY\x00" + b"\x00" * 30 + b"bracket.prt\x00 bolt.prt\x00" + os.urandom(2000)
    pcreo = os.path.join(t, "frame.asm"); open(pcreo, "wb").write(creo)
    r = cl.read_links(pcreo, "asm")
    check("Creo .asm (not OLE): read by scan", (r[0], r[1]), ("scan", ["bolt.prt", "bracket.prt"]))
    check("Creo tag", fp.signature(pcreo, "asm", len(creo)), "CREO")
    # a scan never lists the file itself
    pself = os.path.join(t, "lone.prt"); open(pself, "wb").write(b"\x00lone.prt\x00other.prt\x00" + os.urandom(100))
    check("a file does not reference itself", cl.read_links(pself, "prt")[1], ["other.prt"])

    # ---- OLE application tags (a minimal compound file with the given entry names)
    def ole(names):
        sec = 512; ents = []
        def entry(name, typ):
            nb = name.encode("utf-16le") + b"\x00\x00"
            e = bytearray(128); e[:len(nb)] = nb; struct.pack_into("<H", e, 0x40, len(nb)); e[0x42] = typ
            struct.pack_into("<III", e, 0x44, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF); return bytes(e)
        raw = entry("Root Entry", 5) + b"".join(entry(n, 2) for n in names)
        raw = raw.ljust(512, b"\x00")
        hdr = bytearray(512); hdr[:8] = cl.OLE_MAGIC
        struct.pack_into("<HH", hdr, 0x18, 0x3E, 3); struct.pack_into("<H", hdr, 0x1C, 0xFFFE)
        struct.pack_into("<HH", hdr, 0x1E, 9, 6)
        struct.pack_into("<I", hdr, 0x2C, 1); struct.pack_into("<I", hdr, 0x30, 0)        # one FAT sector, directory at sector 0
        return bytes(hdr) + raw
    for names, want in ((["PSMroots", "PARASOLID"], "OLE2:SolidEdge"), (["JSite12", "Display"], "OLE2:SolidEdge"),
                        (["RSeSegInfo", "x"], "OLE2:Inventor"), (["BasicFileInfo", "Global"], "OLE2:Revit"),
                        (["WordDocument", "1Table"], "OLE2:Office"), (["Whatever"], "OLE2")):
        po = os.path.join(t, "x.bin"); b = ole(names); open(po, "wb").write(b)
        check(f"OLE entry names {names} -> tag", fp.signature(po, "bin", len(b)), want)
    # a corrupt OLE header never raises
    po = os.path.join(t, "bad.asm"); open(po, "wb").write(cl.OLE_MAGIC + b"\xff" * 100)
    check("corrupt OLE file: plain 'OLE2' tag, no crash", fp.signature(po, "asm", 108), "OLE2")
sys.exit(1 if any(bad) else 0)
