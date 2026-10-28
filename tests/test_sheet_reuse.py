"""Exact copies of a drawing (same name, size, date) are not read twice; everything else is."""
import csv, io, os, sys, tempfile, zipfile, builtins
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def dxf(layouts):
    out = ["  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n  0\nSECTION\n  2\nOBJECTS\n"]
    for i, name in enumerate(["Model"] + [f"Layout{k}" for k in range(1, layouts)]):
        out.append(f"  0\nLAYOUT\n  5\n{0x30 + i:X}\n100\nAcDbPlotSettings\n  1\n\n100\nAcDbLayout\n  1\n{name}\n 70\n     1\n")
    out.append("  0\nENDSEC\n  0\nEOF\n")
    return "".join(out).encode()
with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "r"); out = os.path.join(t, "o")
    def put(rel, data, mtime=1_700_000_000):
        p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data); os.utime(p, (mtime, mtime))
    a = dxf(4)
    put("P1/plan.dxf", a); put("P2/copy/plan.dxf", a); put("P3/plan.dxf", a)                       # three exact copies
    put("P4/plan.dxf", dxf(2))                                                                  # same name, different content and size
    put("P5/plan.dxf", a, mtime=1_600_000_000)                                                   # same name and size, other date -> read
    opened = []; real_open = open
    def spy(f, *x, **k):
        if isinstance(f, str) and f.startswith(root) and f.endswith(".dxf"): opened.append(os.path.relpath(f, root))
        return real_open(f, *x, **k)
    builtins.open = spy
    try:
        z = fp.profile(root, out, log=lambda *_: None, resume=False, system_info=False, workers=1)[0]     # one worker: the first copy is finished before the next starts
    finally:
        builtins.open = real_open
    zf = zipfile.ZipFile(z)
    rows = {r["project"] + "/" + r["rel_path"]: r for r in csv.DictReader(io.StringIO(zf.read("Files.csv").decode("utf-8-sig")))}
    info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zf.read("Run_info.csv").decode("utf-8-sig")))}
    check("only one of the three exact copies was opened (plus the two different ones)", len(opened), 3)
    check("all five files have their sheet count and confidence", sorted((r["sheets"], r["sheets_conf"]) for r in rows.values()),
          sorted([("3", "99")] * 4 + [("1", "99")]))
    check("the copies carry exactly the same tag and count as the file that was read", len({(rows[k]["header_signature"], rows[k]["sheets"], rows[k]["sheets_conf"], rows[k]["sheets_status"]) for k in rows if k.endswith("plan.dxf") and rows[k]["size_bytes"] == str(len(a))}), 1)
    check("Run_info says how many counts were reused", info["dwg_sheets_reused_from_identical_copies"], "2")
sys.exit(1 if any(bad) else 0)
