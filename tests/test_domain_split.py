"""Split by domain: one workbook per domain with only that domain's projects; nothing lost, nothing duplicated; By Domain summary adds up.
Run: python tests/test_domain_split.py"""
import csv, os, subprocess, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py")
if not os.path.exists(SCRIPT):
    print("SKIP - compiler not found next to this package"); sys.exit(0)
try:
    import openpyxl
except ImportError:
    print("SKIP - openpyxl not installed"); sys.exit(0)
bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

def profile_zip(path, rows):
    hdr = "project,file_no,ext,size_bytes,modified_utc,depth,header_signature,dir_id,asm_name,rel_path,sheets,sheets_conf,sheets_status\r\n"
    projects = {}
    for r in rows:
        projects[r.split(",")[0]] = projects.get(r.split(",")[0], 0) + 1
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Files.csv", "﻿" + hdr + "\r\n".join(rows) + "\r\n")
        zf.writestr("Run_info.csv", "key,value\r\nfolder_name,x\r\n")
        zf.writestr("Projects.csv", "project,file_count,total_bytes,distinct_extensions,first_modified_utc,last_modified_utc\r\n" +
                    "".join(f"{p},{n},1000,3,,\r\n" for p, n in projects.items()))

def run(z, out, *flags):
    r = subprocess.run([sys.executable, SCRIPT, z, "--out", out, *flags], capture_output=True, text=True)
    return r

def tab(path, name):
    ws = openpyxl.load_workbook(path, read_only=True)[name]
    return list(ws.iter_rows(values_only=True))

with tempfile.TemporaryDirectory() as t:
    rows, n = [], 0
    def add(proj, ext, name, sheets="", conf=""):
        global n
        n += 1
        rows.append(f"{proj},{n},{ext},{100 + n},2024-01-01T00:00:00Z,1,BINARY,d{proj[0]},,{name},{sheets},{conf},{'ok' if sheets != '' else ''}")
    for i in range(6): add("MechA", "sldprt", f"part{i}.sldprt")
    add("MechA", "sldasm", "top.sldasm"); add("MechA", "slddrw", "top.slddrw")
    for i in range(5): add("ArchB", "skp", f"model{i}.skp")
    for i in range(4): add("ArchB", "dwg", f"plan{i}.dwg", sheets=str(i + 1), conf="95")
    for i in range(3): add("ArchC", "rvt", f"house{i}.rvt")
    for i in range(3): add("PlainD", "dwg", f"d{i}.dwg", sheets="2", conf="95")      # only AutoCAD: no domain of its own
    z = os.path.join(t, "p.zip"); profile_zip(z, rows)
    out = os.path.join(t, "o"); r = run(z, out)
    check("run succeeds and says it split", (r.returncode, "split by domain" in r.stdout), (0, True))
    files = sorted(os.listdir(os.path.join(out, "by-domain")))
    check("one workbook per domain", files, ["compiled_Architecture.xlsx", "compiled_Mechanical.xlsx", "compiled_Unclassified.xlsx"])
    main_pc = {r[0]: r for r in tab(os.path.join(out, "compiled.xlsx"), "Project Compiled")[1:]}
    seen = {}
    for f in files:
        d = f[len("compiled_"):-5]
        pcs = tab(os.path.join(out, "by-domain", f), "Project Compiled")[1:]
        for p in pcs:
            seen[p[0]] = d
        check(f"{d}: Project Compiled holds only its own domain's projects", {p[2] for p in pcs}, {d})
    check("every project is in exactly one domain workbook", sorted(seen), sorted(main_pc))
    check("domains as expected", seen, {"MechA": "Mechanical", "ArchB": "Architecture", "ArchC": "Architecture", "PlainD": "Unclassified"})
    # nothing lost or duplicated in the big tabs
    main_dr = tab(os.path.join(out, "compiled.xlsx"), "Drawing Breakdown")[1:]
    split_dr = [r for f in files for r in tab(os.path.join(out, "by-domain", f), "Drawing Breakdown")[1:]]
    check("Drawing Breakdown rows: the domain workbooks together = the main one", (len(split_dr), sorted(map(str, split_dr)) == sorted(map(str, main_dr))), (len(main_dr), True))
    arch_dr = tab(os.path.join(out, "by-domain", "compiled_Architecture.xlsx"), "Drawing Breakdown")[1:]
    check("Architecture drawing rows are ArchB's 4 DWGs only", sorted({r[0] for r in arch_dr}), ["ArchB"])
    main_as = tab(os.path.join(out, "compiled.xlsx"), "Assembly Breakdown")[1:]
    split_as = [r for f in files for r in tab(os.path.join(out, "by-domain", f), "Assembly Breakdown")[1:]]
    check("Assembly Breakdown rows: the domain workbooks together = the main one", sorted(map(str, split_as)), sorted(map(str, main_as)))
    # By Domain summary
    bd = tab(os.path.join(out, "compiled.xlsx"), "By Domain")
    check("By Domain tab heading", bd[0][:3], ("Domain", "Projects", "Files"))
    byd = {r[0]: r for r in bd[1:]}
    check("By Domain: projects and files per domain", {d: (r[1], r[2]) for d, r in byd.items()}, {"Architecture": (2, 12), "Mechanical": (1, 8), "Unclassified": (1, 3)})
    check("By Domain: drawing sheets (Architecture 1+2+3+4 = 10; Unclassified 3x2 = 6; the SolidWorks drawing has no sheet count)", {d: r[9] for d, r in byd.items()},
          {"Architecture": 10, "Mechanical": 0, "Unclassified": 6})
    csvd = list(csv.reader(open(os.path.join(out, "By_Domain.csv"), encoding="utf-8-sig")))
    check("By_Domain.csv has the same domains", sorted(r[0] for r in csvd[1:]), ["Architecture", "Mechanical", "Unclassified"])
    # switched off
    out2 = os.path.join(t, "o2"); r2 = run(z, out2, "--no-split-by-domain")
    check("--no-split-by-domain writes no by-domain folder", (r2.returncode, os.path.exists(os.path.join(out2, "by-domain"))), (0, False))
    # one domain only -> nothing to split
    z1 = os.path.join(t, "one.zip"); profile_zip(z1, [r for r in rows if r.startswith("ArchB") or r.startswith("ArchC")])
    out3 = os.path.join(t, "o3"); r3 = run(z1, out3)
    check("a single domain is not split (and says so)", (r3.returncode, os.path.exists(os.path.join(out3, "by-domain")), "skipped" in r3.stdout), (0, False, True))
sys.exit(1 if any(bad) else 0)
