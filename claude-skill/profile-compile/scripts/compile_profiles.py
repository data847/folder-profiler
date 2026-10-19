#!/usr/bin/env python3
"""Compile Folder Profiler zips into the "Projects Compiled" workbook format.

    python compile_profiles.py PROFILE.zip [MORE.zip | FOLDER_OF_ZIPS ...]
        [--unique] [--pdf-drawings none|all] [--estimate-assemblies] [--map-dir DIR] [--out DIR]

Input : profile_*.zip written by the Folder Profiler (Files.csv, Run_info.csv, ...).
Output: Project_Compiled.csv, File_Level.csv, Project_Stats.csv, Review.csv, and compiled.xlsx
        (needs `openpyxl`; skipped with a notice if it is not installed) with tabs
          "Project Compiled"    11 columns, exactly the template's headings
          "Assembly Breakdown"  template headings only - see Review (needs file names + assembly structure,
                                which the profiler deliberately does not collect) - or, with
                                --estimate-assemblies, ESTIMATED rows (see below)
          "File Level", "Project Stats", "Review"   supporting detail
Local only: no network, no credentials. Reads zips, writes files.

All classification lives in mapping/*.csv (next to this script) - edit those, not this file.
Counting: every file by default; --unique counts each content hash once per project.
--estimate-assemblies: a profile has no assembly file names and no part-to-assembly links, so this fills the
      Assembly Breakdown tab with an ESTIMATE: each project's parts / native 2D / PDF / neutral 3D totals are split
      evenly across its assembly files (remainders to the first rows), so the rows add up to the project totals.
      Names are placeholders ("EST_001.ASM") - never real assembly names. Projects with no assembly files get no rows.
PDFs: PDF Drawings is 0 by default (a metric-only profile cannot tell a drawing from a report);
      --pdf-drawings all counts every PDF as a drawing.
"""
import argparse
import collections
import csv
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # xlsx is a convenience; CSVs are always written
    Workbook = None

HERE = Path(__file__).resolve().parent

HEADINGS = ["Project Name", "Project Description", "Domain", "Native CAD Software", "Part Files",
            "Assembly Files", "Native 2D Drawings", "PDF Drawings", "Neutral 3D Files",
            "2D Files (file types)", "3D Files (file types)"]
ASSEMBLY_HEADINGS = ["Project Name", "Assembly File Name", "Part Files", "Native 2D Drawings",
                     "PDF Drawings", "Neutral 3D Files"]
FILE_HEADER = ["project", "ext", "class", "native_software", "domain", "size_bytes", "modified_utc", "sha256",
               "header_signature", "duplicate_of_earlier_file"]
STATS_HEADER = ["project", "files_total", "unique_hashes", "duplicate_files", "parts", "assemblies",
                "native_2d_drawings", "neutral_3d", "pdf", "simulation", "documents", "data", "images",
                "archive_other", "unmapped", "total_bytes", "counted_as"]
CLASSES = {"PART", "ASSEMBLY", "DRAWING", "NEUTRAL3D", "PDF", "SIM", "DOC", "DATA", "IMG", "ARCHIVE", "OTHER"}
NATIVE = {"PART", "ASSEMBLY", "DRAWING"}      # only these name the "Native CAD Software"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def read_csv(text_or_path):
    text = text_or_path.read_text("utf-8-sig") if isinstance(text_or_path, Path) else text_or_path
    return list(csv.DictReader(io.StringIO(text)))


def load_mapping(map_dir):
    ext_map = {r["ext"].strip().lower(): r for r in read_csv(Path(map_dir) / "extensions.csv")}
    bad = sorted(f".{e} -> {r['class']}" for e, r in ext_map.items() if r["class"] not in CLASSES)
    if bad:
        raise SystemExit("extensions.csv has an unknown class (allowed: " + ", ".join(sorted(CLASSES)) + "): "
                         + "; ".join(bad))
    soft = {r["tag"].strip(): r for r in read_csv(Path(map_dir) / "software_domain.csv")}
    return ext_map, soft


