"""Domain classification: specific tools vote, general-purpose CAD (AutoCAD / Rhino) does not, names are the fallback, and every decision
carries its basis and confidence.   Run: python tests/test_domain.py"""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
try:
    import compile_profiles as cp
except ImportError:
    print("SKIP - compiler not present"); sys.exit(0)
bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

HDR = "project,file_no,ext,size_bytes,modified_utc,depth,header_signature,dir_id,asm_name,rel_path\r\n"
def profile(projects, tmp):
    rows, counts = [], {}
    for proj, files in projects.items():
        for i, (ext, name) in enumerate(files, 1):
            rows.append(f'"{proj}",{i},{ext},{1000 + i},2024-01-01T00:00:00Z,1,BINARY,d1,,"{name}.{ext}"')
        counts[proj] = len(files)
    zp = os.path.join(tmp, "p.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Files.csv", "﻿" + HDR + "\r\n".join(rows) + "\r\n")
        z.writestr("Run_info.csv", "key,value\r\nfolder_name,t\r\n")
        z.writestr("Projects.csv", "project,file_count,total_bytes,distinct_extensions,first_modified_utc,last_modified_utc\r\n" +
                   "".join(f'"{p}",{n},0,1,,\r\n' for p, n in counts.items()))
    return zp

dwgs = lambda names, ext="dwg": [(ext, n) for n in names]
with tempfile.TemporaryDirectory() as t:
    P = {
        "A tools": [("skp", f"model{i}") for i in range(3)] + [("dwg", f"x{i}") for i in range(200)],          # 3 SketchUp + 200 AutoCAD: AutoCAD does not vote
        "B names arch": dwgs([f"GROUND FLOOR PLAN {i}" for i in range(30)] + [f"ELEVATION {i}" for i in range(30)]),
        "C names mech": dwgs([f"gearbox housing {i}" for i in range(15)] + [f"shaft bracket {i}" for i in range(15)]),
        "D nothing": dwgs([f"a{i}" for i in range(40)]),
        "E solidworks": [("sldprt", f"p{i}") for i in range(30)] + [("dwg", f"d{i}") for i in range(30)],
        "F mixed": [("sldprt", f"p{i}") for i in range(10)] + [("skp", f"s{i}") for i in range(8)],
        "G kicad": [("kicad_pcb", "board"), ("kicad_sch", "sheet")],
        "H few names": dwgs(["floor plan 1", "x1", "x2", "x3"]),
        "I tools vs names": [("sldprt", f"p{i}") for i in range(12)] + dwgs([f"floor plan {i}" for i in range(40)]),
    }
    zp = profile(P, t)
    em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, stats, review, *_ = cp.compile_runs(cp.load_profiles([zp]), em, sf, False, "none", False, "folder")
    dom = {c[0]: c[2] for c in compiled}
    st = {s[0]: s for s in stats}
    bi, ci = cp.STATS_HEADER.index("domain_basis"), cp.STATS_HEADER.index("domain_confidence")
    check("3 SketchUp files outweigh 200 AutoCAD files (general-purpose CAD does not vote)", (dom["A tools"], st["A tools"][bi]), ("Architecture", "specific tools"))
    check("AutoCAD only, names say floor plan / elevation -> Architecture by names, High", (dom["B names arch"], st["B names arch"][bi], st["B names arch"][ci]), ("Architecture", "file and folder names", "High"))
    check("AutoCAD only, names say gearbox / shaft / bracket -> Mechanical by names", (dom["C names mech"], st["C names mech"][bi]), ("Mechanical", "file and folder names"))
    check("AutoCAD only, meaningless names -> Unclassified (and says so), no guess", (dom["D nothing"], st["D nothing"][bi]), ("Unclassified", "none"))
    check("SolidWorks + AutoCAD -> Mechanical by tools, High", (dom["E solidworks"], st["E solidworks"][bi], st["E solidworks"][ci]), ("Mechanical", "specific tools", "High"))
    check("SolidWorks vs SketchUp mix: a winner, confidence not High, mixed domains reported",
          (st["F mixed"][ci] != "High", any(r[0] == "F mixed" and "Mixed domains" in r[2] for r in review)), (True, True))
    check("KiCad -> Electronics", dom["G kicad"], "Electronics")
    check("one matching name out of four is not enough evidence", dom["H few names"], "Unclassified")
    check("tools outrank names: 12 SolidWorks parts + 'floor plan' drawing names -> Mechanical, disagreement noted",
          (dom["I tools vs names"], any(r[0] == "I tools vs names" and "names point to Architecture" in r[2] for r in review)), ("Mechanical", True))
    check("every project has a domain line in Review when it is not High / certain", any(r[0] == "D nothing" and r[1] == "domain" for r in review), True)
    soft = sf
    check("general-purpose tools carry no domain of their own", [soft[k]["domain"] for k in ("AutoCAD", "Rhino", "FreeCAD")], ["", "", ""])
sys.exit(1 if any(bad) else 0)
