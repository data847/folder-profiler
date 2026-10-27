"""The three domain sheets of the output template - Mechanical, Architecture, Electronics - one row per project, columns exactly as the template.

Every value is computed from what the profile holds (file extension, size, name / path, folder id, the compiler's own counts) with the rule written
in the template's cell comments. Where a comment gives no definition, or the data cannot answer it, the cell is left EMPTY and the "Metric Notes" tab
says so - nothing is guessed. Every rule that needed a decision (a threshold, a pattern list, which of two readings) is listed there too.

    collect(...)       facts of ONE project (called while the project is compiled; small, so thousands of projects fit in memory)
    build_sheets(...)  -> {sheet name: [rows]} once every project is known (vendor-level columns need all of them)
    NOTES              [(sheet, column, status, rule)] for the "Metric Notes" tab
"""
import collections
import re

MECH_HEADINGS = [
    'Project Name', 'Native CAD Software', 'Part Files',
    'Assembly Files', 'Parts / Assembly Ratio', 'Native 2D Drawings',
    'PDF Drawings', 'Excel Files', 'Neutral 3D Files',
    '2D Files (file types)', '3D Files (file types)', 'Total Files',
    'Duplicate %', 'Design History Present Assembly', 'BOM Present (Y/N)',
    'Share of Vendor Model Files %', 'Native 3D Files (Parts + Assemblies)', 'Native Share of 3D %',
    'Complete Project (Y/N)', 'Neutral Export Assembly Ratio ', 'Drawings Linked to Assembly %',
    'Stub Parts <10 KB %', 'Backup / Old-Revision Files', 'Assemblies >= 10 (Y/N)',
    '1-9 Assemblies and >= 10 Parts per Assembly (Y/N)',
]
ARCH_HEADINGS = [
    'Project Name', 'Native CAD Software', '3D File',
    'Native 2D Drawings', 'PDF Drawings', '2D Files (file types)',
    '3D Files (file types)', 'Total Files', 'Duplicate %',
    'Revit Project Files (RVT)', 'IFC Files', 'Revit Family Files (RFA)',
    'SketchUp Files (SKP) <2 MB', 'SketchUp Files (SKP) > 10MB', 'Rhino / 3ds Max / ArchiCAD Files',
    'DWG Files', 'DXF / DGN Files', 'Render / Image Files',
    'Share of Vendor Model Files %', 'Full-Set Project (Y/N)', 'Components % of 3D Model Files',
    'Repeated Library Copy Flag (Y/N)', 'Image Share %', 'Drawing Sheets (Count)',
]
ELEC_HEADINGS = [
    'Project Name', 'Domain', 'Native EDA Software',
    'Schematic Files', 'Layout (PCB) Files', 'Gerber Files',
    'Drill Files', 'Pick-and-Place Files', 'BOM Files',
    'Netlist Files', 'Simulation Files', 'Library Files',
    'Native 2D Drawings', 'PDF Drawings', 'Neutral 3D Files',
    '2D Files (file types)', 'EDA Files (file types)', 'Total Files',
    'Duplicate %', 'Share of Vendor EDA Files %', 'Labelled Electronics but No EDA Files (Y/N)',
    'Has Schematic and Layout (Y/N)', 'Has Gerber and Drill (Y/N)', 'Has Netlist or Simulation (Y/N)',
    'Library Share of EDA Files %', 'Revisions Visible (Count)',
]
SHEETS = {"Mechanical": MECH_HEADINGS, "Architecture": ARCH_HEADINGS, "Electronics": ELEC_HEADINGS}

