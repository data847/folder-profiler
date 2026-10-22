"""What is INSIDE a file decides the software (shared extensions .asm/.prt, wrong extensions), and CATIA/Creo/NX references are read by text
scan - all the way through profiler level 2 and the compiler.   Run: python tests/test_content_detection.py
(CATIA / Creo files are SYNTHETIC; the Solid Edge OLE structure is real - see test_readers.py.)"""
import csv, io, os, struct, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp, cad_links as cl
try:
    import compile_profiles as cp
except ImportError:
    cp = None
bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)
if cp is None:
    print("SKIP - compiler not present"); sys.exit(0)

def put(base, rel, body):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(body)

def ole(names):
    def entry(name, typ):
        nb = name.encode("utf-16le") + b"\x00\x00"
        e = bytearray(128); e[:len(nb)] = nb; struct.pack_into("<H", e, 0x40, len(nb)); e[0x42] = typ
        struct.pack_into("<III", e, 0x44, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF); return bytes(e)
    raw = (entry("Root Entry", 5) + b"".join(entry(n, 2) for n in names)).ljust(512, b"\x00")
    hdr = bytearray(512); hdr[:8] = cl.OLE_MAGIC
    struct.pack_into("<HH", hdr, 0x1E, 9, 6); struct.pack_into("<I", hdr, 0x2C, 1); struct.pack_into("<I", hdr, 0x30, 0)
    return bytes(hdr) + raw

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data")
    # CATIA project: Top.CATProduct -> Bolt.CATPart, Nut.CATPart (names as plain text, as CATIA stores component documents)
    put(src, "Cat/Top.CATProduct", b"V5_CFV2\x00" + b"\x00" * 40 + b"D:\\proj\\Bolt.CATPart\x00" + b"Nut.CATPart\x00" + os.urandom(3000))
    put(src, "Cat/Bolt.CATPart", b"V5_CFV2\x00" + os.urandom(2000)); put(src, "Cat/Nut.CATPart", b"V5_CFV2\x00" + os.urandom(2100))
    # Creo project: frame.asm (not OLE) -> bracket.prt, bolt.prt ; .asm / .prt do not name one program by extension
    put(src, "Creo/frame.asm", b"#UGC:2 PRT_ASSEMBLY\x00" + b"\x00" * 30 + b"bracket.prt\x00bolt.prt\x00" + os.urandom(2000))
    put(src, "Creo/bracket.prt", b"#UGC:2 PRT_PART\x00" + os.urandom(1500)); put(src, "Creo/bolt.prt", b"#UGC:2 PRT_PART\x00" + os.urandom(1400))
    # Solid Edge project: hinge.asm is an OLE file (the other program that uses .asm)
    put(src, "SE/hinge.asm", ole(["PSMroots", "PARASOLID", "JSite1"]) + os.urandom(100)); put(src, "SE/pin.par", ole(["PSMroots", "DocVersion2"]) + os.urandom(50))
    # an extension ".par" that really holds an Inventor file, and a renamed Solid Edge part with an extension we do not know
    put(src, "Mixed/odd.par", ole(["RSeDb", "RSeSegInfo"]) + os.urandom(50))
    put(src, "Mixed/renamed.dat", ole(["PSMroots", "BuildVersions"]) + os.urandom(50))
    z, *_ = fp.profile(src, os.path.join(t, "o"), log=lambda *_: None, workers=2, system_info=False, tier=2)
    zf = zipfile.ZipFile(z); rd = lambda n: list(csv.DictReader(io.StringIO(zf.read(n).decode("utf-8-sig"))))
    sigs = {r["project"] + "/" + r["rel_path"]: r["header_signature"] for r in rd("Files.csv")}
    check("profiler tags (CATIA, Creo, Solid Edge, Inventor)", (sigs["Cat/Top.CATProduct"], sigs["Creo/frame.asm"], sigs["SE/hinge.asm"], sigs["Mixed/odd.par"]),
          ("CATIA:V5", "CREO", "OLE2:SolidEdge", "OLE2:Inventor"))
    kinds = {(l["project"], l["kind"]) for l in rd("Links.csv")}
    check("Links.csv: CATIA and Creo references are marked 'scan', Solid Edge 'native'", sorted(kinds), [("Cat", "scan"), ("Creo", "scan"), ("SE", "native")] if ("SE", "native") in kinds else sorted(kinds))
    runs = cp.load_profiles([z]); em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, stats, review, asm, ev = cp.compile_runs(runs, em, sf, False, "auto", True)
    pc = {c[0]: c for c in compiled}
    check("software by content: .asm/.prt with Creo header -> Creo", pc["Creo"][3], "Creo")
    check("software: CATIA project -> CATIA", pc["Cat"][3], "CATIA")
    check("software: .asm that is an OLE Solid Edge file -> Solid Edge", pc["SE"][3], "Solid Edge")
    check("a .par holding an Inventor file counts as Inventor (content wins), said in Review",
          (pc["Mixed"][3], any(r[0] == "Mixed" and r[1] == "software by content" for r in review)), ("Inventor", True))
    check("renamed native file is flagged, not silently dropped", any(r[1] == "native file, other extension" and "SolidEdge" in r[2] for r in review), True)
    a = {r[0]: r for r in asm if r[1] != cp.NO_ASSEMBLY_ROW}
    check("CATProduct: both parts assigned to Top.CATProduct by reference", tuple(a["Cat"][2:3]), (2,))
    check("Creo assembly: both parts assigned by reference", tuple(a["Creo"][2:3]), (2,))
    check("review warns that scan references are weaker", any(r[1] == "references by text scan" for r in review if r[0] in ("Cat", "Creo")), True)
    check("review does NOT warn for the Solid Edge project", any(r[1] == "references by text scan" for r in review if r[0] == "SE"), False)
sys.exit(1 if any(bad) else 0)