def tool_in_tag(tag):
    """Tool named by a profiler header tag (closed vocabulary), or ''. Used for review notes and as a
    domain fallback only - never for 'Native CAD Software'."""
    p = tag.split(":")
    if p[0] in ("STEP", "IFC") and len(p) == 3 and p[2] not in ("other", ""):
        return p[2]
    if p[0] == "IGES" and len(p) == 2 and p[1] not in ("other", ""):
        return p[1]
    if p[0] == "SIM" and len(p) == 2 and p[1] not in ("FRD", "INP"):
        return p[1]
    return ""


def load_profiles(paths):
    zips = []
    for p in paths:
        p = Path(p)
        zips += sorted(p.glob("*.zip")) if p.is_dir() else [p]
    runs = []
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            names = set(zf.namelist())
            if "Files.csv" not in names or "Run_info.csv" not in names:
                print(f"skip {z.name}: not a profiler zip", file=sys.stderr)
                continue
            info = {r["key"]: r["value"] for r in read_csv(zf.read("Run_info.csv").decode("utf-8-sig"))}
            files = read_csv(zf.read("Files.csv").decode("utf-8-sig"))
        runs.append({"zip": z.name, "info": info, "files": files})
    if not runs:
        raise SystemExit("No profiler zips found.")
    keys = collections.Counter((r["info"].get("folder_name"), p) for r in runs for p in {f["project"] for f in r["files"]})
    dup = sorted(f"{k[0]}/{k[1]}" for k, n in keys.items() if n > 1)
    if dup:  # same folder profiled twice would be silently double-counted
        raise SystemExit("Same project appears in more than one zip (would double-count): " + ", ".join(dup))
    return runs