# ---- thresholds and patterns (each is listed in NOTES)
KB = 1024
STUB_BYTES = 10 * KB                                   # "Stub Parts <10 KB": 10 KB = 10,240 bytes
IMAGE_BYTES = 500 * KB                                 # "Render / Image Files ... > 500KB"
SKP_SMALL, SKP_BIG = 2 * 1024 * KB, 10 * 1024 * KB     # SKP < 2 MB / > 10 MB
LARGE_PROJECT_ASSEMBLIES = 25                          # the comment: "Y if Assembly Files is 25 or more" (the heading says 10)
SMALL_PROJECT_MAX_ASSEMBLIES, PARTS_PER_ASSEMBLY = 9, 10
FULL_SET_DWG, FULL_SET_PDF = 50, 50
REPEAT_MIN_FILES, REPEAT_MIN_PROJECTS = 50, 3          # "same file count for an extension repeats in 3+ projects (for example 1,002 RFA)"
ARCH_MODEL_EXTS = ("rvt", "rfa", "skp", "3dm", "max", "pln")           # the template's 3D model files of an architecture project
EXCEL_EXTS = ("xls", "xlsx", "xlsm", "xlsb")
BOM_EXTS = ("csv", "xlsx", "xls")
BOM_RX = re.compile(r"\bbom\b|bill[ _-]?of[ _-]?materials?", re.I)
OLD_FOLDERS = {"oldversions", "old versions", "old_versions"}
OLD_COPY_RX = re.compile(r"(?:[ _-](?:old|backup|bak|copy)(?:[ _-]|$))|(?:^|[ _-])copy of[ _-]|\(\d+\)$|[ _-]rev[ _-]?[a-z0-9]{1,3}$", re.I)
REV_RX = re.compile(r"(?<![a-z0-9])rev(?:ision)?[ _.-]?([a-z]|\d{1,3})(?![a-z0-9])|(?<![a-z0-9])v(\d{1,3})(?![a-z0-9])", re.I)
MECH_STUB_DOMAIN = "Mechanical"


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else ""


def _yn(v):
    return "Y" if v else "N"


def collect(proj, rows, counted, n, pdf_drawings, sw_names, t2d, t3d, sheets_total, domain, drawing_rows, soft, eda_roles):
    """The facts of one project. rows = every file, counted = the files that count (duplicates left out under --unique), n = class counts of
    `counted`, drawing_rows = the project's Drawing Breakdown rows."""
    f = {"proj": proj, "domain": domain, "sw": sw_names, "t2d": t2d, "t3d": t3d, "n": dict(n), "pdf_drawings": pdf_drawings, "sheets": sheets_total,
         "files_total": len(rows), "dup_files": sum(1 for r in rows if r["dup"])}
    ext = collections.Counter(r["ext"] for r in counted)
    f["ext"] = ext
    f["eda_types"] = ", ".join(e.upper() for e, _ in collections.Counter(r["ext"] for r in counted if r["cls"] == "EDA").most_common()) or "None"
    skp_small = skp_big = images = stub = mech_parts = backups = excel = 0
    revs, bom_rows, eda_sw, pretty = set(), [], collections.Counter(), set()
    roles = collections.Counter()
    design_dirs = set()
    for r in counted:
        e, size, cls = r["ext"], r["size"], r["cls"]
        if e == "skp":
            skp_small += size < SKP_SMALL
            skp_big += size > SKP_BIG
        if cls == "IMG" and size > IMAGE_BYTES:
            images += 1
        if e in EXCEL_EXTS:
            excel += 1
        if cls == "PART":                                             # stub parts: only parts of MECHANICAL-native CAD (see NOTES)
            s = soft.get(r["sw"]) if r["sw"] else None
            if s and s.get("domain") == MECH_STUB_DOMAIN and (s.get("role") or "specific") != "general":
                mech_parts += 1
                stub += size < STUB_BYTES
        path = r["rel"] or ""
        name = path.rsplit("/", 1)[-1] if path else (r["name"] or "")
        if path or name:
            comps = [c.lower() for c in path.split("/")[:-1]]
            stem = name.rsplit(".", 1)[0] if "." in name else name
            if e == "bak" or any(c in OLD_FOLDERS for c in comps) or OLD_COPY_RX.search(stem):
                backups += 1
            for m in REV_RX.finditer(path):
                revs.add(("rev", m.group(1).lower()) if m.group(1) else ("v", int(m.group(2))))
            for i, c in enumerate(comps):
                if c.endswith(".pretty"):
                    pretty.add("/".join(comps[:i + 1]))
        if cls == "EDA":
            role = eda_roles.get(e, "")
            if role:
                roles[role] += 1
            if r["sw"]:
                eda_sw[r["sw"]] += 1
            if role in ("schematic", "layout") and r["dir"]:
                design_dirs.add(r["dir"])
        elif e in BOM_EXTS and name and BOM_RX.search(name):
            bom_rows.append(r["dir"])
    f.update(skp_small=skp_small, skp_big=skp_big, images=images, excel=excel, stub=stub, mech_parts=mech_parts, backups=backups,
             revisions=len(revs), roles=dict(roles), eda_sw=eda_sw, pretty=len(pretty),
             bom_files=sum(1 for d in bom_rows if d and d in design_dirs), eda_total=n.get("EDA", 0))
    nat = [d for d in drawing_rows if d[2] != "PDF"]                   # Drawing Breakdown rows of NATIVE drawings (column 3 = format)
    f["native_dr"], f["native_dr_linked"] = len(nat), sum(1 for d in nat if d[7])
    return f


