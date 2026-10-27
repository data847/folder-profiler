"""The three domain sheets (Mechanical / Architecture / Electronics): every column checked against values worked out BY HAND for synthetic projects.
Run: python tests/test_domain_sheets.py     (TEMPLATE_XLSX=<path> also compares the headings with the real template)"""
import datetime, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
try:
    import compile_profiles as cp
    import domain_metrics as dm
except ImportError:
    print("SKIP - compiler not present"); sys.exit(0)
bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

KB, MB = 1024, 1024 * 1024
HDR = "project,file_no,ext,size_bytes,modified_utc,depth,header_signature,dir_id,asm_name,rel_path,sheets,sheets_conf,sheets_status\r\n"
clock = [0]
def stamp(same_as=None):
    if same_as is not None:
        return same_as
    clock[0] += 1
    return (datetime.datetime(2024, 1, 1) + datetime.timedelta(seconds=clock[0])).strftime("%Y-%m-%dT%H:%M:%SZ")

class P:
    """One synthetic project: add(path, size, ...) appends a file row."""
    def __init__(self, name): self.name, self.rows = name, []
    def add(self, path, size, n=1, sig="BINARY", dir="d1", sheets=None, mtime=None, asm=False):
        for i in range(n):
            p = path.format(i=i)
            ext = p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
            name = p.rsplit("/", 1)[-1]
            sh = ("", "", "") if sheets is None else (sheets, 95, "ok")
            self.rows.append(f'"{self.name}",{len(self.rows) + 1},{ext},{size},{mtime or stamp()},1,{sig},{dir},{name if asm else ""},"{p}",{sh[0]},{sh[1]},{sh[2]}')
        return self