def split_evenly(total, k):
    """total split into k integers differing by at most 1; the first (total % k) rows get the extra unit."""
    return [total // k + (1 if i < total % k else 0) for i in range(k)]


def compile_runs(runs, ext_map, soft, unique, pdf_mode, estimate=False):
    names = collections.Counter(p for r in runs for p in {f["project"] for f in r["files"]})
    per_project, unmapped = {}, collections.Counter()
    for run in runs:
        folder = run["info"].get("folder_name", run["zip"])
        for f in run["files"]:
            proj = f["project"] if names[f["project"]] == 1 else f"{folder}/{f['project']}"
            ext = f["ext"].lower()
            m = ext_map.get(ext)
            cls = m["class"] if m else "UNMAPPED"
            if not m:
                unmapped[(proj, ext)] += 1
            sw = (m["software"] if m else "") if cls in NATIVE else ""
            tool = tool_in_tag(f["header_signature"]) if cls in ("NEUTRAL3D", "SIM") else ""
            domain = (m["domain"] if m else "") or (soft[sw]["domain"] if sw in soft else "")
            P = per_project.setdefault(proj, {"hashes": set(), "rows": []})
            sha = f["sha256"]
            dup = sha in P["hashes"] and sha != EMPTY_SHA
            P["hashes"].add(sha)
            P["rows"].append(dict(project=proj, ext=ext, cls=cls, sw=sw, tool=tool, domain=domain,
                                  size=int(f["size_bytes"]), modified=f["modified_utc"], sha=sha,
                                  sig=f["header_signature"], dup=dup))

    compiled, file_rows, stats, review, assemblies = [], [], [], [], []
    for proj in sorted(per_project, key=str.lower):
        rows = per_project[proj]["rows"]
        file_rows += rows
        counted = [r for r in rows if not (unique and r["dup"])]
        n = collections.Counter(r["cls"] for r in counted)
        pdf_drawings = n["PDF"] if pdf_mode == "all" else 0

        def types(classes, extra=()):
            c = collections.Counter(r["ext"] for r in counted if r["cls"] in classes)
            for e, k in extra:
                if k:
                    c[e] += k
            return ", ".join(e.upper() for e, _ in c.most_common()) or "None"

        t2d = types({"DRAWING"}, [("pdf", pdf_drawings)])
        t3d = types({"PART", "ASSEMBLY", "NEUTRAL3D"})
        sw = collections.Counter(r["sw"] for r in counted if r["sw"])
        sw_names = ", ".join(soft[t]["display"] if t in soft else t for t, _ in sw.most_common()) or "Unknown"
        dom = collections.Counter(r["domain"] for r in counted if r["cls"] in NATIVE and r["domain"])
        if not dom:  # no native files: fall back to the tool named in neutral-format headers
            dom = collections.Counter(soft[r["tool"]]["domain"] for r in counted
                                      if r["cls"] == "NEUTRAL3D" and r["tool"] in soft)
        domain = dom.most_common(1)[0][0] if dom else "Unclassified"

        compiled.append([proj, "", domain, sw_names, n["PART"], n["ASSEMBLY"], n["DRAWING"], pdf_drawings,
                         n["NEUTRAL3D"], t2d, t3d])
        stats.append([proj, len(rows), len({r["sha"] for r in rows}), sum(r["dup"] for r in rows), n["PART"],
                      n["ASSEMBLY"], n["DRAWING"], n["NEUTRAL3D"], n["PDF"], n["SIM"], n["DOC"], n["DATA"],
                      n["IMG"], n["ARCHIVE"] + n["OTHER"], n["UNMAPPED"], sum(r["size"] for r in rows),
                      "unique hashes" if unique else "all files"])

        if domain == "Unclassified":
            review.append([proj, "domain", "No native or neutral model maps to a domain - set it by hand"])
        elif len(dom) > 1:
            review.append([proj, "domain", "Mixed domains: " + ", ".join(f"{d} ({k})" for d, k in dom.most_common())])
        if sw_names == "Unknown":
            review.append([proj, "software", "No native CAD file identifies the software (only neutral/other files)"])
        elif len(sw) > 1:
            review.append([proj, "software", "Several native tools: " + ", ".join(f"{t} ({k})" for t, k in sw.most_common())])
        tools = collections.Counter(r["tool"] for r in counted if r["tool"])
        if tools:
            review.append([proj, "other tools seen", "Named in STEP/IGES/IFC/simulation headers (not counted as native CAD "
                           "software): " + ", ".join(f"{t} ({k})" for t, k in tools.most_common())])
        if n["PDF"] and pdf_mode == "none":
            review.append([proj, "PDF drawings", f"{n['PDF']} PDF file(s) NOT counted as drawings (PDF Drawings = 0). "
                           "The profile cannot tell drawings from reports; re-run with --pdf-drawings all to count them"])
        if n["ASSEMBLY"] and estimate:
            exts = [e for e, k in collections.Counter(r["ext"] for r in counted if r["cls"] == "ASSEMBLY").most_common()
                    for _ in range(k)]
            cols = [split_evenly(v, n["ASSEMBLY"]) for v in (n["PART"], n["DRAWING"], pdf_drawings, n["NEUTRAL3D"])]
            for i, e in enumerate(exts):
                assemblies.append([proj, f"EST_{i + 1:03d}.{e.upper()}", cols[0][i], cols[1][i], cols[2][i], cols[3][i]])
            review.append([proj, "assembly breakdown", f"ESTIMATE: {n['PART']} parts, {n['DRAWING']} drawings, {pdf_drawings} PDFs and "
                           f"{n['NEUTRAL3D']} neutral files split evenly across {n['ASSEMBLY']} assemblies; names are placeholders "
                           "(EST_nnn), not real assembly names"])
        elif n["ASSEMBLY"]:
            review.append([proj, "assembly breakdown", f"{n['ASSEMBLY']} assembly file(s): Assembly Breakdown tab left empty "
                           "(needs assembly file names and which parts belong to each; --estimate-assemblies gives an even-split estimate)"])
        elif estimate and (n["PART"] or n["DRAWING"] or n["NEUTRAL3D"]):
            review.append([proj, "assembly breakdown", "no assembly files, so its parts/drawings are not attributed to any estimate row "
                           "(Assembly Breakdown rows will not sum to this project's totals)"])
    for (proj, ext), k in sorted(unmapped.items()):
        review.append([proj, "unmapped extension", f".{ext} x{k} - add it to extensions.csv"])
    return compiled, file_rows, stats, review, assemblies


def write_csvs(out, compiled, file_rows, stats, review, assemblies=()):
    def dump(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(header); w.writerows(rows)
    dump("Project_Compiled.csv", HEADINGS, compiled)
    dump("File_Level.csv", FILE_HEADER, [[r["project"], r["ext"], r["cls"], r["sw"], r["domain"], r["size"],
                                          r["modified"], r["sha"], r["sig"], "yes" if r["dup"] else ""] for r in file_rows])
    dump("Project_Stats.csv", STATS_HEADER, stats)
    dump("Review.csv", ["project", "topic", "detail"], review)
    if assemblies:
        dump("Assembly_Breakdown_ESTIMATE.csv", ASSEMBLY_HEADINGS, assemblies)


def style(ws, color="FF181F34"):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor=color)
    for j in range(1, ws.max_column + 1):
        w = max(len(str(c.value or "")) for c in list(ws[get_column_letter(j)])[:200])
        ws.column_dimensions[get_column_letter(j)].width = min(max(w + 2, 12), 60)


def write_workbook(path, compiled, file_rows, stats, review, assemblies=()):
    wb = Workbook()
    ws = wb.active; ws.title = "Project Compiled"
    ws.append(HEADINGS); [ws.append(r) for r in compiled]; style(ws)
    wa = wb.create_sheet("Assembly Breakdown"); wa.append(ASSEMBLY_HEADINGS); [wa.append(r) for r in assemblies]; style(wa)
    wf = wb.create_sheet("File Level"); wf.append(FILE_HEADER)
    for r in file_rows:
        wf.append([r["project"], r["ext"], r["cls"], r["sw"], r["domain"], r["size"], r["modified"], r["sha"],
                   r["sig"], "yes" if r["dup"] else ""])
    style(wf)
    wst = wb.create_sheet("Project Stats"); wst.append(STATS_HEADER); [wst.append(r) for r in stats]; style(wst)
    wr = wb.create_sheet("Review"); wr.append(["project", "topic", "detail"]); [wr.append(r) for r in review]; style(wr)
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--unique", action="store_true", help="count each content hash once per project")
    ap.add_argument("--pdf-drawings", choices=["none", "all"], default="none")
    ap.add_argument("--estimate-assemblies", action="store_true",
                    help="fill Assembly Breakdown with an even-split ESTIMATE (placeholder names)")
    ap.add_argument("--map-dir", default=str(HERE / "mapping"))
    ap.add_argument("--out", help="output dir (default: ./compile-profiles/<timestamp> under the current folder)")
    a = ap.parse_args()

    ext_map, soft = load_mapping(a.map_dir)
    runs = load_profiles(a.inputs)
    compiled, file_rows, stats, review, assemblies = compile_runs(runs, ext_map, soft, a.unique, a.pdf_drawings,
                                                                  a.estimate_assemblies)

    out = Path(a.out) if a.out else Path.cwd() / "compile-profiles" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    write_csvs(out, compiled, file_rows, stats, review, assemblies)
    if Workbook:
        write_workbook(out / "compiled.xlsx", compiled, file_rows, stats, review, assemblies)
    else:
        print("(openpyxl not installed - wrote CSVs only)")

    print(f"{len(runs)} profile zip(s) -> {len(compiled)} project(s), {len(file_rows)} files "
          f"({'unique hashes' if a.unique else 'all files'} counted; PDF drawings: {a.pdf_drawings}; "
          f"assembly rows: {'ESTIMATED, ' + str(len(assemblies)) if a.estimate_assemblies else 'not produced'})")
    for r in compiled:
        print("  ", r)
    print(f"Review items: {len(review)}  (see Review)\nWrote files in {out}")


if __name__ == "__main__":
    main()