def build_sheets(facts, soft):
    """-> {sheet: [row, ...]} - a project is listed in the sheet of its own domain; vendor-level columns use all projects of that sheet."""
    by = {"Mechanical": [], "Architecture": [], "Electronics": []}
    for p in sorted(facts, key=str.lower):
        if facts[p]["domain"] in by:
            by[facts[p]["domain"]].append(facts[p])
    # repeated library copies: an (extension, count) pair of 50+ files that appears in 3+ projects of the WHOLE profile
    pairs = collections.defaultdict(set)
    for p, f in facts.items():
        for e, k in f["ext"].items():
            if k >= REPEAT_MIN_FILES:
                pairs[(e, k)].add(p)
    repeated = {p for ps in pairs.values() if len(ps) >= REPEAT_MIN_PROJECTS for p in ps}
    out = {}
    mech = by["Mechanical"]
    tot_mech = sum(f["n"].get("PART", 0) + f["n"].get("ASSEMBLY", 0) for f in mech)
    rows = []
    for f in mech:
        n = f["n"]; C, D, F, I = n.get("PART", 0), n.get("ASSEMBLY", 0), n.get("DRAWING", 0), n.get("NEUTRAL3D", 0)
        rows.append([f["proj"], f["sw"], C, D, round(C / D, 2) if D else "", F, f["pdf_drawings"], f["excel"], I, f["t2d"], f["t3d"], C + D + F,
                     _pct(f["dup_files"], f["files_total"]), "", "", _pct(C + D, tot_mech), C + D, _pct(C + D, C + D + I),
                     _yn(C > 0 and D > 0 and F > 0), "", _pct(f["native_dr_linked"], f["native_dr"]), _pct(f["stub"], f["mech_parts"]), f["backups"],
                     _yn(D >= LARGE_PROJECT_ASSEMBLIES), _yn(1 <= D <= SMALL_PROJECT_MAX_ASSEMBLIES and C >= PARTS_PER_ASSEMBLY * D)])
    out["Mechanical"] = rows
    arch = by["Architecture"]
    model = lambda f: sum(f["ext"].get(e, 0) for e in ARCH_MODEL_EXTS)
    tot_model = sum(model(f) for f in arch)
    rows = []
    for f in arch:
        x, n = f["ext"], f["n"]
        C = model(f)
        rows.append([f["proj"], f["sw"], C, n.get("DRAWING", 0), f["pdf_drawings"], f["t2d"], f["t3d"], f["files_total"], _pct(f["dup_files"], f["files_total"]),
                     x.get("rvt", 0), x.get("ifc", 0), x.get("rfa", 0), f["skp_small"], f["skp_big"], x.get("3dm", 0) + x.get("max", 0) + x.get("pln", 0),
                     x.get("dwg", 0), x.get("dxf", 0) + x.get("dgn", 0), f["images"], _pct(C, tot_model),
                     _yn((x.get("rvt", 0) >= 1 or f["skp_big"] >= 1 or x.get("3dm", 0) >= 1 or x.get("max", 0) >= 1)
                         and x.get("dwg", 0) >= FULL_SET_DWG and x.get("pdf", 0) >= FULL_SET_PDF),
                     _pct(f["skp_small"] + x.get("rfa", 0), C), _yn(f["proj"] in repeated), _pct(f["images"], f["files_total"]), f["sheets"]])
    out["Architecture"] = rows
    elec = by["Electronics"]
    eda_all = lambda f: f["eda_total"]
    tot_eda = sum(eda_all(f) for f in elec)
    rows = []
    for f in elec:
        r_, n = f["roles"], f["n"]
        lib = r_.get("library", 0) + f["pretty"]
        eda = eda_all(f)
        sw = ", ".join(soft[t]["display"] if t in soft else t for t, _ in f["eda_sw"].most_common()) or ("Unknown" if eda else "")
        rows.append([f["proj"], f["domain"], sw, r_.get("schematic", 0), r_.get("layout", 0), r_.get("gerber", 0), r_.get("drill", 0), r_.get("pickplace", 0),
                     f["bom_files"], r_.get("netlist", 0), r_.get("simulation", 0), lib, n.get("DRAWING", 0), f["pdf_drawings"], n.get("NEUTRAL3D", 0), f["t2d"],
                     f["eda_types"], f["files_total"], _pct(f["dup_files"], f["files_total"]), _pct(eda, tot_eda), "",
                     _yn(r_.get("schematic", 0) > 0 and r_.get("layout", 0) > 0), _yn(r_.get("gerber", 0) > 0 and r_.get("drill", 0) > 0),
                     _yn(r_.get("netlist", 0) > 0 or r_.get("simulation", 0) > 0), _pct(lib, eda + f["pretty"]), f["revisions"]])
    out["Electronics"] = rows
    return out


