#!/usr/bin/env python3
"""Compile Folder Profiler zips into the "Projects Compiled" workbook format.

    python compile_profiles.py PROFILE.zip [MORE.zip | FOLDER_OF_ZIPS ...]
        [--unique] [--pdf-drawings none|all] [--estimate-assemblies] [--map-dir DIR] [--out DIR]

Input : profile_*.zip written by the Folder Profiler (Files.csv, Run_info.csv, ...).
Output: Project_Compiled.csv, File_Level.csv, Project_Stats.csv, Review.csv, and compiled.xlsx
        (needs `openpyxl`; skipped with a notice if it is not installed) with tabs
          "Project Compiled"    11 columns, exactly the template's headings
          "Drawing Breakdown"   one row per drawing file (native 2D drawing or PDF counted as a drawing): format, software, sheets
                                (AutoCAD, with confidence), the assembly it belongs to and HOW that was decided (reference / name / folder /
                                several candidates / none); rows add up to Native 2D Drawings + PDF Drawings
          "Assembly Breakdown"  template headings only - see Review (needs file names + assembly structure,
                                which the profiler deliberately does not collect) - or, with
                                --estimate-assemblies, ESTIMATED rows (see below)
          "File Level", "Project Stats", "Review"   supporting detail
Step 1 zips (quick_*.zip from the profiler's quick count) are recognised and compiled into quick_count.xlsx (Overview by class, By Extension, By
Project) - no other options apply to them.
Local only: no network, no credentials. Reads zips, writes files.
Scale: the profile is streamed one project at a time (memory = the biggest project, not the whole profile); File_Level, Assembly_Breakdown and
Assembly_Evidence are split into parts of 1,000,000 rows (Excel's sheet limit is 1,048,576) and compiled.xlsx holds the first --xlsx-max-sheets of them.

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
import itertools
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # xlsx is a convenience; CSVs are always written
    Workbook = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # structure.py sits next to this file (also under `python -I`)
from structure import (assign_assemblies, build_universe, detect_copies, detect_copies_from, split_by_structure, split_evenly, stem)  # noqa: E402,F401

HEADINGS = ["Project Name", "Project Description", "Domain", "Native CAD Software", "Part Files",
            "Assembly Files", "Native 2D Drawings", "PDF Drawings", "Neutral 3D Files",
            "2D Files (file types)", "3D Files (file types)"]
DRAWING_HEADINGS = ["Project Name", "Drawing File Name", "Format", "Native CAD Software", "Sheets", "Sheets Confidence", "Sheets Basis", "Assembly",
                    "Assembly Method", "In Archive"]
ASSEMBLY_HEADINGS = ["Project Name", "Assembly File Name", "Part Files", "Native 2D Drawings",
                     "PDF Drawings", "Neutral 3D Files"]
FILE_HEADER = ["project", "ext", "class", "native_software", "domain", "size_bytes", "modified_utc", "sha256",
               "header_signature", "duplicate_of_earlier_file", "in_archive", "archive_duplicate_not_counted", "dedupe_key",
               "sheets", "sheets_confidence", "sheets_status", "sheets_used", "sheets_basis"]
STATS_HEADER = ["project", "files_total", "unique_hashes", "duplicate_files", "parts", "assemblies",
                "native_2d_drawings", "neutral_3d", "pdf", "simulation", "documents", "data", "images",
                "archive_other", "unmapped", "total_bytes", "counted_as", "files_in_archives", "archive_duplicates_not_counted",
                "possible_copy_of", "copy_share", "drawing_sheets", "sheet_files_counted", "sheet_files_model_only", "sheet_files_assumed",
                "sheets_confidence_avg", "sheets_confidence_min", "domain_basis", "domain_confidence"]
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


class Row:
    """One file of a profile. A __slots__ object instead of a dict: about a fifth of the memory per file, which is what lets a profile
    with millions of files compile. Behaves like the dict the structure code was written for (r["x"], r.get("x"), r["x"] = v)."""
    __slots__ = ("project", "ext", "cls", "sw", "tool", "domain", "size", "modified", "sha", "keys", "fp", "sig", "dup", "dir", "asm",
                 "name", "stem", "rel", "pdfdraw", "archive", "adup", "lk", "sheets", "sheets_conf", "sheets_status")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getitem__(self, k):
        return getattr(self, k)

    def __setitem__(self, k, v):
        setattr(self, k, v)

    def get(self, k, default=None):
        return getattr(self, k, default)


_I = sys.intern


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
    sigp = Path(map_dir) / "signature_software.csv"          # which program wrote a file, judged by what is INSIDE it
    soft[BY_SIGNATURE] = {r["sig"].strip(): r["software"].strip() for r in read_csv(sigp)} if sigp.exists() else {}
    kwp = Path(map_dir) / "domain_keywords.csv"             # file / folder name words that point to a domain (the fallback when no specific tool does)
    pats = collections.defaultdict(list)
    for r in (read_csv(kwp) if kwp.exists() else []):
        pats[r["domain"].strip()].append(r["pattern"].strip())
    soft[DOMAIN_KEYWORDS] = {d: re.compile("|".join(f"(?:{p})" for p in ps), re.I) for d, ps in pats.items()}
    return ext_map, soft


BY_SIGNATURE = "__by_signature__"
DOMAIN_KEYWORDS = "__domain_keywords__"


class CsvSource:
    """A CSV inside a profile zip. Iterating streams it row by row (never loaded whole) and can be repeated."""

    def __init__(self, zip_path, member):
        self.zip_path, self.member = str(zip_path), member

    def __iter__(self):
        with zipfile.ZipFile(self.zip_path) as zf, zf.open(self.member) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


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
            parents = {}
            if "Dirs.csv" in names:  # profiler >= 1.2: anonymous folder tree (ids only)
                with zf.open("Dirs.csv") as raw:
                    for d in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")):
                        parents[_I(d["dir_id"])] = _I(d["parent_dir_id"])
            counts = None
            if "Projects.csv" in names:
                counts = {r["project"]: int(r["file_count"]) for r in read_csv(zf.read("Projects.csv").decode("utf-8-sig"))}
        runs.append({"zip": z.name, "info": info, "files": CsvSource(z, "Files.csv"), "parents": parents, "projects": counts,
                     "links": CsvSource(z, "Links.csv") if "Links.csv" in names else [],
                     "unreadable": CsvSource(z, "Unreadable.csv") if "Unreadable.csv" in names else []})
    if not runs:
        raise SystemExit("No profiler zips found.")
    for r in runs:
        if r["projects"] is None:                       # older zip without Projects.csv: one streaming pass for the names
            c = collections.Counter(f["project"] for f in r["files"])
            r["projects"] = dict(c)
    keys = collections.Counter((r["info"].get("folder_name"), p) for r in runs for p in r["projects"])
    dup = sorted(f"{k[0]}/{k[1]}" for k, n in keys.items() if n > 1)
    if dup:  # same folder profiled twice would be silently double-counted
        raise SystemExit("Same project appears in more than one zip (would double-count): " + ", ".join(dup))
    return runs


def stream_projects(run, make_builder):
    """Yield (project, builder) in file order. Every CSV row goes straight into its project's builder (a compact Row object), so the raw
    rows are never held; a project is handed on as soon as its last row has been read (Projects.csv says how many rows it has). Rows of
    one project that are not contiguous simply keep their builder open."""
    counts = run.get("projects") or {}
    open_ = {}
    for f in run["files"]:
        p = f["project"]
        b = open_.get(p)
        if b is None:
            b = open_[p] = make_builder(p)
        b.add(f)
        if len(b.rows) >= counts.get(p, 1 << 60):
            b = None
            yield p, open_.pop(p)
    for p, b in open_.items():
        yield p, b


class LinkIndex:
    """Links.csv rows of one project at a time. The profiler writes them in the same project order as Files.csv, so this reads the file
    once, in step with the projects; rows that arrive out of that order are counted in `unmatched`."""

    def __init__(self, links):
        self.it = iter(links)
        self.ahead = None
        self.asked = set()
        self.unmatched = 0

    def take(self, project):
        self.asked.add(project)
        out = []
        while True:
            if self.ahead is None:
                self.ahead = next(self.it, None)
                if self.ahead is None:
                    break
            if self.ahead["project"] == project:
                out.append(self.ahead)
            elif self.ahead["project"] in self.asked:      # a row of a project that was already handled: out of order, cannot be used
                self.unmatched += 1
            else:
                break                                      # belongs to a later project
            self.ahead = None
        return out

    def rest(self):
        n = self.unmatched + (1 if self.ahead is not None else 0)
        return n + sum(1 for _ in self.it)


def _lk_map(link_rows):
    lk = collections.defaultdict(lambda: {"native": set(), "names": set(), "step": collections.defaultdict(set), "scan": False})
    for l in link_rows:
        e = lk[int(l["file_no"])]
        if l["kind"] in ("native", "scan"):
            e["native"].add(stem(l["child"]))
            e["names"].add(l["child"].lower())
            if l["kind"] == "scan":
                e["scan"] = True
        else:
            e["step"][stem(l["parent"], False)].add(stem(l["child"], False))
    return lk


# ---------------------------------------------------------------------------------------------------- where rows go
class MemorySink:
    """Rows are kept in a list (small runs, tests)."""

    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def close(self, keep_empty=True):
        pass


class CsvSink:
    """Rows go straight to disk, in parts of at most `part_rows` rows (Excel cannot hold more than 1,048,576 rows in a tab, and a CSV
    that big cannot be opened in it either): <stem>.csv, <stem>_2.csv, ..."""

    def __init__(self, out, stem_name, header, part_rows=1_000_000):
        self.out, self.stem_name, self.header, self.part_rows = Path(out), stem_name, header, part_rows
        self.parts, self.n, self.f, self.w, self.total = [], 0, None, None, 0

    def _open(self):
        path = self.out / (f"{self.stem_name}.csv" if not self.parts else f"{self.stem_name}_{len(self.parts) + 1}.csv")
        self.parts.append(path)
        self.f = open(path, "w", newline="", encoding="utf-8-sig")
        self.w = csv.writer(self.f)
        self.w.writerow(self.header)
        self.n = 0

    def add(self, row):
        if self.f is None or self.n >= self.part_rows:
            if self.f:
                self.f.close()
            self._open()
        self.w.writerow([_csv_safe(v) for v in row])
        self.n += 1
        self.total += 1

    def close(self, keep_empty=True):
        if self.f:
            self.f.close()
            self.f = None
        if not self.parts and keep_empty:               # a tab with headings only is still a tab
            self._open()
            self.f.close()
            self.f = None

    def rows(self, part):
        with open(self.parts[part], newline="", encoding="utf-8-sig") as f:
            r = csv.reader(f)
            next(r)
            yield from r


class Sinks:
    def __init__(self, files=None, assemblies=None, evidence=None, drawings=None):
        self.files = files or MemorySink()
        self.assemblies = assemblies or MemorySink()
        self.evidence = evidence or MemorySink()
        self.drawings = drawings or MemorySink()

    def close(self):
        for s in (self.files, self.assemblies, self.evidence, self.drawings):
            s.close()


SHEET_BASIS_COUNTED, SHEET_BASIS_MODEL, SHEET_BASIS_ASSUMED = "counted", "model tab only - counted as 1", "not read - assumed 1"
SHEET_BASIS_NODATA = "profile has no sheet data - assumed 1"


def sheets_used(r):
    """-> (sheets to report, basis). A drawing file is at least ONE sheet: an AutoCAD file with only the Model tab (0 layouts) and one that
    could not be read (R2007, damaged ...) both count as 1, and the basis column says so; the confidence stays the file's own (0 when unread)."""
    if r["ext"] not in ("dwg", "dxf"):                                           # other drawing formats: sheets are not counted
        return ("" if r["sheets"] is None else r["sheets"]), (SHEET_BASIS_COUNTED if r["sheets"] is not None else "")
    if r["sheets_conf"] is None:                                                 # a profile made without sheet counting (older profiler / level < 2)
        return 1, SHEET_BASIS_NODATA
    if r["sheets"] is None:
        return 1, SHEET_BASIS_ASSUMED
    if r["sheets"] == 0:
        return 1, SHEET_BASIS_MODEL
    return r["sheets"], SHEET_BASIS_COUNTED


