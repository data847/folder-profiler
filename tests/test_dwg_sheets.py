"""AutoCAD sheet (layout) counting: DXF, the LZ77 page decoder, damaged input, and - when real files are available - agreement of every
DWG version with the DXF export of the same drawing.   Run: python tests/test_dwg_sheets.py
Real files: set DWG_SAMPLES to a folder with sub-folders dwgsrc2000 ... dwgsrc2018 holding .dwg files (and .dxf twins), e.g. the public
test data of LibreDWG (test/test-data/<version>/). Without it the real-file part is skipped."""
import glob, os, random, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import dwg_sheets as ds

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

def dxf(layouts, model=True):
    out = ["  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n  0\nSECTION\n  2\nOBJECTS\n"]
    for i, name in enumerate((["Model"] if model else []) + [f"Layout{k}" for k in range(1, layouts)]):
        out.append(f"  0\nLAYOUT\n  5\n{0x30 + i:X}\n100\nAcDbPlotSettings\n  1\n\n100\nAcDbLayout\n  1\n{name}\n 70\n     1\n")
    out.append("  0\nENDSEC\n  0\nEOF\n")
    return "".join(out).encode()

# ---- DXF
r = ds.count_sheets_bytes(dxf(4), "dxf"); check("DXF with Model + 3 layouts -> 3 sheets, confidence 99", (r["sheets"], r["confidence"]), (3, 99))
r = ds.count_sheets_bytes(dxf(1), "dxf"); check("DXF with only the Model layout -> 0 sheets", (r["sheets"], r["confidence"]), (0, 99))
r = ds.count_sheets_bytes(dxf(3, model=False).replace(b"\r", b""), "dxf"); check("DXF whose Model layout is not named: lower confidence", (r["sheets"], r["confidence"]), (1, 80))
r = ds.count_sheets_bytes(dxf(3).replace(b"\n", b"\r\n"), "dxf"); check("DXF with Windows line ends", r["sheets"], 2)
r = ds.count_sheets_bytes(b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n  0\nEOF\n", "dxf"); check("old DXF without layouts: not counted, confidence 0", (r["sheets"], r["confidence"]), (None, 0))
r = ds.count_sheets_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 50, "dxf"); check("binary DXF: not supported", (r["sheets"], r["confidence"]), (None, 0))
text = dxf(3) + b"  0\nCLASS\n  1\nLAYOUT\n"
check("a CLASS record named LAYOUT is not a layout", ds.count_sheets_bytes(text, "dxf")["sheets"], 2)

# ---- LZ77 page decoder (hand-assembled streams in the published opcode format)
check("literal run only", ds.lz77(bytes([0x01, 0x61, 0x62, 0x63, 0x64, 0x11]), 4), b"abcd")
check("literal run + back-reference of 4 bytes", ds.lz77(bytes([0x01, 0x61, 0x62, 0x63, 0x64, 0x5C, 0x00, 0x11]), 8), b"abcdabcd")
check("wrong expected length -> refused", ds.lz77(bytes([0x01, 0x61, 0x62, 0x63, 0x64, 0x11]), 5), None)
check("overlapping copy repeats the pattern", ds.lz77(bytes([0x01, 0x61, 0x62, 0x63, 0x64, 0x9C, 0x00, 0x11]), 12), b"abcdabcdabcd")

# ---- damaged / hostile input never raises and never claims confidence
rnd = random.Random(7)
for v in (b"AC1012", b"AC1014", b"AC1015", b"AC1018", b"AC1021", b"AC1024", b"AC1027", b"AC1032", b"AC9999", b"%PDF-1"):
    for blob in (v, v + bytes(rnd.randrange(256) for _ in range(5000)), v + b"\x00" * 3000):
        try:
            r = ds.count_sheets_bytes(blob, "dwg")
            ok = r["confidence"] == 0 and r["sheets"] is None
        except Exception as e:
            ok = False
    check(f"damaged {v.decode()} header: no exception, confidence 0", ok, True)
check("unknown extension", ds.count_sheets_bytes(b"x", "txt")["sheets"], None)

# ---- real files (optional)
base = os.environ.get("DWG_SAMPLES", "")
dirs = sorted(glob.glob(os.path.join(base, "dwgsrc*"))) if base else []
if not dirs:
    print("SKIP - real DWG files (set DWG_SAMPLES to run)")
else:
    agree = dis = counted = total = 0
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.dwg"))):
            r = ds.count_sheets_bytes(open(f, "rb").read(), "dwg"); total += 1
            if r["sheets"] is not None:
                counted += 1
            twin = f[:-4] + ".dxf"
            if os.path.exists(twin) and r["sheets"] is not None:
                t = ds.count_sheets_bytes(open(twin, "rb").read(), "dxf")["sheets"]
                agree += t == r["sheets"]; dis += t != r["sheets"]
    check("real DWG files agree with their DXF twins", (agree > 0, dis), (True, 0))
    v2007 = sum(1 for d in dirs if d.endswith("2007") for _ in glob.glob(os.path.join(d, "*.dwg")))
    check("real DWG files counted (all versions except 2007)", total - counted <= v2007 + 2, True)
sys.exit(1 if any(bad) else 0)