# ---------------------------------------------------------------------------------------------------- the Metric Notes tab
def _n(sheet, col, status, rule):
    return (sheet, col, status, rule)


CALC, ASSUME, NONE = "calculated", "calculated - a decision was needed (see rule)", "NOT calculated"
M, A, E = "Mechanical", "Architecture", "Electronics"
NOTES = [
    _n(M, "Project Name", CALC, "The project (folder) name, or the sub-project name when a folder was split by root assembly."),
    _n(M, "Native CAD Software", CALC, "Tools of the project's native CAD files, judged from file content as well as extension (Project Compiled column)."),
    _n(M, "Part Files", CALC, "Files classed PART (extensions.csv) that count (duplicates left out only with --unique)."),
    _n(M, "Assembly Files", CALC, "Files classed ASSEMBLY."),
    _n(M, "Parts / Assembly Ratio", CALC, "Part Files / Assembly Files, 2 decimals; empty when there are no assemblies."),
    _n(M, "Native 2D Drawings", CALC, "Files classed DRAWING (electronics design files are no longer counted here)."),
    _n(M, "PDF Drawings", CALC, "PDFs counted as drawings by file name (needs a profile with names)."),
    _n(M, "Excel Files", CALC, "Files with extension xls, xlsx, xlsm or xlsb."),
    _n(M, "Neutral 3D Files", CALC, "Files classed NEUTRAL3D (STEP, IGES, STL, IFC, ...)."),
    _n(M, "2D Files (file types)", CALC, "Extensions of the 2D files, most frequent first."),
    _n(M, "3D Files (file types)", CALC, "Extensions of the part, assembly and neutral 3D files, most frequent first."),
    _n(M, "Total Files", ASSUME, "Part Files + Assembly Files + Native 2D Drawings, as the cell comment says. NOTE: Project Stats 'files_total' is ALL files - the template heading could mean either; Duplicate % uses all files."),
    _n(M, "Duplicate %", ASSUME, "Duplicate files / all files of the project x 100. A duplicate is judged by type + size + modified date (no hashing): 97% of what it flags is a duplicate and it finds 94% of the true ones, so this % is approximate."),
    _n(M, "Design History Present Assembly", NONE, "The template gives no definition. If it means feature history inside the CAD file, the profile does not hold it (files are not opened that deep)."),
    _n(M, "BOM Present (Y/N)", NONE, "The template gives no definition (a BOM by file name would be found in only a handful of files on real data; a BOM inside the CAD assembly is not extracted)."),
    _n(M, "Share of Vendor Model Files %", ASSUME, "(Parts + Assemblies) of the project / (Parts + Assemblies) of ALL projects in this sheet (the vendor = the projects compiled together) x 100."),
    _n(M, "Native 3D Files (Parts + Assemblies)", CALC, "Part Files + Assembly Files."),
    _n(M, "Native Share of 3D %", CALC, "(Parts + Assemblies) / (Parts + Assemblies + Neutral 3D Files) x 100; empty when all three are 0."),
    _n(M, "Complete Project (Y/N)", CALC, "Y when Part Files, Assembly Files and Native 2D Drawings are all above 0."),
    _n(M, "Neutral Export Assembly Ratio ", NONE, "The template gives no definition."),
    _n(M, "Drawings Linked to Assembly %", ASSUME, "Native drawings (not PDFs) with an assembly in the Drawing Breakdown / all native drawings x 100. The assembly may come from a file reference, the name or the folder (the method is in the Drawing Breakdown); the template does not say which count."),
    _n(M, "Stub Parts <10 KB %", ASSUME, "Parts under 10,240 bytes / parts, counting ONLY parts of mechanical-native CAD (a specific tool whose domain is Mechanical: SolidWorks, Inventor, Creo, NX, Solid Edge, CATIA ...). SketchUp, Revit and Rhino parts are left out: on real data every part under 10 KB was a small SketchUp component, not a stub. A .prt of unknown software is left out. Empty when the project has no such parts."),
    _n(M, "Backup / Old-Revision Files", ASSUME, "Files that are .bak, OR sit in a folder named OldVersions / Old Versions, OR whose name contains old / backup / bak / copy as a separate word after a space, underscore or hyphen, starts with 'copy of', ends with '(1)' (any number) or ends with '_revA' style. Needs a profile with names. The pattern list is a decision - the count changes with it."),
    _n(M, "Assemblies >= 10 (Y/N)", ASSUME, "The heading says 10 but the cell comment says 'Y if Assembly Files is 25 or more' - the comment's 25 is used."),
    _n(M, "1-9 Assemblies and >= 10 Parts per Assembly (Y/N)", CALC, "Y when Assembly Files is 1-9 and Part Files >= 10 x Assembly Files."),
    _n(A, "Project Name", CALC, "As above."),
    _n(A, "Native CAD Software", CALC, "As above."),
    _n(A, "3D File", ASSUME, "No definition in the template. Used: files with extension rvt, rfa, skp, 3dm, max or pln (the native 3D models of an architecture project). 'Components %' and the vendor share depend on it."),
    _n(A, "Native 2D Drawings", CALC, "Files classed DRAWING."),
    _n(A, "PDF Drawings", CALC, "As above."),
    _n(A, "2D Files (file types)", CALC, "As above."),
    _n(A, "3D Files (file types)", CALC, "As above."),
    _n(A, "Total Files", ASSUME, "ALL files of the project (Project Stats 'files_total'); the template gives no definition here."),
    _n(A, "Duplicate %", ASSUME, "As in the Mechanical sheet (approximate)."),
    _n(A, "Revit Project Files (RVT)", CALC, "Count of .rvt."),
    _n(A, "IFC Files", CALC, "Count of .ifc."),
    _n(A, "Revit Family Files (RFA)", CALC, "Count of .rfa."),
    _n(A, "SketchUp Files (SKP) <2 MB", ASSUME, "Count of .skp under 2 MB (2,097,152 bytes)."),
    _n(A, "SketchUp Files (SKP) > 10MB", ASSUME, "Count of .skp over 10 MB (10,485,760 bytes)."),
    _n(A, "Rhino / 3ds Max / ArchiCAD Files", CALC, "Count of .3dm + .max + .pln."),
    _n(A, "DWG Files", CALC, "Count of .dwg."),
    _n(A, "DXF / DGN Files", CALC, "Count of .dxf + .dgn."),
    _n(A, "Render / Image Files", ASSUME, "Files classed IMG (photos and renders; video is no longer in this class) over 500 KB (512,000 bytes), as the cell comment says."),
    _n(A, "Share of Vendor Model Files %", ASSUME, "'3D File' of the project / '3D File' of all projects in this sheet x 100."),
    _n(A, "Full-Set Project (Y/N)", ASSUME, "Two versions of the rule are in the comments. Used the LATER one: (RVT, or SKP > 10 MB, or 3dm, or 3ds Max) AND 50+ DWG AND 50+ PDF files. The earlier text said '1+ RVT or IFC'."),
    _n(A, "Components % of 3D Model Files", ASSUME, "(SKP under 2 MB + RFA) / '3D File' x 100 (the later comment's definition); empty when there are no 3D files."),
    _n(A, "Repeated Library Copy Flag (Y/N)", ASSUME, "Y when the project has an extension with the same file count (50 or more files) as at least 2 other projects in the profile. The 50-file minimum is a decision."),
    _n(A, "Image Share %", CALC, "Render / Image Files / Total Files x 100."),
    _n(A, "Drawing Sheets (Count)", ASSUME, "Sum of sheets of the project's AutoCAD drawings (Project Stats 'drawing_sheets'). It INCLUDES assumed 1s for drawings that could not be read or have no layout tab (the Sheets Basis column of the Drawing Breakdown separates them). PDF page counts are not read."),
    _n(E, "Project Name", CALC, "As above."),
    _n(E, "Domain", CALC, "The project's detected domain (specific tools, else names)."),
    _n(E, "Native EDA Software", CALC, "Tools of the project's EDA files (mapping + file content for Eagle / KiCad). 'Unknown' when the extensions are shared by several programs."),
    _n(E, "Schematic Files", CALC, "Files classed EDA with role schematic (eda_roles.csv: kicad_sch, sch, schdoc, dsn, opj)."),
    _n(E, "Layout (PCB) Files", CALC, "Role layout (kicad_pcb, brd, pcbdoc, pcb, prjpcb)."),
    _n(E, "Gerber Files", CALC, "Role gerber (gbr, gtl, gbl, gts, gbs, gto, gbo, ger)."),
    _n(E, "Drill Files", CALC, "Role drill (drl, xln)."),
    _n(E, "Pick-and-Place Files", CALC, "Role pickplace (pos)."),
    _n(E, "BOM Files", ASSUME, "csv / xlsx / xls files whose NAME says bom / bill of materials and that sit in the same folder as a schematic or layout file. Needs names."),
    _n(E, "Netlist Files", CALC, "Role netlist (net, cir, sp, cdl)."),
    _n(E, "Simulation Files", CALC, "Role simulation (asc). The template says 'and similar' - add more extensions to eda_roles.csv."),
    _n(E, "Library Files", CALC, "Role library (lib, schlib, pcblib, olb, bxl) + the number of folders named *.pretty (KiCad footprint libraries). Needs names for the folders."),
    _n(E, "Native 2D Drawings", CALC, "Files classed DRAWING (schematics, layouts and gerbers are EDA files now, not drawings)."),
    _n(E, "PDF Drawings", CALC, "As above."),
    _n(E, "Neutral 3D Files", CALC, "As above."),
    _n(E, "2D Files (file types)", CALC, "As above."),
    _n(E, "EDA Files (file types)", CALC, "Extensions of the EDA files, most frequent first."),
    _n(E, "Total Files", ASSUME, "ALL files of the project."),
    _n(E, "Duplicate %", ASSUME, "As in the Mechanical sheet (approximate)."),
    _n(E, "Share of Vendor EDA Files %", ASSUME, "EDA files of the project / EDA files of all projects in this sheet x 100."),
    _n(E, "Labelled Electronics but No EDA Files (Y/N)", NONE, "Needs the vendor's OWN domain label for each project, which is not in the profile; the detected domain would make the test circular."),
    _n(E, "Has Schematic and Layout (Y/N)", CALC, "Y when Schematic Files and Layout Files are both above 0."),
    _n(E, "Has Gerber and Drill (Y/N)", CALC, "Y when Gerber Files and Drill Files are both above 0."),
    _n(E, "Has Netlist or Simulation (Y/N)", CALC, "Y when Netlist Files or Simulation Files is above 0."),
    _n(E, "Library Share of EDA Files %", ASSUME, "Library Files / (EDA files + .pretty folders) x 100."),
    _n(E, "Revisions Visible (Count)", ASSUME, "Number of DIFFERENT revision marks in folder and file names: 'Rev A', 'Rev_B', 'revision 2', 'v2' ... (each distinct mark counts once). Needs names."),
]
