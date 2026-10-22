"""End to end: synthetic project tree -> profiler zip -> compiler -> expected sheet rows.  Run: python tests/test_end_to_end.py"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp
try:
    import compile_profiles as cp
except ImportError:  # the compiler skill lives in the folder-profiler repo / skill zip, not in the client package
    print("SKIP - compile_profiles not found next to this package (end-to-end test needs the skill)")
    sys.exit(0)

def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(body if isinstance(body, bytes) else body.encode())

STEP = "ISO-10303-21;\nHEADER;\nFILE_NAME('a','2026',(''),(''),'','FreeCAD','');\nFILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));\nENDSEC;\n"
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    # Mech: 3 parts (two byte-identical), 1 assembly, 1 slddrw + 1 dwg, 2 step, 2 pdf, 1 png, junk, 1 unmapped
    # identical files share size+date; different content gets a different size (duplicates are judged without hashing)
    for i, body in enumerate([b"a", b"a", b"bb"]): put(src, f"Mech/p{i}.sldprt", body)
    put(src, "Mech/sub/top.sldasm"); put(src, "Mech/d1.slddrw"); put(src, "Mech/d2.dwg", b"AC1027....")
    put(src, "Mech/m1.step", STEP); put(src, "Mech/m2.step", STEP + "\n"); put(src, "Mech/r1.pdf", b"%PDF-1"); put(src, "Mech/r2.pdf", b"%PDF-2")
    put(src, "Mech/pic.png", b"\x89PNG\r\n\x1a\n"); put(src, "Mech/.DS_Store"); put(src, "Mech/odd.weird", "?")
    # Electronics: only Altium docs.   Architecture: Revit models.
    put(src, "Elec/a.schdoc", "s"); put(src, "Elec/b.pcbdoc", "p")
    put(src, "Arch/house.rvt", "r"); put(src, "Arch/fam.rfa", "f")
    zip_path, nproj, nfiles, errors = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None)
    runs = cp.load_profiles([zip_path]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))

    def rows(**kw):
        c, _files, _stats, review, asm, _ev = cp.compile_runs(runs, ext_map, soft, kw.get("unique", False), kw.get("pdf", "none"),
                                                         kw.get("estimate", False))
        rows.asm = asm
        return {r[0]: r for r in c}, review

    bad = []
    def check(label, got, want):
        ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

    r, review = rows()
    # columns: name, desc, domain, software, parts, assemblies, 2d, pdf, neutral3d, 2d types, 3d types
    check("Mech counts (parts, asm, 2D, PDF, neutral)", tuple(r["Mech"][4:9]), (3, 1, 2, 0, 2))
    check("Mech domain / software", (r["Mech"][2], r["Mech"][3]), ("Mechanical", "SolidWorks, AutoCAD"))
    check("Mech 2D types / 3D types", (r["Mech"][9], r["Mech"][10]), ("SLDDRW, DWG", "SLDPRT, STEP, SLDASM"))
    check("Elec domain", r["Elec"][2], "Electronics"); check("Elec software", r["Elec"][3], "Altium")
    check("Arch domain / parts", (r["Arch"][2], r["Arch"][4]), ("Architecture", 2))
    import zipfile, csv, io
    info = {x["key"]: x["value"] for x in csv.DictReader(io.StringIO(zipfile.ZipFile(zip_path).read("Run_info.csv").decode("utf-8-sig")))}
    check("files counted (16 real files)", nfiles, 16)
    check("junk .DS_Store ignored, and reported", info["ignored_system_files"], "1")
    check("unmapped listed", any(x[1] == "unmapped extension" and ".weird" in x[2] for x in review), True)
    check("other tools note (FreeCAD from STEP)", any("FreeCAD (2)" in x[2] for x in review), True)
    r, _ = rows(unique=True); check("unique parts", r["Mech"][4], 2)
    r, _ = rows(pdf="all");  check("pdf all -> PDF Drawings and 2D types", (r["Mech"][7], r["Mech"][9]), (2, "PDF, SLDDRW, DWG"))   # types are ordered by frequency
    # estimate: rows must add up to project totals; placeholder names; no rows when the flag is off
    check("no assembly rows by default", len(rows()[0]) and len(rows.asm), 0)
    r, review = rows(estimate=True)
    mech = [a for a in rows.asm if a[0] == "Mech"]
    check("Mech estimate rows sum to project totals (parts, 2D, PDF, neutral)", tuple(sum(a[i] for a in mech) for i in (2, 3, 4, 5)), (3, 2, 0, 2))
    check("Mech row carries the real assembly name recorded by profiler 1.2", mech[0][1], "top.sldasm")
    check("projects without an assembly file get one summary row each (never an empty tab)",
          sorted((a[0], a[1]) for a in rows.asm if a[1].startswith("(no assembly")), [("Arch", "(no assembly file in this project)"), ("Elec", "(no assembly file in this project)")])
    check("Mech's rows are its real assembly", {a[0] for a in rows.asm if not a[1].startswith("(no assembly")}, {"Mech"})
    check("review says ESTIMATE", any("ESTIMATE" in x[2] for x in review), True)
    # uneven split: 7 parts over 3 assemblies -> 3,2,2 summing to 7
    check("split_evenly(7,3)", cp.split_evenly(7, 3), [3, 2, 2])
    check("split_evenly(0,4)", cp.split_evenly(0, 4), [0, 0, 0, 0])
sys.exit(1 if any(bad) else 0)
