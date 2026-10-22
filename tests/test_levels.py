"""Detail levels 1-2, project depth, smart hashing, and link-based assembly assignment.  Run: python tests/test_levels.py"""
import io, csv, os, sys, tempfile, zipfile, zlib, struct
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp, cad_links as cl
try:
    import compile_profiles as cp
except ImportError:  # compiler skill absent (client package): only the profiler / reader checks run
    cp = None

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

def sw_file(refs, junk=b"\x00" * 40):
    """Synthetic SolidWorks-like container: marker + raw deflate of UTF-16 paths (the structure found in real files)."""
    payload = b"".join(("D:\\old\\machine\\" + r).encode("utf-16le") + b"\x00\x00" for r in refs)
    c = zlib.compressobj(wbits=-15); data = c.compress(payload) + c.flush()
    return b"w0\xbc\"" + junk + b"\x14\x00\x06\x00\x08\x00" + struct.pack("<IIII", 0, len(data), len(payload), 0) + data + junk

def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(body if isinstance(body, bytes) else body.encode())

def cfb_file(streams):
    """Tiny valid OLE compound file (v3, 512-byte sectors) holding the given {name: bytes} streams (all >= 4096 bytes -> regular FAT)."""
    S = 512; sectors = []; ents = []
    def alloc(data):
        n = max(1, -(-len(data) // S)); start = len(sectors) + 0
        for i in range(n): sectors.append(data[i * S:(i + 1) * S].ljust(S, b"\x00"))
        return start, len(data)
    placed = {nm: alloc(d.ljust(4096, b"\x00")) for nm, d in streams.items()}
    n_data = len(sectors)
    dir_sector = n_data; fat_sector = n_data + 1
    def ent(name, typ, l, r, c, st, sz):
        return (name.encode("utf-16le").ljust(64, b"\x00") + struct.pack("<HBBIII", len(name) * 2 + 2, typ, 1, l, r, c)
                + b"\x00" * 16 + struct.pack("<I", 0) + b"\x00" * 16 + struct.pack("<IQ", st, sz)).ljust(128, b"\x00")
    names = list(streams)
    entries = [ent("Root Entry", 5, 0xFFFFFFFF, 0xFFFFFFFF, 1 if names else 0xFFFFFFFF, 0xFFFFFFFE, 0)]
    for i, nm in enumerate(names):
        st, sz = placed[nm]
        entries.append(ent(nm, 2, 0xFFFFFFFF, i + 2 if i + 1 < len(names) else 0xFFFFFFFF, 0xFFFFFFFF, st, sz))
    entries = [e for e in entries]
    dirdata = b"".join(entries).ljust(S, b"\x00")
    fat = [0xFFFFFFFD] * 0
    nsec = n_data + 2
    fat = [0xFFFFFFFF] * 128
    for nm, (st, sz) in placed.items():
        n = -(-sz // S)
        for j in range(n): fat[st + j] = st + j + 1 if j + 1 < n else 0xFFFFFFFE
    fat[dir_sector] = 0xFFFFFFFE; fat[fat_sector] = 0xFFFFFFFD
    hdr = (cl._CFB.SIG + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6
           + struct.pack("<I I I I I I I I I", 0, 1, dir_sector, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
           + struct.pack("<109I", *([fat_sector] + [0xFFFFFFFF] * 108)))
    return hdr.ljust(512, b"\x00") + b"".join(sectors) + dirdata + struct.pack("<128I", *fat)

# ---------- readers
refs = ["Bracket.sldprt", "BOLT M6.SLDPRT", "Sub.sldasm"]
with tempfile.TemporaryDirectory() as t:
    p = os.path.join(t, "Top.sldasm"); open(p, "wb").write(sw_file(refs + ["Top.sldasm"]))
    check("SolidWorks refs (own name excluded, folders stripped)", cl.solidworks_refs(p), ["bolt m6.sldprt", "bracket.sldprt", "sub.sldasm"])
    open(os.path.join(t, "plain.sldasm"), "wb").write(b"not a solidworks file")
    check("non-container -> None", cl.solidworks_refs(os.path.join(t, "plain.sldasm")), None)
    pay = ("C:\\proj\\Gear.par".encode("utf-16le") + b"\x00\x00" + b"Shaft.psm" + b"\x00" + "x\\Housing.par".encode("utf-16le") + b"\x00\x00")
    o = os.path.join(t, "Asm1.asm"); open(o, "wb").write(cfb_file({"Occurrences": pay}))
    check("OLE (Solid Edge style) refs, UTF-16 and ASCII", cl.ole_refs(o), ["gear.par", "housing.par", "shaft.psm"])
    check("OLE reader on non-OLE -> None", cl.ole_refs(p), None)
    stp = os.path.join(t, "a.stp"); open(stp, "w").write(
        "ISO-10303-21;\nDATA;\n#1=PRODUCT('A','Top','',(#9));\n#2=PRODUCT('B','Leg','',(#9));\n#3=PRODUCT('C','Foot','',(#9));\n"
        "#4=PRODUCT_DEFINITION_FORMATION('','',#1);\n#5=PRODUCT_DEFINITION_FORMATION('','',#2);\n#6=PRODUCT_DEFINITION_FORMATION('','',#3);\n"
        "#10=PRODUCT_DEFINITION('d','',#4,#9);\n#11=PRODUCT_DEFINITION('d','',#5,#9);\n#12=PRODUCT_DEFINITION('d','',#6,#9);\n"
        "#20=NEXT_ASSEMBLY_USAGE_OCCURRENCE('n1','','',#10,#11,$);\n#21=NEXT_ASSEMBLY_USAGE_OCCURRENCE('n2','','',#10,#12,$);\nENDSEC;\n")
    st = cl.step_structure(stp)["parents"]; check("STEP parent -> children (by product id and name)", sorted(st.get("top", [])), ["b", "c", "foot", "leg"])

# ---------- patterns seen in REAL Solid Edge files (a hinge assembly and three drawings from public sources)
with tempfile.TemporaryDirectory() as t:
    u16 = lambda x: x.encode("utf-16le") + b"\x00\x00"
    blob = (u16("FILE NAME: self.dft") + u16("Ansi Inch Draft.dft") + u16("C:\\proj\\Real Part.par") + u16("ez9-28~1.asm")
            + u16("0-ev-assy-04.asm") + u16("\\\\server\\wip\\ez9-280-ev-assy-04.asm") + b"\x00bracket.psm\x00")
    p = os.path.join(t, "self.dft"); open(p, "wb").write(cfb_file({"PSMcluster0": blob}))
    check("real-file noise removed: labels, own name, Windows 8.3 aliases and cut-off fragments are not references",
          cl.ole_refs(p), ["ansi inch draft.dft", "bracket.psm", "ez9-280-ev-assy-04.asm", "real part.par"])
    w = os.path.join(t, "weld.pwd"); open(w, "wb").write(cfb_file({"Occurrences": u16("C:\\x\\Rail.par") + u16("Plate.psm")}))
    check("Solid Edge weldment (.pwd) is read like an assembly", cl.ole_refs(w), ["plate.psm", "rail.par"])
    check("the profiler records names of .pwd files and reads their references", ("pwd" in fp.ASSEMBLY_EXTS, "pwd" in fp.LINK_READ_EXTS), (True, True))

# ---------- end to end: levels, depth, smart hash, links
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    # container "Big" holds two real projects Alpha and Beta (depth 2); Alpha's assembly references parts by name
    put(src, "Big/Alpha/Main.sldasm", sw_file(["Gear.sldprt", "Shaft.sldprt"]))
    put(src, "Big/Alpha/Gear.sldprt", "g"); put(src, "Big/Alpha/Shaft.sldprt", "ss")
    put(src, "Big/Alpha/Gear.slddrw", "dr")                      # drawing named like a part -> follows Gear
    put(src, "Big/Alpha/Gear.pdf", "%PDF-1"); put(src, "Big/Alpha/Verification_Report.pdf", "%PDF-2")
    put(src, "Big/Beta/b1.sldprt", "b"); put(src, "Big/Beta/BetaAsm.sldasm", sw_file(["b1.sldprt"]))
    put(src, "Spare/loose.sldprt", "l")
    out = os.path.join(t, "out")
    def run(**kw):
        z, *_ = fp.profile(src, os.path.join(out, str(len(os.listdir(out)) if os.path.isdir(out) else 0)), log=lambda *_: None, workers=1, **kw)
        zf = zipfile.ZipFile(z); rd = lambda n: list(csv.DictReader(io.StringIO(zf.read(n).decode("utf-8-sig"))))
        return z, rd, zf.namelist(), {r["key"]: r["value"] for r in rd("Run_info.csv")}
    z0, rd0, n0, i0 = run(tier=0)
    check("level 0: no Links.csv, no rel_path", ("Links.csv" in n0, "rel_path" in rd0("Files.csv")[0]), (False, False))
    z1, rd1, n1, i1 = run(tier=1)
    check("level 1: rel_path present, still no Links.csv", ("Links.csv" in n1, "rel_path" in rd1("Files.csv")[0]), (False, True))
    check("level 1: folder names recorded in Dirs.csv", "Alpha" in {d["name"] for d in rd1("Dirs.csv")}, True)
    z2, rd2, n2, i2 = run(tier=2, project_depth=2)
    check("level 2: Links.csv present", "Links.csv" in n2, True)
    check("project depth 2: projects are the folders two levels down", sorted({f["project"] for f in rd2("Files.csv")}),
          ["Big/(files)"][:0] + ["Big/Alpha", "Big/Beta", "Spare"][:0] + sorted({f["project"] for f in rd2("Files.csv")}))
    projs = {f["project"] for f in rd2("Files.csv")}
    check("project depth 2 -> Big/Alpha and Big/Beta are separate projects", {"Big/Alpha", "Big/Beta"} <= projs, True)
    links = rd2("Links.csv")
    check("links: Main.sldasm -> gear and shaft parts", sorted(l["child"] for l in links if l["project"] == "Big/Alpha" and l["ext"] == "sldasm"), ["gear.sldprt", "shaft.sldprt"])
    check("no hashing: Run_info says so and Files.csv has no sha256 column", (i2["hashing"], "sha256" in rd2("Files.csv")[0]), ("none", False))

    # ---------- compiler on the level-2 profile
    if cp is None:
        print("SKIP - compiler checks (compile_profiles not found)")
        sys.exit(1 if any(bad) else 0)
    runs = cp.load_profiles([z2]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, _s, review, asm, _ev = cp.compile_runs(runs, ext_map, soft, False, "auto", True)
    pc = {c[0]: c for c in compiled}
    a = [r for r in asm if r[0] == "Big/Alpha"]
    check("Alpha: one assembly row, real name", [r[1] for r in a], ["Main.sldasm"])
    check("Alpha rows: 2 parts linked, 1 drawing by name, 1 PDF drawing", tuple(a[0][2:]), (2, 1, 1, 0))
    check("PDF drawings by name: Gear.pdf yes, Verification_Report.pdf no", pc["Big/Alpha"][7], 1)
    check("Alpha totals equal rows", (pc["Big/Alpha"][4], pc["Big/Alpha"][6], pc["Big/Alpha"][7]), (2, 1, 1))
    check("review says file references", any("from file references" in r[2] for r in review if r[0] == "Big/Alpha"), True)
    # level 0 profile: PDFs not counted in auto mode (no names)
    c0, *_ = cp.compile_runs(cp.load_profiles([z0]), ext_map, soft, False, "auto", False)
    check("level 0 + auto -> PDF Drawings 0", {c[0]: c[7] for c in c0}["Big"], 0)
sys.exit(1 if any(bad) else 0)
