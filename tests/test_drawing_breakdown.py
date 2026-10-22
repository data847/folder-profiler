"""Drawing Breakdown tab: one row per drawing file, the assembly it belongs to and HOW that was decided; rows add up to the project's
Native 2D Drawings + PDF Drawings.   Run: python tests/test_drawing_breakdown.py"""
import os, struct, sys, tempfile, zlib
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

def sw_file(refs):
    payload = b"".join(("D:\\old\\" + r).encode("utf-16le") + b"\x00\x00" for r in refs)
    c = zlib.compressobj(wbits=-15); data = c.compress(payload) + c.flush()
    return b"w0\xbc\"" + b"\x00" * 40 + b"\x14\x00\x06\x00\x08\x00" + struct.pack("<IIII", 0, len(data), len(payload), 0) + data

def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(body if isinstance(body, bytes) else body.encode())

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent"); Q = lambda rel, body=b"x": put(src, "P/" + rel, body)
    Q("A/A.sldasm", sw_file(["a1.sldprt"])); Q("A/a1.sldprt", "a1 data")
    Q("A/A.slddrw", sw_file(["A.sldasm"]))                 # drawing of the assembly: by reference
    Q("A/a1.slddrw", sw_file(["a1.sldprt"]))               # drawing of a part that belongs to A: by reference (through the part)
    Q("A/misc.slddrw", "no references at all")             # nothing to go on but its folder: one assembly there
    Q("A/A.pdf", "%PDF-1.4 a")                             # PDF named like the assembly
    Q("B/B1.sldasm", sw_file(["b1.sldprt"])); Q("B/B2.sldasm", sw_file(["b2.sldprt"]))
    Q("B/b1.sldprt", "b1"); Q("B/b2.sldprt", "b2 data"); Q("B/other.slddrw", "x other")   # two assemblies in the folder: cannot tell
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2)
    runs = cp.load_profiles([z]); em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, stats, review, asm, ev = cp.compile_runs(runs, em, sf, False, "auto", True, "folder")
    rows = {r[1]: r for r in ev["drawings"]}
    check("one row per drawing file (4 native drawings + 1 PDF drawing)", sorted(rows), ["A.pdf", "A.slddrw", "a1.slddrw", "misc.slddrw", "other.slddrw"])
    check("rows = Native 2D Drawings + PDF Drawings of the project", (len(ev["drawings"]), compiled[0][6] + compiled[0][7]), (len(ev["drawings"]), len(ev["drawings"])))
    check("A.slddrw -> A.sldasm by reference", rows["A.slddrw"][6:8], ["A.sldasm", "link"])
    check("a1.slddrw (drawing of a part) -> the part's assembly A by reference", rows["a1.slddrw"][6:8], ["A.sldasm", "link"])
    check("A.pdf -> A.sldasm by name", rows["A.pdf"][6:8], ["A.sldasm", "name"])
    check("misc.slddrw: only its folder to go on, one assembly there -> folder", rows["misc.slddrw"][6:8], ["A.sldasm", "folder"])
    check("other.slddrw: two assemblies in its folder -> not named, says why", (rows["other.slddrw"][6], "2 candidate" in rows["other.slddrw"][7]), ("", True))
    check("format and software columns", (rows["A.slddrw"][2], rows["A.slddrw"][3]), ("SLDDRW", "SolidWorks"))
    check("PDF row has no software", rows["A.pdf"][3], "")
    check("Review sums it up", any(r[1] == "drawing breakdown" and "5 drawing rows" in r[2] for r in review), True)
    # the real number of drawing files equals the tab
    nd = cp.compile_runs(runs, em, sf, False, "auto", True, "folder")[0][0]
    check("Project Compiled drawings + PDF drawings = tab rows", nd[6] + nd[7], len(ev["drawings"]))
sys.exit(1 if any(bad) else 0)
