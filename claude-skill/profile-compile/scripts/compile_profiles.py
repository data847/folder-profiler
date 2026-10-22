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
Defaults: --project-by auto (structure split whenever the profile has resolvable references) and the Assembly Breakdown is
filled (confidence reported). --project-by folder / --no-assembly-breakdown turn these off.
--project-by structure|auto: split each folder-project into sub-projects, one per ROOT assembly (an assembly no other assembly
      references) plus everything it references; needs a level-2 profile. Sub-projects under --min-project-files merge into
      "<project> / (other)". Outputs add Assembly_Confidence.csv (per project: High / Medium / Low and why) and
      Assembly_Evidence.csv (per assembly: references, how many resolved, parts by method).
--estimate-assemblies: a profile has no real part-to-assembly links, so this fills the Assembly Breakdown tab with an
      ESTIMATE that adds up to each project's totals. Profiles from profiler >= 1.2 carry anonymous folder ids and the
      names of assembly files, so each part / native 2D / PDF / neutral 3D file goes to the assembly in its own folder,
      else the nearest folder above, else is spread over the nearest assemblies (FOLDER-BASED estimate, real names).
      Older profiles have neither, so totals are split evenly with placeholder names ("EST_001.ASM") (EVEN-SPLIT).
      Projects with no assembly files get no rows. Always call it an estimate: folders are not CAD links.
PDFs: PDF Drawings is 0 by default (a metric-only profile cannot tell a drawing from a report);
      --pdf-drawings all counts every PDF as a drawing.
