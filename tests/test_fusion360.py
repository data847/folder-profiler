"""Fusion 360 files: .f3d part, .f3z assembly, .f2d drawing, Fusion Electronics .fsch/.fbrd/.flbr; domain follows the software."""
import os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
try:
    import compile_profiles as cp
except ImportError:
    print("SKIP"); sys.exit(0)
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def zipit(rows, path):
    hdr = "project,file_no,ext,size_bytes,modified_utc,depth,header_signature,dir_id,asm_name,rel_path\r\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Files.csv", hdr + "\r\n".join(f'"{p}",{i},{e},{s},2024-01-01T00:00:0{i % 10}Z,1,ZIP,d1,,"{p}/{n}"' for i, (p, e, s, n) in enumerate(rows, 1)) + "\r\n")
        z.writestr("Run_info.csv", "key,value\r\nfolder_name,v\r\n")
        z.writestr("Projects.csv", "project,file_count,total_bytes,distinct_extensions,first_modified_utc,last_modified_utc\r\n")
with tempfile.TemporaryDirectory() as t:
    zp = os.path.join(t, "v.zip")
    zipit([("F1", "f3d", 90000 + i, f"p{i}.f3d") for i in range(3)] + [("F1", "f3z", 500000, "asm.f3z"), ("F1", "f2d", 60000, "sheet.f2d"),
          ("F2", "fsch", 9000, "a.fsch"), ("F2", "fbrd", 9000, "a.fbrd"), ("F2", "flbr", 9000, "a.flbr")], zp)
    em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, stats, review, asm, ev = cp.compile_runs(cp.load_profiles([zp]), em, sf, False, "auto", True, "folder")
    pc = {c[0]: dict(zip(cp.HEADINGS, c)) for c in compiled}
    f1, f2 = pc["v/F1"], pc["v/F2"]
    check("F1 domain / software", (f1["Domain"], f1["Native CAD Software"]), ("Mechanical", "Fusion 360"))
    check("F1: 3 parts, 1 assembly, 1 native drawing", (f1["Part Files"], f1["Assembly Files"], f1["Native 2D Drawings"]), (3, 1, 1))
    check("F2 domain / software", (f2["Domain"], f2["Native CAD Software"]), ("Electronics", "Fusion 360 Electronics"))
    check("F2: Fusion Electronics files are EDA, not parts / drawings", (f2["Part Files"], f2["Native 2D Drawings"]), (0, 0))
    sh = {s: [r[0] for r in rows] for s, rows in ev["domain_sheets"].items() if rows}
    check("domain sheets", sh, {"Mechanical": ["v/F1"], "Electronics": ["v/F2"]})
    el = next(r for r in ev["domain_sheets"]["Electronics"] if r[0] == "v/F2")
    import domain_metrics as dm
    check("F2 schematic / layout / library files", [el[dm.ELEC_HEADINGS.index(h)] for h in ("Schematic Files", "Layout (PCB) Files", "Library Files")], [1, 1, 1])
sys.exit(1 if any(bad) else 0)