def zipit(projects, path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Files.csv", "﻿" + HDR + "\r\n".join(r for p in projects for r in p.rows) + "\r\n")
        z.writestr("Run_info.csv", "key,value\r\nfolder_name,vendor\r\n")
        z.writestr("Projects.csv", "project,file_count,total_bytes,distinct_extensions,first_modified_utc,last_modified_utc\r\n" +
                   "".join(f'"{p.name}",{len(p.rows)},0,1,,\r\n' for p in projects))

# ------------------------------------------------------------------ Mechanical
M1 = P("M1")
M1.add("p00.sldprt", 5000).add("p01.sldprt", 5000).add("p02.sldprt", 5000)                      # 3 stubs (< 10,240 bytes)
for i in range(3, 9): M1.add(f"p{i:02d}.sldprt", 60000)
M1.add("p09 (1).sldprt", 60000).add("p10_copy.sldprt", 60000).add("OldVersions/p11.sldprt", 60000, dir="d_old")   # backups by name / folder
M1.add("p03.bak", 70000)                                                                         # backup by extension
M1.add("top.sldasm", 80000, asm=True).add("sub.sldasm", 80000, asm=True)
M1.add("top.slddrw", 90000).add("orphan.slddrw", 90000)
M1.add("p00.pdf", 30000).add("report.pdf", 30000).add("model.step", 40000, sig="STEP:AP214:other")
same = stamp(); M1.add("a.xlsx", 7000, mtime=same).add("c.xlsx", 7000, mtime=same).add("b.xls", 8000)    # c.xlsx = a duplicate of a.xlsx
M2 = P("M2")
for i in range(10): M2.add(f"u{i}.sldprt", 60000)
M2.add("unit.sldasm", 80000, asm=True).add("unit.slddrw", 90000)
# ------------------------------------------------------------------ Architecture
A1 = P("A1")
A1.add("m{i}.rvt", 50 * MB, 2).add("x.ifc", MB).add("f{i}.rfa", 300 * KB, 50)
A1.add("s_small{i}.skp", 100 * KB, 4).add("s_big{i}.skp", 12 * MB, 3).add("s_mid{i}.skp", 5 * MB, 2)
A1.add("r.3dm", 10 * MB).add("r.max", 7 * MB).add("r.pln", 2 * MB)
A1.add("d{i}.dwg", 3 * MB, 60, sig="DWG:AC1032", sheets=3).add("x{i}.dxf", MB, 2, sheets=3).add("g.dgn", MB)
A1.add("sheet{i}.pdf", 400 * KB, 55).add("ph{i}.jpg", 600 * KB, 10).add("small{i}.jpg", 100 * KB, 5).add("walk{i}.mp4", 5 * MB, 2)
A2 = P("A2")
A2.add("f{i}.rfa", 300 * KB, 50).add("s_small{i}.skp", 100 * KB, 5).add("s_big{i}.skp", 12 * MB, 20)
A2.add("d{i}.dwg", 3 * MB, 10, sig="DWG:AC1032", sheets=1).add("sheet{i}.pdf", 400 * KB, 10).add("ph{i}.jpg", 600 * KB, 20)
A3 = P("A3").add("f{i}.rfa", 300 * KB, 50).add("m.rvt", 50 * MB)
A4 = P("A4").add("f{i}.rfa", 300 * KB, 10).add("s_big{i}.skp", 12 * MB, 3).add("d{i}.dwg", 3 * MB, 5, sig="DWG:AC1032", sheets=2)
# ------------------------------------------------------------------ Electronics
E1 = P("E1")
E1.add("main.kicad_sch", 5000, sig="EDA:KiCad", dir="d_e").add("power.kicad_sch", 5000, sig="EDA:KiCad", dir="d_e").add("main.kicad_pcb", 9000, sig="EDA:KiCad", dir="d_e")
E1.add("g{i}.gbr", 3000, 3, dir="d_e").add("g.gtl", 3000, dir="d_e").add("d.drl", 800, dir="d_e").add("d.xln", 800, dir="d_e").add("p.pos", 700, dir="d_e")
E1.add("n.net", 600, dir="d_e").add("n.cir", 600, dir="d_e").add("sim.asc", 900, dir="d_e").add("parts.lib", 2000, dir="d_e").add("x.olb", 2000, dir="d_e")
E1.add("fp.pretty/a.kicad_mod", 500, dir="d_fp").add("fp.pretty/b.kicad_mod", 500, dir="d_fp")
E1.add("bom.csv", 1200, dir="d_e").add("bom_old.csv", 1200, dir="d_other")                         # only the first is beside the design
E1.add("board.step", 50000, sig="STEP:AP214:other").add("schematic.pdf", 70000).add("docs_v2.pdf", 70000)
E1.add("fab/Rev A/notes.txt", 100, dir="d_a").add("fab/Rev B/notes.txt", 100, dir="d_b")
E2 = P("E2").add("amp.sch", 4000, sig="EDA:Eagle").add("amp.brd", 6000, sig="EDA:Eagle")

with tempfile.TemporaryDirectory() as t:
    zp = os.path.join(t, "vendor.zip"); zipit([M1, M2, A1, A2, A3, A4, E1, E2], zp)
    em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, stats, review, asm, ev = cp.compile_runs(cp.load_profiles([zp]), em, sf, False, "auto", True, "folder")
    sheets = ev["domain_sheets"]
    def cell(sheet, proj, col):
        heads = dm.SHEETS[sheet]
        row = next(r for r in sheets[sheet] if r[0] == proj)
        return row[heads.index(col)]
    check("heading counts equal the template (25 / 24 / 26)", (len(dm.MECH_HEADINGS), len(dm.ARCH_HEADINGS), len(dm.ELEC_HEADINGS)), (25, 24, 26))
    tpl = os.environ.get("TEMPLATE_XLSX")
    if tpl:
        import openpyxl
        wb = openpyxl.load_workbook(tpl)
        check("headings equal the real template, character for character", [[c.value for c in wb[s][1]] for s in ("Mechanical", "Architecture", "Electronics")], [dm.MECH_HEADINGS, dm.ARCH_HEADINGS, dm.ELEC_HEADINGS])
    check("each project is in the sheet of its own domain", {s: [r[0] for r in rows] for s, rows in sheets.items()},
          {"Mechanical": ["M1", "M2"], "Architecture": ["A1", "A2", "A3", "A4"], "Electronics": ["E1", "E2"]})

    S = "Mechanical"
    want = {"Native CAD Software": "SolidWorks", "Part Files": 12, "Assembly Files": 2, "Parts / Assembly Ratio": 6.0, "Native 2D Drawings": 2, "PDF Drawings": 1, "Excel Files": 3,
            "Neutral 3D Files": 1, "Total Files": 16, "Duplicate %": 4.3, "Design History Present Assembly": "", "BOM Present (Y/N)": "", "Share of Vendor Model Files %": 56.0,
            "Native 3D Files (Parts + Assemblies)": 14, "Native Share of 3D %": 93.3, "Complete Project (Y/N)": "Y", "Neutral Export Assembly Ratio ": "",
            "Drawings Linked to Assembly %": 50.0, "Stub Parts <10 KB %": 25.0, "Backup / Old-Revision Files": 4, "Assemblies >= 10 (Y/N)": "N",
            "1-9 Assemblies and >= 10 Parts per Assembly (Y/N)": "N"}
    for col, v in want.items(): check(f"M1 {col}", cell(S, "M1", col), v)
    want = {"Part Files": 10, "Assembly Files": 1, "Parts / Assembly Ratio": 10.0, "Native 2D Drawings": 1, "Total Files": 12, "Duplicate %": 0.0, "Share of Vendor Model Files %": 44.0,
            "Native Share of 3D %": 100.0, "Complete Project (Y/N)": "Y", "Drawings Linked to Assembly %": 100.0, "Stub Parts <10 KB %": 0.0, "Backup / Old-Revision Files": 0,
            "1-9 Assemblies and >= 10 Parts per Assembly (Y/N)": "Y"}
    for col, v in want.items(): check(f"M2 {col}", cell(S, "M2", col), v)

    S = "Architecture"
    want = {"3D File": 64, "Native 2D Drawings": 63, "PDF Drawings": 55, "Total Files": 200, "Duplicate %": 0.0, "Revit Project Files (RVT)": 2, "IFC Files": 1, "Revit Family Files (RFA)": 50,
            "SketchUp Files (SKP) <2 MB": 4, "SketchUp Files (SKP) > 10MB": 3, "Rhino / 3ds Max / ArchiCAD Files": 3, "DWG Files": 60, "DXF / DGN Files": 3, "Render / Image Files": 10,
            "Share of Vendor Model Files %": 31.5, "Full-Set Project (Y/N)": "Y", "Components % of 3D Model Files": 84.4, "Repeated Library Copy Flag (Y/N)": "Y", "Image Share %": 5.0,
            "Drawing Sheets (Count)": 186}
    for col, v in want.items(): check(f"A1 {col}", cell(S, "A1", col), v)
    check("A1 video is not an image", cell(S, "A1", "Render / Image Files"), 10)
    check("A2 3D File / share / full-set (SKP>10MB but only 10 DWG)", (cell(S, "A2", "3D File"), cell(S, "A2", "Share of Vendor Model Files %"), cell(S, "A2", "Full-Set Project (Y/N)")), (75, 36.9, "N"))
    check("A2 components %, image share, sheets, repeated", (cell(S, "A2", "Components % of 3D Model Files"), cell(S, "A2", "Image Share %"), cell(S, "A2", "Drawing Sheets (Count)"), cell(S, "A2", "Repeated Library Copy Flag (Y/N)")), (73.3, 17.4, 10, "Y"))
    check("A3 (RVT, 1 model) components %, sheets empty when no drawings, repeated", (cell(S, "A3", "Components % of 3D Model Files"), cell(S, "A3", "Drawing Sheets (Count)"), cell(S, "A3", "Repeated Library Copy Flag (Y/N)")), (98.0, "", "Y"))
    check("A4 not flagged: only 10 RFA, below the 50-file minimum", cell(S, "A4", "Repeated Library Copy Flag (Y/N)"), "N")
    check("vendor shares add up to 100", round(sum(r[dm.ARCH_HEADINGS.index("Share of Vendor Model Files %")] for r in sheets[S]), 0), 100.0)

    S = "Electronics"
    want = {"Domain": "Electronics", "Native EDA Software": "KiCad, LTspice, OrCAD", "Schematic Files": 2, "Layout (PCB) Files": 1, "Gerber Files": 4, "Drill Files": 2, "Pick-and-Place Files": 1, "BOM Files": 1,
            "Netlist Files": 2, "Simulation Files": 1, "Library Files": 3, "Native 2D Drawings": 0, "PDF Drawings": 1, "Neutral 3D Files": 1, "Total Files": 24, "Duplicate %": 0.0,
            "Share of Vendor EDA Files %": 88.2, "Labelled Electronics but No EDA Files (Y/N)": "", "Has Schematic and Layout (Y/N)": "Y", "Has Gerber and Drill (Y/N)": "Y",
            "Has Netlist or Simulation (Y/N)": "Y", "Library Share of EDA Files %": 18.8, "Revisions Visible (Count)": 3}
    for col, v in want.items(): check(f"E1 {col}", cell(S, "E1", col), v)
    check("E1 EDA file types start with the most frequent (.gbr)", cell(S, "E1", "EDA Files (file types)").startswith("GBR"), True)
    want = {"Native EDA Software": "Eagle", "Schematic Files": 1, "Layout (PCB) Files": 1, "Gerber Files": 0, "Total Files": 2, "Share of Vendor EDA Files %": 11.8, "Has Schematic and Layout (Y/N)": "Y",
            "Has Gerber and Drill (Y/N)": "N", "Has Netlist or Simulation (Y/N)": "N", "Library Share of EDA Files %": 0.0, "Revisions Visible (Count)": 0}
    for col, v in want.items(): check(f"E2 {col} (Eagle recognised from file content)", cell(S, "E2", col), v)
    # EDA files are no longer 2D drawings in Project Compiled
    pc = {c[0]: c for c in compiled}
    check("Project Compiled: EDA files are not counted as Native 2D Drawings", (pc["E1"][6], pc["E2"][6]), (0, 0))
    # every sheet column has a note
    notes = {(n[0], n[1]) for n in dm.NOTES}
    check("Metric Notes has one entry for every column of every sheet", {(s, h) for s, hs in dm.SHEETS.items() for h in hs} <= notes, True)
    check("columns that cannot be calculated are blank in EVERY row (never guessed)", all(cell("Mechanical", p, c) == "" for p in ("M1", "M2") for c in ("Design History Present Assembly", "BOM Present (Y/N)", "Neutral Export Assembly Ratio ")) and all(cell("Electronics", p, "Labelled Electronics but No EDA Files (Y/N)") == "" for p in ("E1", "E2")), True)
sys.exit(1 if any(bad) else 0)