"""
import argparse
import collections
import csv
import io
import re
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
sys.path.insert(0, str(HERE))          # structure.py sits next to this file (also under `python -I`)
from structure import (assign_assemblies, build_universe, detect_copies, split_by_structure, split_evenly, stem)  # noqa: E402,F401

HEADINGS = ["Project Name", "Project Description", "Domain", "Native CAD Software", "Part Files",
            "Assembly Files", "Native 2D Drawings", "PDF Drawings", "Neutral 3D Files",
            "2D Files (file types)", "3D Files (file types)"]
ASSEMBLY_HEADINGS = ["Project Name", "Assembly File Name", "Part Files", "Native 2D Drawings",
                     "PDF Drawings", "Neutral 3D Files"]
FILE_HEADER = ["project", "ext", "class", "native_software", "domain", "size_bytes", "modified_utc", "sha256",
               "header_signature", "duplicate_of_earlier_file", "in_archive", "archive_duplicate_not_counted", "dedupe_key"]
STATS_HEADER = ["project", "files_total", "unique_hashes", "duplicate_files", "parts", "assemblies",
                "native_2d_drawings", "neutral_3d", "pdf", "simulation", "documents", "data", "images",
                "archive_other", "unmapped", "total_bytes", "counted_as", "files_in_archives", "archive_duplicates_not_counted",
                "possible_copy_of", "copy_share"]
CLASSES = {"PART", "ASSEMBLY", "DRAWING", "NEUTRAL3D", "PDF", "SIM", "DOC", "DATA", "IMG", "ARCHIVE", "OTHER"}
NATIVE = {"PART", "ASSEMBLY", "DRAWING"}      # only these name the "Native CAD Software"
REPORT_WORDS = re.compile(r"report|spec|manual|datasheet|data.?sheet|catalog|brochure|invoice|quot|certificate|summary|presentation|"
                          r"assessment|breakdown|change.?log|verification|proposal|minutes|checklist|offer|\bbom\b|readme", re.I)
DRAWING_WORDS = re.compile(r"drawing|\bdrg\b|\bdwg\b|layout|sheet|assy|detail|schematic|arrangement", re.I)
NO_ASSEMBLY_ROW = "(no assembly file in this project)"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


VERSIONED_EXTS = {"prt", "asm", "drw", "frm", "lay", "mfg", "sec", "dgm"}      # Creo / Pro-E: part.prt.7
SIG_FALLBACK = (("STEP:", "NEUTRAL3D"), ("IFC:", "NEUTRAL3D"), ("IGES:", "NEUTRAL3D"), ("DXF:", "DRAWING"), ("DWG:", "DRAWING"),
                ("PDF", "DOC"), ("ZIP", "ARCHIVE"), ("GZIP", "ARCHIVE"), ("7Z", "ARCHIVE"), ("PNG", "IMG"), ("JPEG", "IMG"),
                ("GIF", "IMG"), ("BMP", "IMG"), ("TIFF", "IMG"), ("SIM:", "SIM"), ("MESH:", "SIM"), ("VTK:", "SIM"))


def derive_ext(ext, rel):
    """Old profiles recorded Creo versions as '.7'; when the file name is known, recover the real type ('.prt')."""
    if ext.isdigit() and rel:
        base = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        inner = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        if inner in VERSIONED_EXTS:
            return inner
    return ext


def class_from_signature(sig):
    """When the extension is not in the table: classify by what the file starts with (STEP, DXF, PDF, ZIP ...)."""
    for prefix, cls in SIG_FALLBACK:
        if sig.startswith(prefix):
            return cls
    return None


def dedupe_keys(size, mtime, sha, name, archive, ext=""):
    """-> (primary, secondary) keys used to spot duplicate files WITHOUT hashing contents:
         profile with sha256 (older profiler)   primary = that exact hash
         loose file                             primary = type + size + modified date, secondary = file name + size
         file inside an archive                 primary = file name + size (zip dates are too coarse to compare)
       The secondary key lets an archived file be matched with a loose copy. Empty files are never duplicates."""
    if size <= 0:
        return "", ""
    sec = f"ad:{name}:{size}" if name else ""
    if sha:
        return "sha:" + sha, sec
    if archive:
        return sec, sec
    return f"sd:{ext}:{size}:{mtime}", sec           # two different file types are never duplicates of each other


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
        zips += sorted(z for z in p.glob("*.zip") if not z.name.startswith("run_stats_")) if p.is_dir() else [p]
    runs = []
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            names = set(zf.namelist())
            if "Files.csv" not in names or "Run_info.csv" not in names:
                print(f"skip {z.name}: not a profiler zip", file=sys.stderr)
                continue
            info = {r["key"]: r["value"] for r in read_csv(zf.read("Run_info.csv").decode("utf-8-sig"))}
            files = read_csv(zf.read("Files.csv").decode("utf-8-sig"))
            links = read_csv(zf.read("Links.csv").decode("utf-8-sig")) if "Links.csv" in names else []
            parents = {}
            if "Dirs.csv" in names:  # profiler >= 1.2: anonymous folder tree (ids only)
                parents = {d["dir_id"]: d["parent_dir_id"] for d in read_csv(zf.read("Dirs.csv").decode("utf-8-sig"))}
        runs.append({"zip": z.name, "info": info, "files": files, "parents": parents, "links": links})
    if not runs:
        raise SystemExit("No profiler zips found.")
    keys = collections.Counter((r["info"].get("folder_name"), p) for r in runs for p in {f["project"] for f in r["files"]})
    dup = sorted(f"{k[0]}/{k[1]}" for k, n in keys.items() if n > 1)
    if dup:  # same folder profiled twice would be silently double-counted
        raise SystemExit("Same project appears in more than one zip (would double-count): " + ", ".join(dup))
    return runs


def compile_runs(runs, ext_map, soft, unique, pdf_mode, estimate=False, project_by="folder", min_project_files=25,
                 unique_scope="project", copies="flag"):
    names = collections.Counter(p for r in runs for p in {f["project"] for f in r["files"]})
    per_project, unmapped = {}, collections.Counter()
    sig_classified = collections.Counter()
    unmapped_info = collections.defaultdict(lambda: [0, 0, set(), collections.Counter()])
    for ri, run in enumerate(runs):
        lk = collections.defaultdict(lambda: {"native": set(), "names": set(), "step": collections.defaultdict(set)})
        for l in run.get("links", []):
            e = lk[(l["project"], int(l["file_no"]))]
            if l["kind"] == "native":
                e["native"].add(stem(l["child"]))
                e["names"].add(l["child"].lower())
            else:
                e["step"][stem(l["parent"], False)].add(stem(l["child"], False))
        folder = run["info"].get("folder_name", run["zip"])
        for f in run["files"]:
            proj = f["project"] if names[f["project"]] == 1 else f"{folder}/{f['project']}"
            rel = f.get("rel_path", "")
            ext = derive_ext(f["ext"].lower(), rel)
            m = ext_map.get(ext)
            cls = m["class"] if m else "UNMAPPED"
            by_signature = False
            if not m:
                fb = class_from_signature(f["header_signature"])
                if fb:
                    cls, by_signature = fb, True
                    sig_classified[ext] += 1
                else:
                    unmapped[(proj, ext)] += 1
                    unmapped_info[ext][0] += 1
                    unmapped_info[ext][1] += int(f["size_bytes"])
                    unmapped_info[ext][2].add(proj)
                    unmapped_info[ext][3][f["header_signature"]] += 1
            sw = (m["software"] if m else "") if cls in NATIVE else ""
            tool = tool_in_tag(f["header_signature"]) if cls in ("NEUTRAL3D", "SIM") else ""
            domain = (m["domain"] if m else "") or (soft[sw]["domain"] if sw in soft else "")
            P = per_project.setdefault(proj, {"hashes": set(), "rows": [], "parents": {}})
            P["parents"] = run.get("parents", {})
            sha = f.get("sha256", "")
            size = int(f["size_bytes"])
            rel_name = rel.rsplit("/", 1)[-1] if rel else ""
            prim, sec = dedupe_keys(size, f["modified_utc"], sha, rel_name.lower(), f.get("archive", ""), ext)
            dup = bool(prim) and prim in P["hashes"]
            P["hashes"].update(k for k in (prim, sec) if k)
            nm = rel_name if rel else (f.get("asm_name", "") or "")
            P["rows"].append(dict(project=proj, ext=ext, cls=cls, sw=sw, tool=tool, domain=domain,
                                  size=size, modified=f["modified_utc"], sha=sha, keys=(prim, sec), fp=prim if prim.startswith(("sha:", "sd:")) else "",
                                  sig=f["header_signature"], dup=dup, dir=f.get("dir_id", ""), asm=f.get("asm_name", ""),
                                  name=nm, stem=stem(nm) if nm else "", rel=rel, pdfdraw=False, archive=f.get("archive", ""),
                                  adup=dup and bool(f.get("archive", "")),
                                  lk=lk.get((f["project"], int(f["file_no"])))))

    review, evidence_rows, confidence_rows = [], [], []
    universe = build_universe([r for P in per_project.values() for r in P["rows"]])    # references may point to other folders
    if project_by in ("structure", "auto"):             # split folder-projects into sub-projects by root assembly
        for proj in list(per_project):
            P = per_project[proj]
            groups, why = split_by_structure(proj, P["rows"], P["parents"], min_project_files, with_reason=True, universe=universe)
            if not groups:
                if project_by == "auto" and any(r.get("lk") for r in P["rows"]):
                    review.append([proj, "project split", f"kept as one folder-project: {why}"])
                continue
            del per_project[proj]
            if why:
                review.append([proj, "project split", why])
            for gname, grows in groups.items():
                G = per_project.setdefault(gname, {"hashes": set(), "rows": [], "parents": P["parents"]})
                for r in grows:
                    r["project"] = gname
                    r["dup"] = bool(r["keys"][0]) and r["keys"][0] in G["hashes"]
                    r["adup"] = r["dup"] and bool(r["archive"])
                    G["hashes"].update(k for k in r["keys"] if k)
                    G["rows"].append(r)
            review.append([proj, "project split", f"split into {len(groups)} sub-projects by root assembly (an assembly no other assembly "
                           f"references, plus everything it references): " + ", ".join(sorted(g.split(" / ", 1)[-1] for g in groups)[:12])
                           + (" ..." if len(groups) > 12 else "")])
    copy_of = detect_copies(per_project) if copies != "keep" else {}
    if unique and unique_scope == "all":                # each content counted once in the WHOLE profile (biggest project keeps it)
        seen_all = set()
        for proj in sorted(per_project, key=lambda p: (-len(per_project[p]["rows"]), p.lower())):
            mine = set()
            for r in per_project[proj]["rows"]:
                if r["fp"]:
                    if r["fp"] in seen_all:
                        r["dup"] = True
                    mine.add(r["fp"])
            seen_all |= mine
    compiled, file_rows, stats, assemblies = [], [], [], []
    for proj in sorted(per_project, key=str.lower):
        rows = per_project[proj]["rows"]
        file_rows += rows
        if proj in copy_of and copies == "exclude":
            orig, share, nfiles = copy_of[proj]
            review.append([proj, "possible copy", f"EXCLUDED from Project Compiled: {share:.0%} of its {nfiles} files match files in '{orig}'"])
            continue
        counted = [r for r in rows if not ((unique and r["dup"]) or r["adup"])]
        n = collections.Counter(r["cls"] for r in counted)
        models = {r["stem"] for r in counted if r["cls"] in ("DRAWING", "PART", "ASSEMBLY") and r["stem"]}
        for r in counted:                               # which PDFs are drawings
            if r["cls"] != "PDF":
                continue
            if pdf_mode == "all":
                r["pdfdraw"] = True
            elif pdf_mode == "auto" and r["name"]:
                r["pdfdraw"] = (r["stem"] in models) or bool(DRAWING_WORDS.search(r["name"]) and not REPORT_WORDS.search(r["name"]))
        pdf_drawings = sum(1 for r in counted if r["pdfdraw"])

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
        stats.append([proj, len(rows), len({r["keys"][0] for r in rows if r["keys"][0]}) + sum(1 for r in rows if not r["keys"][0]), sum(r["dup"] for r in rows), n["PART"],
                      n["ASSEMBLY"], n["DRAWING"], n["NEUTRAL3D"], n["PDF"], n["SIM"], n["DOC"], n["DATA"],
                      n["IMG"], n["ARCHIVE"] + n["OTHER"], n["UNMAPPED"], sum(r["size"] for r in rows),
                      ("unique hashes (whole profile)" if unique_scope == "all" else "unique hashes (per project)") if unique else "all files",
                      sum(1 for r in rows if r["archive"]), sum(1 for r in rows if r["adup"]),
                      copy_of[proj][0] if proj in copy_of else "", f"{copy_of[proj][1]:.0%}" if proj in copy_of else ""])
        n_arch = sum(1 for r in rows if r["archive"])
        if n_arch:
            review.append([proj, "archives", f"{n_arch} files found inside archives ({sum(1 for r in rows if r['adup'])} identical to files "
                           "already present in this project, so not counted again; 7z/rar are listed only and have no fingerprint)"])
        if proj in copy_of and copies == "flag":
            orig, share, nfiles = copy_of[proj]
            review.append([proj, "possible copy", f"{share:.0%} of its {nfiles} files match files in '{orig}' (same size and modified date, or identical content when the profile has hashes) - likely a "
                           "backup or copy; counts include it (use --copies exclude to drop it, --unique --unique-scope all to count each content once)"])

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
            review.append([proj, "PDF drawings", f"{n['PDF']} PDF file(s) NOT counted as drawings (PDF Drawings = 0; --pdf-drawings none)"])
        elif n["PDF"] and pdf_mode == "auto" and not any(r["name"] for r in counted if r["cls"] == "PDF"):
            review.append([proj, "PDF drawings", f"{n['PDF']} PDF file(s) NOT counted: the profile has no file names (detail level 0), so "
                           "drawings cannot be told from reports. Re-profile at level 1+, or use --pdf-drawings all"])
        elif n["PDF"] and pdf_mode == "auto":
            review.append([proj, "PDF drawings", f"{pdf_drawings} of {n['PDF']} PDFs counted as drawings by name (same name as a model or "
                           "drawing, or drawing-like wording without report-like wording); the rest treated as documents"])
        if n["ASSEMBLY"] and estimate:
            rows_a, note, ev = assign_assemblies(proj, counted, per_project[proj]["parents"], universe)
            assemblies += rows_a
            review.append([proj, "assembly breakdown", f"ESTIMATE - {note}"])
            if ev:
                evidence_rows += ev["rows"]
                confidence_rows.append(ev["project"])
        elif n["ASSEMBLY"]:
            review.append([proj, "assembly breakdown", f"{n['ASSEMBLY']} assembly file(s): Assembly Breakdown tab left empty "
                           "(needs assembly file names and which parts belong to each; --estimate-assemblies gives an even-split estimate)"])
        elif estimate and (n["PART"] or n["DRAWING"] or n["NEUTRAL3D"] or pdf_drawings):
            assemblies.append([proj, NO_ASSEMBLY_ROW, n["PART"], n["DRAWING"], pdf_drawings, n["NEUTRAL3D"]])
            review.append([proj, "assembly breakdown", f"this project has no assembly file, so ONE summary row ('{NO_ASSEMBLY_ROW}') carries its "
                           f"{n['PART']} parts, {n['DRAWING']} drawings, {pdf_drawings} PDF drawings and {n['NEUTRAL3D']} neutral files; the tab adds "
                           "up to the project totals"])
            confidence_rows.append({"project": proj, "confidence": "n/a", "why": "no assembly file in this project: one summary row, no assembly structure",
                                    **{h: 0 for h in CONFIDENCE_HEADER[3:19]}, "unresolved_examples": ""})
    for (proj, ext), k in sorted(unmapped.items()):
        review.append([proj, "unmapped extension", f".{ext} x{k} - add it to extensions.csv"])
    total_n = sum(len(P["rows"]) for P in per_project.values())
    unm_n = sum(v[0] for v in unmapped_info.values())
    unm_b = sum(v[1] for v in unmapped_info.values())
    tot_b = sum(r["size"] for P in per_project.values() for r in P["rows"]) or 1
    sig_n = sum(sig_classified.values())
    review.insert(0, ["(all projects)", "categorisation",
                      f"{total_n - sig_n - unm_n:,} of {total_n:,} files ({100 * (total_n - sig_n - unm_n) / max(1, total_n):.1f}%) classified by extension, "
                      f"{sig_n:,} by file content (unknown extension but a recognisable STEP/DXF/DWG/PDF/ZIP/... start), {unm_n:,} unmapped "
                      f"({100 * unm_b / tot_b:.1f}% of bytes) - see Unmapped_Extensions.csv"
                      + (f"; by content: {', '.join(f'.{e} x{k}' for e, k in sig_classified.most_common(5))}" if sig_n else "")])
    unm_rows = [[e, v[0], round(v[1] / 1e6, 1), len(v[2]), "; ".join(f"{t} x{k}" for t, k in v[3].most_common(3))]
                for e, v in sorted(unmapped_info.items(), key=lambda kv: -kv[1][0])]
    return compiled, file_rows, stats, review, assemblies, {"rows": evidence_rows, "projects": confidence_rows, "unmapped": unm_rows}


EVIDENCE_HEADER = ["project", "assembly", "refs_total", "refs_resolved", "refs_external", "refs_ambiguous", "refs_unresolved", "parts_link", "parts_folder",
                   "parts_spread", "parts_even", "other_by_link", "other_by_name", "other_by_folder"]
CONFIDENCE_HEADER = ["project", "confidence", "why", "assemblies", "parts", "parts_link", "parts_folder", "parts_spread", "parts_even",
                     "refs_total", "refs_resolved", "refs_external", "refs_ambiguous", "refs_unresolved", "shared_parts", "assemblies_without_refs",
                     "others_link", "others_name", "others_folder_or_even", "unresolved_examples"]


UNMAPPED_HEADER = ["ext", "files", "MB", "projects", "header_tags_seen"]


def file_row(r):
    return [r["project"], r["ext"], r["cls"], r["sw"], r["domain"], r["size"], r["modified"], r["sha"], r["sig"],
            "yes" if r["dup"] else "", r["archive"], "yes" if r["adup"] else "", r["fp"] or r["keys"][0]]


def write_csvs(out, compiled, file_rows, stats, review, assemblies=(), evidence=None):
    def dump(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(header); w.writerows(rows)
    dump("Project_Compiled.csv", HEADINGS, compiled)
    dump("File_Level.csv", FILE_HEADER, [file_row(r) for r in file_rows])
    dump("Project_Stats.csv", STATS_HEADER, stats)
    dump("Review.csv", ["project", "topic", "detail"], review)
    if assemblies:
        dump("Assembly_Breakdown_ESTIMATE.csv", ASSEMBLY_HEADINGS, assemblies)
    if evidence and evidence.get("unmapped"):
        dump("Unmapped_Extensions.csv", UNMAPPED_HEADER, evidence["unmapped"])
    if evidence and evidence["projects"]:
        dump("Assembly_Evidence.csv", EVIDENCE_HEADER, [[r[h] for h in EVIDENCE_HEADER] for r in evidence["rows"]])
        dump("Assembly_Confidence.csv", CONFIDENCE_HEADER, [[r[h] for h in CONFIDENCE_HEADER] for r in evidence["projects"]])


def style(ws, color="FF181F34"):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor=color)
    for j in range(1, ws.max_column + 1):
        w = max(len(str(c.value or "")) for c in list(ws[get_column_letter(j)])[:200])
        ws.column_dimensions[get_column_letter(j)].width = min(max(w + 2, 12), 60)


def write_workbook(path, compiled, file_rows, stats, review, assemblies=(), evidence=None):
    wb = Workbook()
    ws = wb.active; ws.title = "Project Compiled"
    ws.append(HEADINGS); [ws.append(r) for r in compiled]; style(ws)
    wa = wb.create_sheet("Assembly Breakdown"); wa.append(ASSEMBLY_HEADINGS); [wa.append(r) for r in assemblies]; style(wa)
    wf = wb.create_sheet("File Level"); wf.append(FILE_HEADER)
    for r in file_rows:
        wf.append(file_row(r))
    style(wf)
    wst = wb.create_sheet("Project Stats"); wst.append(STATS_HEADER); [wst.append(r) for r in stats]; style(wst)
    if evidence and evidence["projects"]:
        wc = wb.create_sheet("Assembly Confidence"); wc.append(CONFIDENCE_HEADER)
        [wc.append([r[h] for h in CONFIDENCE_HEADER]) for r in evidence["projects"]]; style(wc)
        we = wb.create_sheet("Assembly Evidence"); we.append(EVIDENCE_HEADER)
        [we.append([r[h] for h in EVIDENCE_HEADER]) for r in evidence["rows"]]; style(we)
    if evidence and evidence.get("unmapped"):
        wu = wb.create_sheet("Unmapped Extensions"); wu.append(UNMAPPED_HEADER)
        [wu.append(r) for r in evidence["unmapped"]]; style(wu)
    wr = wb.create_sheet("Review"); wr.append(["project", "topic", "detail"]); [wr.append(r) for r in review]; style(wr)
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--unique", action="store_true", help="count each content hash once (per project, or see --unique-scope)")
    ap.add_argument("--unique-scope", choices=["project", "all"], default="project",
                    help="with --unique: 'project' (default) or 'all' = once in the whole profile (copies of a project elsewhere vanish)")
    ap.add_argument("--copies", choices=["flag", "exclude", "keep"], default="flag",
                    help="projects that are mostly (>= 80%%) identical to a bigger project: flag them in Review (default), exclude them "
                         "from Project Compiled, or keep = do not look")
    ap.add_argument("--pdf-drawings", choices=["auto", "none", "all"], default="auto",
                    help="auto (default): by file name when the profile has names (level >= 1), else none; none; all")
    ap.add_argument("--estimate-assemblies", action="store_true", help=argparse.SUPPRESS)     # kept for old command lines: now the default
    ap.add_argument("--no-assembly-breakdown", action="store_true",
                    help="leave the Assembly Breakdown tab empty (default: fill it, with method and confidence reported)")
    ap.add_argument("--project-by", choices=["auto", "structure", "folder"], default="auto",
                    help="auto (default): split each folder into sub-projects by root assembly when the profile has references "
                         "(level 2) and enough of them resolve, otherwise keep the folder; structure: same but always tries; "
                         "folder: never split")
    ap.add_argument("--min-project-files", type=int, default=25,
                    help="with --project-by structure: sub-projects smaller than this are merged into '<project> / (other)'")
    ap.add_argument("--map-dir", default=str(HERE / "mapping"))
    ap.add_argument("--out", help="output dir (default: ./compile-profiles/<timestamp> under the current folder)")
    a = ap.parse_args()

    ext_map, soft = load_mapping(a.map_dir)
    runs = load_profiles(a.inputs)
    compiled, file_rows, stats, review, assemblies, evidence = compile_runs(
        runs, ext_map, soft, a.unique, a.pdf_drawings, not a.no_assembly_breakdown, a.project_by, a.min_project_files, a.unique_scope, a.copies)

    out = Path(a.out) if a.out else Path.cwd() / "compile-profiles" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    write_csvs(out, compiled, file_rows, stats, review, assemblies, evidence)
    if Workbook:
        write_workbook(out / "compiled.xlsx", compiled, file_rows, stats, review, assemblies, evidence)
    else:
        print("(openpyxl not installed - wrote CSVs only)")

    print(f"{len(runs)} profile zip(s) -> {len(compiled)} project(s), {len(file_rows)} files "
          f"({'unique hashes' if a.unique else 'all files'} counted; PDF drawings: {a.pdf_drawings}; "
          f"assembly rows: {'ESTIMATED, ' + str(len(assemblies)) if not a.no_assembly_breakdown else 'not produced'})")
    for r in compiled:
        print("  ", r)
    print(f"Review items: {len(review)}  (see Review)\nWrote files in {out}")


if __name__ == "__main__":
    main()