def _csv_safe(v):
    """A text cell that starts with = + @ (tab, CR) would be run as a formula when the CSV is opened in Excel; a file called 'x.=1+1' has
    the extension '=1+1'. Such cells get a leading apostrophe. Numbers and everything else are untouched."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t", "\r") else v


def file_row(r):
    return [r["project"], r["ext"], r["cls"], r["sw"], r["domain"], r["size"], r["modified"], r["sha"], r["sig"],
            "yes" if r["dup"] else "", r["archive"], "yes" if r["adup"] else "", r["fp"] or r["keys"][0],
            "" if r["sheets"] is None else r["sheets"], "" if r["sheets_conf"] is None else r["sheets_conf"], r["sheets_status"] or "", *sheets_used(r)]


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class RowBuilder:
    """Turns the CSV rows of ONE project into Row objects one at a time (classification, software, duplicate flags)."""

    def __init__(self, eng, proj, lk, count=True):
        self.e, self.proj, self.lk, self.count = eng, proj, lk, count
        self.hashes, self.rows = set(), []

    def add(self, f):
        e, proj, count = self.e, self.proj, self.count
        rel = f.get("rel_path", "")
        ext = derive_ext(f["ext"].lower(), rel)
        m = e.ext_map.get(ext)
        cls = m["class"] if m else "UNMAPPED"
        sig = f["header_signature"]
        sigsw = e.sig_soft.get(sig, "")
        if not m:
            fb = class_from_signature(sig)
            if fb:
                cls = fb
                if count:
                    e.sig_classified[ext] += 1
            elif count:
                e.unmapped[(proj, ext)] += 1
                ui = e.unmapped_info[ext]
                ui[0] += 1
                ui[1] += int(f["size_bytes"])
                ui[2].add(proj)
                ui[3][sig] += 1
        sw = (m["software"] if m else "") if cls in NATIVE else ""
        if sigsw and cls not in NATIVE and count:        # CAD content under an extension that is not a native CAD one
            e.native_unknown_ext[(proj, sigsw)] += 1
        if cls in NATIVE and sigsw and sw != sigsw:     # what is inside the file decides; the extension may be shared or wrong
            if count:
                e.sw_by_content[(proj, sw or "-", sigsw)] += 1
            sw = sigsw
        tool = tool_in_tag(sig) if cls in ("NEUTRAL3D", "SIM") else ""
        domain = (m["domain"] if m else "") or (e.soft[sw]["domain"] if sw in e.soft else "")
        size = int(f["size_bytes"])
        sha = f.get("sha256", "")
        archive = f.get("archive", "")
        rel_name = rel.rsplit("/", 1)[-1] if rel else ""
        prim, sec = dedupe_keys(size, f["modified_utc"], sha, rel_name.lower(), archive, ext)
        dup = bool(prim) and prim in self.hashes
        self.hashes.update(k for k in (prim, sec) if k)
        nm = rel_name if rel else (f.get("asm_name", "") or "")
        self.rows.append(Row(project=_I(proj), ext=_I(ext), cls=_I(cls), sw=_I(sw), tool=_I(tool), domain=_I(domain), size=size,
                             modified=f["modified_utc"], sha=sha, keys=(prim, sec),
                             fp=prim if prim.startswith(("sha:", "sd:")) else "", sig=_I(sig), dup=dup, dir=_I(f.get("dir_id", "")),
                             asm=f.get("asm_name", ""), name=nm, stem=stem(nm) if nm else "", rel=rel, pdfdraw=False, archive=_I(archive),
                             adup=dup and bool(archive),
                             lk=self.lk.get(int(f["file_no"])) if self.lk is not None else None,
                             sheets=_to_int(f.get("sheets")), sheets_conf=_to_int(f.get("sheets_conf")), sheets_status=_I(f.get("sheets_status", ""))))


# ---------------------------------------------------------------------------------------------------- the compiler
class Engine:
    """Compiles a profile one project at a time. Memory is the biggest single project plus small per-project summaries, not the whole
    profile (the old compiler held every row of every project: about 5 KB per file)."""

    def __init__(self, ext_map, soft, unique, pdf_mode, sinks, estimate=False, project_by="folder", min_project_files=25,
                 unique_scope="project", copies="flag"):
        self.ext_map, self.soft, self.unique, self.pdf_mode, self.sinks = ext_map, soft, unique, pdf_mode, sinks
        self.estimate, self.project_by, self.min_files, self.unique_scope, self.copies = estimate, project_by, min_project_files, unique_scope, copies
        self.sig_soft = soft.get(BY_SIGNATURE, {})
        self.unmapped = collections.Counter()
        self.unmapped_info = collections.defaultdict(lambda: [0, 0, set(), collections.Counter()])
        self.sig_classified = collections.Counter()
        self.sw_by_content = collections.Counter()       # (project, extension's software or "-", software seen inside)
        self.native_unknown_ext = collections.Counter()  # (project, software): native-looking content under an extension we do not know
        self.total_n = self.total_b = 0
        self.compiled, self.stats, self.confidence = {}, {}, []
        self.review_split, self.review_proj = [], collections.defaultdict(list)
        self.fp_total, self.fp_cnt = {}, {}
        self.deferred = {}                                # exclude mode: per-project outputs held back until copies are known
        self.universe = None
        self.lost_links = 0
        self.unreadable_rows = []                         # [project, kind, path, reason] of everything the profiler could not read

    # ------------------------------------------------------------------------------------------ rows
    def builder(self, proj, lk, count=True):
        return RowBuilder(self, proj, lk, count)

    def build_rows(self, proj, folder, files, lk, count=True):
        b = RowBuilder(self, proj, lk, count)
        for f in files:
            b.add(f)
        return b.rows

    def build_universe(self, runs, names):
        """References may point into OTHER folder-projects (shared standard-parts libraries, sibling projects). One light pass indexes only
        the model files that some reference names - nothing else of any other project is kept in memory."""
        ref_names, ref_stems = set(), set()
        for run in runs:
            for l in run["links"]:
                if l["kind"] in ("native", "scan"):
                    ref_names.add(l["child"].lower())
                    ref_stems.add(stem(l["child"]))
                else:
                    ref_stems.add(stem(l["child"], False))
        if not ref_names and not ref_stems:
            return None
        by_name, by_stem = collections.defaultdict(list), collections.defaultdict(list)
        for run in runs:
            folder = run["info"].get("folder_name", run["zip"])
            disp = lambda raw: raw if names[raw] == 1 else f"{folder}/{raw}"
            for raw, b in stream_projects(run, lambda raw: self.builder(disp(raw), None, count=False)):
                for r in b.rows:
                    if r["cls"] in ("PART", "ASSEMBLY") and r["name"] and not r["adup"]:
                        e = Row(cls=r["cls"], name=r["name"], stem=r["stem"], dir=r["dir"], rel=r["rel"], fp=r["fp"], adup=False)
                        if r["name"].lower() in ref_names:
                            by_name[r["name"].lower()].append(e)
                        if r["stem"] in ref_stems:
                            by_stem[r["stem"]].append(e)
        return {"by_name": by_name, "by_stem": by_stem}

    # ------------------------------------------------------------------------------------------ one folder-project -> final projects
    def split(self, proj, rows, parents):
        if self.project_by not in ("structure", "auto"):
            return [(proj, rows)]
        groups, why = split_by_structure(proj, rows, parents, self.min_files, with_reason=True, universe=self.universe)
        if not groups:
            if self.project_by == "auto" and any(r.get("lk") for r in rows):
                self.review_split.append([proj, "project split", f"kept as one folder-project: {why}"])
            return [(proj, rows)]
        if why:
            self.review_split.append([proj, "project split", why])
        out = []
        for gname, grows in groups.items():
            hashes = set()
            for r in grows:
                r["project"] = gname
                r["dup"] = bool(r["keys"][0]) and r["keys"][0] in hashes
                r["adup"] = r["dup"] and bool(r["archive"])
                hashes.update(k for k in r["keys"] if k)
            out.append((gname, grows))
        self.review_split.append([proj, "project split", f"split into {len(groups)} sub-projects by root assembly (an assembly no other assembly "
                                  f"references, plus everything it references): " + ", ".join(sorted(g.split(" / ", 1)[-1] for g in groups)[:12])
                                  + (" ..." if len(groups) > 12 else "")])
        return out

    def groups(self, runs, names):
        for run in runs:
            folder = run["info"].get("folder_name", run["zip"])
            parents = run.get("parents", {})
            links = LinkIndex(run["links"]) if run["links"] else None
            disp = lambda raw: raw if names[raw] == 1 else f"{folder}/{raw}"
            make = lambda raw: self.builder(disp(raw), _lk_map(links.take(raw)) if links else None)
            for raw, b in stream_projects(run, make):
                proj, rows = disp(raw), b.rows
                b = None
                self.total_n += len(rows)
                self.total_b += sum(r["size"] for r in rows)
                for gname, grows in self.split(proj, rows, parents):
                    yield gname, grows, parents
            if links:
                self.lost_links += links.rest()

    # ------------------------------------------------------------------------------------------ domain
    def classify_domain(self, counted, proj):
        """-> (domain, basis, confidence, [review texts]). In order:
        1. SPECIFIC tools (SolidWorks, Revit, SketchUp, KiCad ... - software_domain.csv role 'specific') vote, one vote per native file;
           general-purpose CAD (AutoCAD, Rhino ...) does not vote, because it is used in every domain.
        2. the tool named in neutral-file headers (STEP / IFC ...);
        3. file and folder NAMES matched against domain_keywords.csv (needs level >= 1);
        4. Unclassified."""
        soft, notes = self.soft, []
        native = [r for r in counted if r["cls"] in NATIVE]
        spec = collections.Counter(r["domain"] for r in native if r["domain"] and (soft.get(r["sw"], {}).get("role") or "specific") != "general")
        domain = basis = conf = ""
        if spec:
            domain, top = spec.most_common(1)[0]
            tot = sum(spec.values())
            share, cover = top / tot, tot / max(1, len(native))
            conf = "High" if share >= 0.9 and cover >= 0.2 else "Medium" if share >= 0.6 else "Low"
            basis = "specific tools"
            general = len(native) - tot
            notes.append(f"decided by {', '.join(f'{d} tools: {k} files' for d, k in spec.most_common())}" + (f"; {general} files of general-purpose CAD (AutoCAD / Rhino ...) did not vote" if general else ""))
            if len(spec) > 1 and spec.most_common(2)[1][1] / tot >= 0.2:
                notes.append("Mixed domains: " + ", ".join(f"{d} ({k})" for d, k in spec.most_common()))
        if not domain:
            neutral = collections.Counter(soft[r["tool"]]["domain"] for r in counted if r["cls"] == "NEUTRAL3D" and r["tool"] in soft and soft[r["tool"]]["domain"])
            if neutral:
                domain = neutral.most_common(1)[0][0]
                basis, conf = "neutral-file headers", "Low"
                notes.append("only the tool named inside STEP / IFC / IGES headers points to this domain: " + ", ".join(f"{d} ({k})" for d, k in neutral.most_common()))
        if True:
            kws = soft.get(DOMAIN_KEYWORDS) or {}
            hits = collections.Counter()
            named = 0
            for r in counted:
                path = r["rel"] or r["name"]
                if not path:
                    continue
                named += 1
                text = proj + "/" + path
                for d, rx in kws.items():
                    if rx.search(text):
                        hits[d] += 1
            H = sum(hits.values())
            if hits:
                nd, nk = hits.most_common(1)[0]
                nshare = nk / H
                nconf = "High" if nshare >= 0.8 and H >= 50 else "Medium" if nshare >= 0.65 and H >= 20 else "Low"
                if not domain and H >= 10 and nshare >= 0.55:
                    domain, basis, conf = nd, "file and folder names", nconf
                    notes.append(f"decided by file / folder names: {', '.join(f'{d} {k}' for d, k in hits.most_common())} of {named} named files")
                elif domain and nd != domain and nconf != "Low":
                    notes.append(f"but file / folder names point to {nd} ({nk} of {named} named files)")
                    if conf == "High" and nconf in ("Medium", "High"):
                        conf = "Medium"                                      # strong disagreement between the two kinds of evidence
                elif domain and nd == domain and conf != "High" and nconf in ("Medium", "High"):
                    conf = "High" if conf == "Medium" else conf              # tools and names agree
                    notes.append(f"file / folder names agree ({nd}: {nk} of {named} named files)")
        if not domain:
            domain, basis, conf = "Unclassified", "none", ""
            notes.append("no specific tool, neutral-file header or file / folder name points to a domain - set it by hand")
        return domain, basis, conf, notes

    # ------------------------------------------------------------------------------------------ one final project -> its output rows
    def finish(self, proj, rows, parents):
        rev = self.review_proj[proj]
        for r in rows:
            self.sinks.files.add(r if isinstance(self.sinks.files, MemorySink) else file_row(r))
        rne = [r for r in rows if r["size"] > 0]
        self.fp_total[proj] = len(rne)
        self.fp_cnt[proj] = collections.Counter(hash(r["fp"]) for r in rne if r.get("fp"))
        unique, pdf_mode, estimate, soft = self.unique, self.pdf_mode, self.estimate, self.soft
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
        domain, dom_basis, dom_conf, dom_notes = self.classify_domain(counted, proj)

        self.compiled[proj] = [proj, "", domain, sw_names, n["PART"], n["ASSEMBLY"], n["DRAWING"], pdf_drawings,
                               n["NEUTRAL3D"], t2d, t3d]
        sh_rows = [r for r in counted if r["cls"] == "DRAWING" and r["ext"] in ("dwg", "dxf")]
        sh_ok = [r for r in sh_rows if r["sheets"] is not None]
        sh_no = [r for r in sh_rows if r["sheets"] is None and r["sheets_conf"] is not None]       # attempted but not readable
        if sh_rows:
            used = [sheets_used(r) for r in sh_rows]
            n_real = sum(1 for _v, b in used if b == SHEET_BASIS_COUNTED)
            n_model = sum(1 for _v, b in used if b == SHEET_BASIS_MODEL)
            n_nodata = sum(1 for _v, b in used if b == SHEET_BASIS_NODATA)
            sh_real = [r for r, (_v, b) in zip(sh_rows, used) if b == SHEET_BASIS_COUNTED]
            sheet_stats = [sum(v for v, _b in used if v != ""), n_real, n_model, len(sh_no) + n_nodata,
                           round(sum(r["sheets_conf"] for r in sh_real) / len(sh_real)) if sh_real else "", min((r["sheets_conf"] for r in sh_real), default="")]
            STATUS_TEXT = {"unsupported-r2007": "AutoCAD 2007 format (not supported)", "no-layouts-in-version": "older than AutoCAD 2000 / no layouts in this file",
                           "dxf-binary": "binary DXF (not supported)", "too-large": "file larger than the size limit", "unreadable": "file could not be read",
                           "not-dwg": "not an AutoCAD file in spite of the extension", "not-read-tiered": "cloud-tiered file, deliberately not opened (it would be downloaded)", "no-layout-objects": "no layout found in the file",
                           "handle-mismatch": "file structure did not check out", "object-map": "object table not understood", "class-table": "class table not understood"}
            why = collections.Counter((STATUS_TEXT.get(r["sheets_status"], f"could not be read ({r['sheets_status']})") if r["sheets_status"] else
                                       "AutoCAD 2007 format (not supported)" if r["sig"] == "DWG:AC1021" else
                                       "older than AutoCAD 2000 (has no layouts)" if r["sig"] in ("DWG:AC1012", "DWG:AC1014") else
                                       "no layouts in this DXF" if r["ext"] == "dxf" else "could not be read") for r in sh_no)
            rev.append([proj, "drawing sheets",
                        f"{sheet_stats[0]} sheets in {len(sh_rows)} AutoCAD drawing files (every drawing file is at least 1 sheet): {n_real} counted from their layouts, "
                        f"{n_model} with only the Model tab (counted as 1 each), {len(sh_no) + n_nodata} not read (assumed 1 each"
                        + (f": {', '.join(f'{k} x{v}' for k, v in (why + collections.Counter({'the profile has no sheet data (made by an older profiler or below level 2)': n_nodata} if n_nodata else {})).most_common())}" if (sh_no or n_nodata) else "") + f"); confidence of the counted ones: average {sheet_stats[4]}, lowest {sheet_stats[5]} "
                        "(0-100, computed on each file from checks on its own structure)"])
        else:
            sheet_stats = ["", "", "", "", "", ""]
        self.stats[proj] = [proj, len(rows), len({r["keys"][0] for r in rows if r["keys"][0]}) + sum(1 for r in rows if not r["keys"][0]),
                            sum(r["dup"] for r in rows), n["PART"], n["ASSEMBLY"], n["DRAWING"], n["NEUTRAL3D"], n["PDF"], n["SIM"],
                            n["DOC"], n["DATA"], n["IMG"], n["ARCHIVE"] + n["OTHER"], n["UNMAPPED"], sum(r["size"] for r in rows),
                            ("unique hashes (whole profile)" if self.unique_scope == "all" else "unique hashes (per project)") if unique else "all files",
                            sum(1 for r in rows if r["archive"]), sum(1 for r in rows if r["adup"]), "", "", *sheet_stats, dom_basis, dom_conf]
        n_arch = sum(1 for r in rows if r["archive"])
        if n_arch:
            rev.append([proj, "archives", f"{n_arch} files found inside archives ({sum(1 for r in rows if r['adup'])} identical to files "
                        "already present in this project, so not counted again; 7z/rar are listed only and have no fingerprint)"])
        copy_slot = [proj, "possible copy", None]
        rev.append(copy_slot)                           # filled in (or removed) once every project is known

        if domain == "Unclassified" or dom_conf != "High" or len(dom_notes) > 1:
            rev.append([proj, "domain", f"{domain}" + (f" (confidence {dom_conf}, basis: {dom_basis})" if dom_conf else "") + ": " + "; ".join(dom_notes)])
        if sw_names == "Unknown":
            rev.append([proj, "software", "No native CAD file identifies the software (only neutral/other files)"])
        elif len(sw) > 1:
            rev.append([proj, "software", "Several native tools: " + ", ".join(f"{t} ({k})" for t, k in sw.most_common())])
        tools = collections.Counter(r["tool"] for r in counted if r["tool"])
        if tools:
            rev.append([proj, "other tools seen", "Named in STEP/IGES/IFC/simulation headers (not counted as native CAD "
                        "software): " + ", ".join(f"{t} ({k})" for t, k in tools.most_common())])
        if n["PDF"] and pdf_mode == "none":
            rev.append([proj, "PDF drawings", f"{n['PDF']} PDF file(s) NOT counted as drawings (PDF Drawings = 0; --pdf-drawings none)"])
        elif n["PDF"] and pdf_mode == "auto" and not any(r["name"] for r in counted if r["cls"] == "PDF"):
            rev.append([proj, "PDF drawings", f"{n['PDF']} PDF file(s) NOT counted: the profile has no file names (detail level 0), so "
                        "drawings cannot be told from reports. Re-profile at level 1+, or use --pdf-drawings all"])
        elif n["PDF"] and pdf_mode == "auto":
            rev.append([proj, "PDF drawings", f"{pdf_drawings} of {n['PDF']} PDFs counted as drawings by name (same name as a model or "
                        "drawing, or drawing-like wording without report-like wording); the rest treated as documents"])
        n_scan = sum(1 for r in counted if r.get("lk") and r["lk"].get("scan"))
        if n_scan:
            rev.append([proj, "references by text scan", f"{n_scan} assembly/drawing file(s) of this project are CATIA / Creo / NX files whose "
                        "references were found by searching the file for CAD file names (no format reader exists for them): treat "
                        "their assignments as lower confidence than SolidWorks / Solid Edge / STEP"])
        outs = {"asm": [], "ev": [], "conf": [], "dr": []}
        _assign_map = []
        if n["ASSEMBLY"] and estimate:
            rows_a, note, ev = assign_assemblies(proj, counted, parents, self.universe)
            _assign_map.append(ev["drawing_assign"] if ev else {})
            outs["asm"] += rows_a
            rev.append([proj, "assembly breakdown", f"ESTIMATE - {note}"])
            if ev:
                outs["ev"] += ev["rows"]
                outs["conf"].append(ev["project"])
        elif n["ASSEMBLY"]:
            rev.append([proj, "assembly breakdown", f"{n['ASSEMBLY']} assembly file(s): Assembly Breakdown tab left empty "
                        "(needs assembly file names and which parts belong to each; --estimate-assemblies gives an even-split estimate)"])
        elif estimate and (n["PART"] or n["DRAWING"] or n["NEUTRAL3D"] or pdf_drawings):
            outs["asm"].append([proj, NO_ASSEMBLY_ROW, n["PART"], n["DRAWING"], pdf_drawings, n["NEUTRAL3D"]])
            rev.append([proj, "assembly breakdown", f"this project has no assembly file, so ONE summary row ('{NO_ASSEMBLY_ROW}') carries its "
                        f"{n['PART']} parts, {n['DRAWING']} drawings, {pdf_drawings} PDF drawings and {n['NEUTRAL3D']} neutral files; the tab adds "
                        "up to the project totals"])
            outs["conf"].append({"project": proj, "confidence": "n/a", "why": "no assembly file in this project: one summary row, no assembly structure",
                                 **{h: 0 for h in CONFIDENCE_HEADER[3:19]}, "unresolved_examples": "", "folder_rule_agrees": "n/a"})
        # ---- drawing level: one row per drawing file (native 2D drawings and PDFs counted as drawings)
        assign_map = _assign_map[0] if _assign_map else {}
        for r in counted:
            if r["cls"] != "DRAWING" and not (r["cls"] == "PDF" and r["pdfdraw"]):
                continue
            if id(r) in assign_map:
                a_name, a_how = assign_map[id(r)]
            elif not n["ASSEMBLY"]:
                a_name, a_how = "", "no assembly file in this project"
            elif not estimate:
                a_name, a_how = "", "assembly breakdown switched off"
            else:
                a_name, a_how = "", "not placed"
            outs["dr"].append([proj, r["name"] or "(name not recorded: level 0 profile)", r["ext"].upper(),
                               (soft[r["sw"]]["display"] if r["sw"] in soft else r["sw"]) if r["cls"] == "DRAWING" else "",
                               *((lambda u: (u[0], "" if r["sheets_conf"] is None else r["sheets_conf"], u[1]))(sheets_used(r)) if r["cls"] == "DRAWING" else ("", "", "")),
                               a_name, a_how, r["archive"]])
        outs["dr"].sort(key=lambda x: (x[1].lower(), x[2]))
        if outs["dr"]:
            def how_of(x):
                name, h = x[7], x[8]
                return ("by reference" if h == "link" else "by name" if h == "name" else "by folder" if (h in ("folder", "spread") and name)
                        else "no assembly" if h.startswith(("no assembly", "assembly breakdown")) else "unplaced" if h.startswith("not placed")
                        else "estimate only (several candidates)")
            how = collections.Counter(how_of(x) for x in outs["dr"])
            rev.append([proj, "drawing breakdown", f"{len(outs['dr'])} drawing rows (= Native 2D Drawings {n['DRAWING']} + PDF Drawings {pdf_drawings}); which assembly: "
                        + ", ".join(f"{k}: {v}" for k, v in how.most_common())])
        if self.copies == "exclude":
            self.deferred[proj] = outs                   # may still be dropped when the project turns out to be a copy
        else:
            self._emit(outs)

    def _emit(self, outs):
        for r in outs["asm"]:
            self.sinks.assemblies.add(r)
        for r in outs["dr"]:
            self.sinks.drawings.add(r)
        for r in outs["ev"]:
            self.sinks.evidence.add(r if isinstance(self.sinks.evidence, MemorySink) else [r[h] for h in EVIDENCE_HEADER])
        self.confidence += outs["conf"]

    # ------------------------------------------------------------------------------------------ the whole profile
    def run(self, runs):
        names = collections.Counter(p for r in runs for p in r["projects"])
        if any(r["links"] for r in runs):
            self.universe = self.build_universe(runs, names)
        if self.unique and self.unique_scope == "all":   # needs every project at once (biggest keeps the content): not streamable
            groups = [g for g in self.groups(runs, names)]
            seen_all = set()
            for gname, grows, _p in sorted(groups, key=lambda g: (-len(g[1]), g[0].lower())):
                mine = set()
                for r in grows:
                    if r["fp"]:
                        if r["fp"] in seen_all:
                            r["dup"] = True
                        mine.add(r["fp"])
                seen_all |= mine
            for gname, grows, parents in sorted(groups, key=lambda g: g[0].lower()):
                self.finish(gname, grows, parents)
        else:
            for gname, grows, parents in self.groups(runs, names):
                self.finish(gname, grows, parents)
        copy_of = detect_copies_from(self.fp_total, self.fp_cnt) if self.copies != "keep" else {}
        for proj, (orig, share, nfiles) in copy_of.items():
            rev = self.review_proj[proj]
            slot = next((x for x in rev if x[1] == "possible copy" and x[2] is None), None)
            if self.copies == "exclude":
                self.review_proj[proj] = [[proj, "possible copy", f"EXCLUDED from Project Compiled: {share:.0%} of its {nfiles} files match files in '{orig}'"]]
                self.compiled.pop(proj, None)
                self.stats.pop(proj, None)
                self.deferred.pop(proj, None)
            else:
                self.stats[proj][19:21] = [orig, f"{share:.0%}"]
                if self.copies == "flag" and slot is not None:
                    slot[2] = (f"{share:.0%} of its {nfiles} files match files in '{orig}' (same size and modified date, or identical content when the profile has hashes) - likely a "
                               "backup or copy; counts include it (use --copies exclude to drop it, --unique --unique-scope all to count each content once)")
        for proj in sorted(self.deferred, key=str.lower):
            self._emit(self.deferred[proj])
        for proj, rev in self.review_proj.items():
            rev[:] = [x for x in rev if x[2] is not None]
        review = []
        for proj in sorted(self.review_proj, key=str.lower):
            review += self.review_proj[proj]
        review = self.review_split + review
        if len(self.unmapped) <= 40:
            for (proj, ext), k in sorted(self.unmapped.items()):
                review.append([proj, "unmapped extension", f".{ext} x{k} - add it to extensions.csv"])
        else:                                                    # many: one line per extension (the biggest 40), the rest is in Unmapped_Extensions.csv
            per_ext = collections.Counter()
            projs = collections.defaultdict(set)
            for (proj, ext), k in self.unmapped.items():
                per_ext[ext] += k
                projs[ext].add(proj)
            for ext, k in per_ext.most_common(40):
                review.append(["(all projects)", "unmapped extension", f".{ext} x{k} in {len(projs[ext])} project(s) - add it to extensions.csv"])
            if len(per_ext) > 40:
                review.append(["(all projects)", "unmapped extension", f"{len(per_ext) - 40} more extension(s) not listed here ({len(self.unmapped)} project/extension pairs) - "
                               "see Unmapped_Extensions.csv"])
        for (proj, was, now), k in sorted(self.sw_by_content.items()):
            review.append([proj, "software by content", f"{k} file(s) {('with an extension that does not name one program (.asm .prt ...)' if was == '-' else 'whose extension says ' + was)}"
                           f" are {now} files judging by what is inside them: counted as {now}"])
        for (proj, now), k in sorted(self.native_unknown_ext.items()):
            review.append([proj, "native file, other extension", f"{k} file(s) look like {now} native files by content but their extension is not a native CAD "
                           "extension in extensions.csv, so they are NOT counted as parts / assemblies - rename them or add the extension"])
        bad = collections.defaultdict(lambda: [collections.Counter(), collections.Counter()])
        for run in runs:
            folder = run["info"].get("folder_name", run["zip"])
            for r in run.get("unreadable") or []:
                proj = r["project"] if names[r["project"]] == 1 else f"{folder}/{r['project']}"
                self.unreadable_rows.append([proj, r["kind"], r["path"], r["reason"]])
                bad[proj][0][r["kind"]] += 1
                bad[proj][1][r["reason"]] += 1
        for proj, (kinds, why) in sorted(bad.items(), key=lambda kv: kv[0].lower()):
            review.append([proj, "unreadable", f"{kinds['folder']} folder(s) and {kinds['file']} file(s) could not be read ("
                           + ", ".join(f"{k} x{v}" for k, v in why.most_common(3)) + ") - what is inside them is NOT in the counts; grant read access or "
                           "re-run for those; the list is in Unreadable.csv"])
        if self.lost_links:
            review.append(["(all projects)", "references", f"{self.lost_links} reference row(s) in Links.csv could not be matched to a project "
                           "(out of order) and were ignored"])
        total_n, unm_n = self.total_n, sum(v[0] for v in self.unmapped_info.values())
        unm_b = sum(v[1] for v in self.unmapped_info.values())
        tot_b = self.total_b or 1
        sig_n = sum(self.sig_classified.values())
        review.insert(0, ["(all projects)", "categorisation",
                          f"{total_n - sig_n - unm_n:,} of {total_n:,} files ({100 * (total_n - sig_n - unm_n) / max(1, total_n):.1f}%) classified by extension, "
                          f"{sig_n:,} by file content (unknown extension but a recognisable STEP/DXF/DWG/PDF/ZIP/... start), {unm_n:,} unmapped "
                          f"({100 * unm_b / tot_b:.1f}% of bytes) - see Unmapped_Extensions.csv"
                          + (f"; by content: {', '.join(f'.{e} x{k}' for e, k in self.sig_classified.most_common(5))}" if sig_n else "")])
        unm_rows = [[e, v[0], round(v[1] / 1e6, 1), len(v[2]), "; ".join(f"{t} x{k}" for t, k in v[3].most_common(3))]
                    for e, v in sorted(self.unmapped_info.items(), key=lambda kv: -kv[1][0])]
        order = sorted(self.compiled, key=str.lower)
        return ([self.compiled[p] for p in order], [self.stats[p] for p in order], review, unm_rows, total_n)


def compile_runs(runs, ext_map, soft, unique, pdf_mode, estimate=False, project_by="folder", min_project_files=25,
                 unique_scope="project", copies="flag"):
    """In-memory convenience wrapper (tests, small profiles): -> (compiled, file_rows, stats, review, assemblies, evidence)."""
    sinks = Sinks()
    eng = Engine(ext_map, soft, unique, pdf_mode, sinks, estimate, project_by, min_project_files, unique_scope, copies)
    compiled, stats, review, unm_rows, _n = eng.run(runs)
    key = lambda p: p.lower()
    file_rows = sorted(sinks.files.rows, key=lambda r: key(r["project"]))
    assemblies = sorted(sinks.assemblies.rows, key=lambda r: key(r[0]))
    ev_rows = sorted(sinks.evidence.rows, key=lambda r: key(r["project"]))
    conf = sorted(eng.confidence, key=lambda r: key(r["project"]))
    drawings = sorted(sinks.drawings.rows, key=lambda r: key(r[0]))
    return compiled, file_rows, stats, review, assemblies, {"rows": ev_rows, "projects": conf, "unmapped": unm_rows, "drawings": drawings}


EVIDENCE_HEADER = ["project", "assembly", "refs_total", "refs_resolved", "refs_external", "refs_ambiguous", "refs_unresolved", "parts_link", "parts_folder",
                   "parts_spread", "parts_even", "other_by_link", "other_by_name", "other_by_folder"]
CONFIDENCE_HEADER = ["project", "confidence", "why", "assemblies", "parts", "parts_link", "parts_folder", "parts_spread", "parts_even",
                     "refs_total", "refs_resolved", "refs_external", "refs_ambiguous", "refs_unresolved", "shared_parts", "assemblies_without_refs",
                     "others_link", "others_name", "others_folder_or_even", "unresolved_examples", "folder_rule_agrees"]


UNMAPPED_HEADER = ["ext", "files", "MB", "projects", "header_tags_seen"]




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




def write_small_csvs(out, compiled, stats, review, confidence, unmapped):
    def dump(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows([_csv_safe(v) for v in r] for r in rows)
    dump("Project_Compiled.csv", HEADINGS, compiled)
    dump("Project_Stats.csv", STATS_HEADER, stats)
    dump("Review.csv", ["project", "topic", "detail"], review)
    dump("By_Domain.csv", BY_DOMAIN_HEADER, by_domain_rows(compiled, stats))
    if unmapped:
        dump("Unmapped_Extensions.csv", UNMAPPED_HEADER, unmapped)
    if confidence:
        dump("Assembly_Confidence.csv", CONFIDENCE_HEADER, [[r[h] for h in CONFIDENCE_HEADER] for r in confidence])


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v


def _sheet(wb, title, header, rows, color="FF181F34", numeric=()):
    """One write-only sheet (rows are streamed to disk by openpyxl, not held in memory). Column widths come from the first 200 rows."""
    ws = wb.create_sheet(title)
    first, it = [], iter(rows)
    for r in it:
        first.append(r)
        if len(first) >= 200:
            break
    for j, h in enumerate(header, 1):
        w = max([len(str(h))] + [len(str(r[j - 1])) for r in first if j - 1 < len(r)])
        ws.column_dimensions[get_column_letter(j)].width = min(max(w + 2, 12), 60)
    head = []
    for h in header:
        c = WriteOnlyCell(ws, value=h)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=color)
        head.append(c)
    ws.append(head)
    for r in itertools.chain(first, it):
        cells = []
        for j, v in enumerate(r):
            if j in numeric and v != "":
                v = _num(v)
            if isinstance(v, str) and v[:1] == "=":                  # text that merely looks like a formula stays text
                c = WriteOnlyCell(ws, value=v)
                c.data_type = "s"
                v = c
            cells.append(v)
        ws.append(cells)


def _sheets(wb, title, header, sink, max_sheets, numeric=()):
    """A big tab: one sheet per CSV part (Excel holds at most 1,048,576 rows per sheet), at most `max_sheets` of them; returns how many
    parts did not fit (they are only in the CSV files)."""
    for k in range(min(len(sink.parts), max_sheets)):
        _sheet(wb, title if k == 0 else f"{title} ({k + 1})", header, sink.rows(k), numeric=numeric)
    return max(0, len(sink.parts) - max_sheets)


BY_DOMAIN_HEADER = ["Domain", "Projects", "Files", "Size_GB", "Part Files", "Assembly Files", "Native 2D Drawings", "PDF Drawings", "Neutral 3D Files",
                    "Drawing Sheets", "Projects (names)"]


def by_domain_rows(compiled, stats):
    """One row per domain: how many projects and what they hold. The domain is the label of each project (Project Compiled, column 3)."""
    st = {r[0]: r for r in stats}
    H = STATS_HEADER
    agg = {}
    for c in compiled:
        d = agg.setdefault(c[2] or "Unclassified", {"p": [], "files": 0, "bytes": 0, "n": [0, 0, 0, 0, 0], "sheets": 0})
        d["p"].append(c[0])
        for i, col in enumerate((4, 5, 6, 7, 8)):
            d["n"][i] += int(c[col] or 0)
        r = st.get(c[0])
        if r:
            d["files"] += int(r[1] or 0)
            d["bytes"] += int(r[15] or 0)
            d["sheets"] += int(r[H.index("drawing_sheets")] or 0)
    order = sorted(agg, key=lambda k: (k == "Unclassified", -len(agg[k]["p"]), k))
    return [[k, len(agg[k]["p"]), agg[k]["files"], round(agg[k]["bytes"] / 1e9, 2), *agg[k]["n"], agg[k]["sheets"],
             "; ".join(sorted(agg[k]["p"], key=str.lower)[:12]) + (f" ... (+{len(agg[k]['p']) - 12})" if len(agg[k]["p"]) > 12 else "")] for k in order]


def _filtered_tabs(wb, title, header, sink, keep, max_sheets, numeric=()):
    """Like _sheets() but only the rows of the given projects (row[0]); at most `max_sheets` sheets of 1,000,000 rows."""
    it = (r for k in range(len(sink.parts)) for r in sink.rows(k) if r[0] in keep)
    for k in range(max_sheets):
        chunk = itertools.islice(it, 1_000_000)
        first = next(chunk, None)
        if first is None and k > 0:
            break
        _sheet(wb, title if k == 0 else f"{title} ({k + 1})", header, itertools.chain([first] if first is not None else [], chunk), numeric=numeric)


def write_domain_workbooks(out, compiled, stats, review, confidence, sinks, max_sheets=3):
    """The split by domain: by-domain/compiled_<Domain>.xlsx for every domain, holding the usual tabs for that domain's projects only
    (Project Compiled, Assembly Breakdown, Drawing Breakdown, Project Stats, Assembly Confidence / Evidence, Review). File Level stays in the
    main workbook. -> {domain: number of projects}."""
    dom = {c[0]: (c[2] or "Unclassified") for c in compiled}
    domains = sorted(set(dom.values()), key=lambda d: (d == "Unclassified", d))
    if len(domains) < 2 or not Workbook:
        return {}
    folder = out / "by-domain"
    folder.mkdir(exist_ok=True)
    done = {}
    for d in domains:
        keep = {p for p, x in dom.items() if x == d}
        wb = Workbook(write_only=True)
        _sheet(wb, "Project Compiled", HEADINGS, [c for c in compiled if c[0] in keep], numeric=set(range(4, 9)))
        _filtered_tabs(wb, "Assembly Breakdown", ASSEMBLY_HEADINGS, sinks.assemblies, keep, max_sheets, numeric={2, 3, 4, 5})
        _filtered_tabs(wb, "Drawing Breakdown", DRAWING_HEADINGS, sinks.drawings, keep, max_sheets, numeric={4, 5})
        _sheet(wb, "Project Stats", STATS_HEADER, [r for r in stats if r[0] in keep])
        conf = [r for r in confidence if r["project"] in keep]
        if conf:
            _sheet(wb, "Assembly Confidence", CONFIDENCE_HEADER, [[r[h] for h in CONFIDENCE_HEADER] for r in conf])
            _filtered_tabs(wb, "Assembly Evidence", EVIDENCE_HEADER, sinks.evidence, keep, max_sheets, numeric=set(range(2, 14)))
        _sheet(wb, "Review", ["project", "topic", "detail"], [r for r in review if r[0] in keep])
        wb.save(folder / f"compiled_{re.sub(r'[^A-Za-z0-9_-]+', '_', d)}.xlsx")
        done[d] = len(keep)
    return done


def write_workbook(path, compiled, stats, review, confidence, unmapped, sinks, max_sheets=3):
    wb = Workbook(write_only=True)
    _sheet(wb, "Project Compiled", HEADINGS, compiled, numeric=set(range(4, 9)))
    left = {}
    left["Assembly Breakdown"] = _sheets(wb, "Assembly Breakdown", ASSEMBLY_HEADINGS, sinks.assemblies, max_sheets, numeric={2, 3, 4, 5})
    left["Drawing Breakdown"] = _sheets(wb, "Drawing Breakdown", DRAWING_HEADINGS, sinks.drawings, max_sheets, numeric={4, 5})
    left["File Level"] = _sheets(wb, "File Level", FILE_HEADER, sinks.files, max_sheets, numeric={5})
    _sheet(wb, "Project Stats", STATS_HEADER, stats)
    _sheet(wb, "By Domain", BY_DOMAIN_HEADER, by_domain_rows(compiled, stats), numeric=set(range(1, 10)))
    if confidence:
        _sheet(wb, "Assembly Confidence", CONFIDENCE_HEADER, [[r[h] for h in CONFIDENCE_HEADER] for r in confidence])
        left["Assembly Evidence"] = _sheets(wb, "Assembly Evidence", EVIDENCE_HEADER, sinks.evidence, max_sheets, numeric=set(range(2, 14)))
    if unmapped:
        _sheet(wb, "Unmapped Extensions", UNMAPPED_HEADER, unmapped)
    for tab, n in left.items():
        if n:
            review.append(["(all projects)", "excel row limit", f"'{tab}' has more rows than {max_sheets} Excel sheets hold: the last {n} part(s) are only in the "
                           f"CSV files ({tab.replace(' ', '_')}_*.csv); raise --xlsx-max-sheets to put them in the workbook too"])
    _sheet(wb, "Review", ["project", "topic", "detail"], review)
    wb.save(path)


# ---------------------------------------------------------------------------------------------------- step 1 (quick count) zips
def _zip_list(paths):
    out = []
    for p in paths:
        p = Path(p)
        out += sorted(z for z in p.glob("*.zip") if not z.name.startswith("run_stats_")) if p.is_dir() else [p]
    return out


def is_quick_zip(path):
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        return "Extensions_All.csv" in names and "Files.csv" not in names
    except (zipfile.BadZipFile, OSError):
        return False


QUICK_BY_EXT = ["extension", "class", "native_software", "domain", "files", "size_GB", "share_of_files_pct", "share_of_size_pct"]
QUICK_BY_PROJECT = ["project", "files", "size_GB", "parts", "assemblies", "native_2d_drawings", "neutral_3d", "pdf", "other_files", "top_extensions"]
QUICK_OVERVIEW = ["class", "meaning", "files", "size_GB", "share_of_files_pct"]
CLASS_MEANING = {"PART": "native part / single-file model", "ASSEMBLY": "native assembly", "DRAWING": "native 2D drawing", "NEUTRAL3D": "neutral 3D exchange file (STEP, IGES, STL ...)",
                 "PDF": "PDF", "SIM": "simulation", "DOC": "document", "DATA": "data / backup / log", "IMG": "image", "ARCHIVE": "archive", "OTHER": "other",
                 "UNMAPPED": "extension not in extensions.csv"}


def compile_quick(paths, ext_map, soft, out):
    """Step 1 output (quick_*.zip of the profiler) -> a simple workbook: how many files of each type and class, per project and overall."""
    rd = lambda zf, n: list(csv.DictReader(io.StringIO(zf.read(n).decode("utf-8-sig"))))
    runs = []
    for z in paths:
        with zipfile.ZipFile(z) as zf:
            info = {r["key"]: r["value"] for r in rd(zf, "Run_info.csv")}
            runs.append((info.get("folder_name", Path(z).stem), rd(zf, "Extensions.csv")))
    owners = collections.defaultdict(set)                           # project name -> the runs that have it (a clash gets the run's name as prefix)
    for folder, rows in runs:
        for r in rows:
            owners[r["project"]].add(folder)
    by_ext, by_proj = collections.defaultdict(lambda: [0, 0]), {}
    for folder, rows in runs:
        for r in rows:
            proj = r["project"] if len(owners[r["project"]]) == 1 else f"{folder}/{r['project']}"
            n, b, ext = int(r["file_count"]), int(r["total_bytes"]), r["ext"]
            by_ext[ext][0] += n
            by_ext[ext][1] += b
            P = by_proj.setdefault(proj, {"files": 0, "bytes": 0, "cls": collections.Counter(), "ext": collections.Counter()})
            m = ext_map.get(ext)
            cls = m["class"] if m else "UNMAPPED"
            P["files"] += n; P["bytes"] += b; P["cls"][cls] += n; P["ext"][ext] += n
    total_n = sum(v[0] for v in by_ext.values()) or 1
    total_b = sum(v[1] for v in by_ext.values()) or 1
    ext_rows, cls_tot = [], collections.defaultdict(lambda: [0, 0])
    for ext, (n, b) in sorted(by_ext.items(), key=lambda kv: -kv[1][0]):
        m = ext_map.get(ext)
        cls = m["class"] if m else "UNMAPPED"
        sw = m["software"] if m else ""
        ext_rows.append([ext, cls, soft[sw]["display"] if sw in soft else sw, (m["domain"] if m else "") or (soft[sw]["domain"] if sw in soft else ""),
                         n, round(b / 1e9, 3), round(100 * n / total_n, 2), round(100 * b / total_b, 2)])
        cls_tot[cls][0] += n
        cls_tot[cls][1] += b
    cls_rows = [[c, CLASS_MEANING.get(c, ""), n, round(b / 1e9, 3), round(100 * n / total_n, 2)] for c, (n, b) in sorted(cls_tot.items(), key=lambda kv: -kv[1][0])]
    proj_rows = []
    for proj, P in sorted(by_proj.items(), key=lambda kv: kv[0].lower()):
        c = P["cls"]
        proj_rows.append([proj, P["files"], round(P["bytes"] / 1e9, 3), c["PART"], c["ASSEMBLY"], c["DRAWING"], c["NEUTRAL3D"], c["PDF"],
                          P["files"] - c["PART"] - c["ASSEMBLY"] - c["DRAWING"] - c["NEUTRAL3D"] - c["PDF"],
                          ", ".join(f".{e} {k}" for e, k in P["ext"].most_common(5))])
    def dump(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(header); w.writerows([_csv_safe(v) for v in r] for r in rows)
    bad = []
    for z in paths:
        with zipfile.ZipFile(z) as zf:
            if "Unreadable.csv" in zf.namelist():
                bad += [[r["project"], r["kind"], r["path"], r["reason"]] for r in rd(zf, "Unreadable.csv")]
    if bad:
        dump("Quick_Unreadable.csv", ["project", "kind", "path", "reason"], bad)
    dump("Quick_Overview.csv", QUICK_OVERVIEW, cls_rows)
    dump("Quick_By_Extension.csv", QUICK_BY_EXT, ext_rows)
    dump("Quick_By_Project.csv", QUICK_BY_PROJECT, proj_rows)
    if Workbook:
        wb = Workbook(write_only=True)
        _sheet(wb, "Overview", QUICK_OVERVIEW, cls_rows)
        _sheet(wb, "By Extension", QUICK_BY_EXT, ext_rows)
        _sheet(wb, "By Project", QUICK_BY_PROJECT, proj_rows)
        wb.save(out / "quick_count.xlsx")
    return len(proj_rows), total_n, cls_rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--unique", action="store_true", help="count each content hash once (per project, or see --unique-scope)")
    ap.add_argument("--unique-scope", choices=["project", "all"], default="project",
                    help="with --unique: 'project' (default) or 'all' = once in the whole profile (copies of a project elsewhere vanish; "
                         "this one needs memory for the whole profile)")
    ap.add_argument("--copies", choices=["flag", "exclude", "keep"], default="flag",
                    help="projects that are mostly (>= 80%%) identical to a bigger project: flag them in Review (default), exclude them "
                         "from Project Compiled, or keep = do not look")
    ap.add_argument("--pdf-drawings", choices=["auto", "none", "all"], default="auto",
                    help="auto (default): by file name when the profile has names (level >= 1), else none; none; all")
    ap.add_argument("--estimate-assemblies", action="store_true", help=argparse.SUPPRESS)     # kept for old command lines: now the default
    ap.add_argument("--no-split-by-domain", action="store_true",
                    help="do not write the per-domain workbooks (by-domain/compiled_<Domain>.xlsx); they are written by default when the projects fall into "
                         "more than one domain")
    ap.add_argument("--no-assembly-breakdown", action="store_true",
                    help="leave the Assembly Breakdown tab empty (default: fill it, with method and confidence reported)")
    ap.add_argument("--project-by", choices=["auto", "structure", "folder"], default="auto",
                    help="auto (default): split each folder into sub-projects by root assembly when the profile has references "
                         "(level 2) and enough of them resolve, otherwise keep the folder; structure: same but always tries; "
                         "folder: never split")
    ap.add_argument("--min-project-files", type=int, default=25,
                    help="with --project-by structure: sub-projects smaller than this are merged into '<project> / (other)'")
    ap.add_argument("--xlsx-max-sheets", type=int, default=3,
                    help="File Level / Assembly Breakdown / Assembly Evidence are split into sheets of 1,000,000 rows (Excel's limit is 1,048,576); "
                         "at most this many sheets per tab go into compiled.xlsx, the rest stay in the CSV parts (default 3)")
    ap.add_argument("--csv-part-rows", type=int, default=1_000_000, help=argparse.SUPPRESS)
    ap.add_argument("--map-dir", default=str(HERE / "mapping"))
    ap.add_argument("--out", help="output dir (default: ./compile-profiles/<timestamp> under the current folder)")
    a = ap.parse_args()

    ext_map, soft = load_mapping(a.map_dir)
    out = Path(a.out) if a.out else Path.cwd() / "compile-profiles" / datetime.now().strftime("%Y%m%d-%H%M%S")
    zips = _zip_list(a.inputs)
    quick_zips = [z for z in zips if is_quick_zip(z)]
    if quick_zips:                                                   # step 1 output: the simple extension-count workbook
        out.mkdir(parents=True, exist_ok=True)
        nproj, nfiles, cls_rows = compile_quick(quick_zips, ext_map, soft, out)
        print(f"{len(quick_zips)} quick-count zip(s) -> {nproj} project(s), {nfiles:,} files")
        for r in cls_rows[:8]:
            print(f"   {r[0]:10} {r[2]:>10,} files  {r[3]:>8} GB  {r[4]}%")
        print(f"Wrote files in {out}")
        if len(quick_zips) == len(zips):
            return
        a.inputs = [str(z) for z in zips if z not in quick_zips]
    runs = load_profiles(a.inputs)
    out.mkdir(parents=True, exist_ok=True)
    sinks = Sinks(CsvSink(out, "File_Level", FILE_HEADER, a.csv_part_rows),
                  CsvSink(out, "Assembly_Breakdown_ESTIMATE", ASSEMBLY_HEADINGS, a.csv_part_rows),
                  CsvSink(out, "Assembly_Evidence", EVIDENCE_HEADER, a.csv_part_rows),
                  CsvSink(out, "Drawing_Breakdown", DRAWING_HEADINGS, a.csv_part_rows))
    eng = Engine(ext_map, soft, a.unique, a.pdf_drawings, sinks, not a.no_assembly_breakdown, a.project_by, a.min_project_files,
                 a.unique_scope, a.copies)
    compiled, stats, review, unmapped, n_files = eng.run(runs)
    sinks.files.close()
    sinks.assemblies.close(keep_empty=False)
    sinks.evidence.close(keep_empty=bool(eng.confidence))
    sinks.drawings.close(keep_empty=False)
    write_small_csvs(out, compiled, stats, review, eng.confidence, unmapped)
    if eng.unreadable_rows:
        with open(out / "Unreadable.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["project", "kind", "path", "reason"])
            w.writerows([_csv_safe(v) for v in r] for r in eng.unreadable_rows)
    if Workbook:
        write_workbook(out / "compiled.xlsx", compiled, stats, review, eng.confidence, unmapped, sinks, a.xlsx_max_sheets)
        if not a.no_split_by_domain:
            split = write_domain_workbooks(out, compiled, stats, review, eng.confidence, sinks, a.xlsx_max_sheets)
            if split:
                print("split by domain -> by-domain/: " + ", ".join(f"{d} ({n} projects)" for d, n in split.items()))
            else:
                print("split by domain: skipped (all projects share one domain)")
    else:
        print("(openpyxl not installed - wrote CSVs only)")
    n_asm = sinks.assemblies.total

    print(f"{len(runs)} profile zip(s) -> {len(compiled)} project(s), {n_files} files "
          f"({'unique hashes' if a.unique else 'all files'} counted; PDF drawings: {a.pdf_drawings}; "
          f"assembly rows: {'ESTIMATED, ' + str(n_asm) if not a.no_assembly_breakdown else 'not produced'})")
    for r in compiled[:200]:
        print("  ", r)
    if len(compiled) > 200:
        print(f"   ... and {len(compiled) - 200} more projects (see Project_Compiled.csv)")
    print(f"Review items: {len(review)}  (see Review)\nWrote files in {out}")


if __name__ == "__main__":
    main()
