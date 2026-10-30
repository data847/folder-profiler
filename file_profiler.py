"""Folder Profiler — collects file METRICS only. It never copies, uploads or stores file contents, and (since 1.6) it never
hashes a file: nothing is read in full except, at detail level 2, the assembly / drawing / STEP files whose references are listed.

Two steps: `--quick` = step 1, a very fast count of files per type and size (nothing is opened; writes quick_*.zip); no flag = step 2, the
full profile described below.

What it writes (one zip of CSVs, nothing else):
  Files.csv       one row per file: project, file_no, ext, size_bytes, modified, depth, header_signature, dir_id, asm_name,
                  (rel_path at level >= 1), sheets, sheets_conf, sheets_status (level 2)
  Projects.csv    one row per project folder: counts, bytes, date range
  Extensions.csv  one row per project + extension: counts, bytes
  Dirs.csv        the folder tree (anonymous ids; names at level >= 1)
  Links.csv       (level 2) names of files referenced inside assemblies, drawings and STEP files
  Run_info.csv    tool version, date, OS, folder name, totals, read errors, time per step
  Run_log.csv, Run_phases.csv, Run_metrics.csv, System_info.csv   how the run went and what machine it ran on (also written
                  separately as run_stats_*.zip, safe to share: no file or folder names)

What it does NOT write: folder names or paths below the project name (levels 0), file names (except ASSEMBLY files at level 0),
or any file text.

Assembly Breakdown support: each file carries `dir_id`, an anonymous folder identifier (salted hash of the folder name; the salt is
thrown away, so folder names cannot be recovered) and Dirs.csv holds the folder tree as ids only. Files whose extension is an
assembly format (sldasm asm iam catproduct) also carry `asm_name` - the real file name, or an anonymous code such as
ASM-3F9A1C2B.SLDASM with --anonymize-assembly-names. No other file name is written at level 0.

How the file contents are touched (and nothing else):
  * header_signature - computed from the first few KB of certain file types and reduced to a tag from the fixed lists in this
    file (e.g. "STEP:AP214:SolidWorks", "DXF:AC1027:3D", "PDF"). The raw header text is never kept.
  * level 2 only: assembly / drawing / STEP files are read to list the FILE NAMES they reference, and AutoCAD drawings (.dwg / .dxf) to
    count their sheets (layouts) - a number only.

Resumable: progress is saved in <out>/.resume/ after every chunk of files; if the run stops for any reason, start it again with the
same settings and it continues from the last saved chunk (--fresh ignores saved progress). No depth limit on folders.

Standard library only. No network code. Files are opened read-only.

Usage:  python file_profiler.py [folder] [--out DIR] [--name LABEL]
        (no folder given -> you are asked for one)
Detail levels: 0 = metrics only (default) | 1 = + file/folder names and paths (rel_path) | 2 = + Links.csv, the NAMES of
files that assemblies/drawings/STEP files reference (read with cad_links.py; nothing else is taken from inside files).
Compressed files (zip, rar, 7z, tar ...) are counted as ONE file each; nothing is looked inside them.
--project-depth N makes the folders N levels down the projects (any N).
Docker: see Dockerfile / run-docker.* - read-only input mount, no network, non-root user.
"""
import collections
import csv
import errno
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:                                   # detail level 2 only; the profiler works without them
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # also under `python -I` and when frozen
    import cad_links
except ImportError:
    cad_links = None
try:
    import dwg_sheets
except ImportError:
    dwg_sheets = None
import sysmetrics
import diag
from sysmetrics import now_utc

VERSION = "1.8"
try:
    import name_anon
except ImportError:          # optional feature: the profiler works without it, the option is then refused
    name_anon = None
HEAD_BYTES = 4096            # header peek (not for NO_HEAD_EXTS: see below)
DXF_SCAN_BYTES = 1_048_576   # DXF only: scan up to 1 MB for 3D entity names (flag only, text discarded)

# ---- closed vocabularies: every header tag is built from these, never from file text ----
CAD_SYSTEMS = [  # (substring in STEP header, lowercase) -> tag
    ("solidworks", "SolidWorks"), ("fusion", "Fusion360"), ("inventor", "Inventor"),
    ("catia", "CATIA"), ("creo", "Creo"), ("pro/engineer", "Creo"), ("pro_engineer", "Creo"),
    ("unigraphics", "NX"), ("siemens nx", "NX"), ("nx ", "NX"), ("solid edge", "SolidEdge"),
    ("solidedge", "SolidEdge"), ("freecad", "FreeCAD"), ("onshape", "Onshape"),
    ("autocad", "AutoCAD"), ("rhino", "Rhino"), ("opencascade", "OpenCascade"),
    ("open cascade", "OpenCascade"), ("sketchup", "SketchUp"), ("revit", "Revit"),
    ("archicad", "ArchiCAD"), ("altium", "Altium"), ("kicad", "KiCad"), ("eagle", "Eagle"),
    ("calculix", "CalculiX"), ("ansys", "ANSYS"), ("abaqus", "Abaqus"),
]
ACAD_VERSIONS = {  # DWG/DXF $ACADVER codes -> versions (closed list)
    "AC1006", "AC1009", "AC1012", "AC1014", "AC1015", "AC1018", "AC1021", "AC1024",
    "AC1027", "AC1032",
}
DXF_3D_ENTITIES = (b"3DSOLID", b"3DFACE", b"MESH", b"SURFACE", b"BODY", b"REGION")
TEXT_FORMATS = [  # (lowercase pattern in first 4 KB, tag) - first match wins; tags are fixed strings
    (b"(kicad_pcb", "EDA:KiCad"), (b"(kicad_sch", "EDA:KiCad"), (b"(kicad_symbol_lib", "EDA:KiCad"), (b"(kicad_wks", "EDA:KiCad"),
    (b"1upgm               calculix", "SIM:CalculiX"),
    (b"summary of c0nvergence", "SIM:CalculiX"),
    (b"summary of job information", "SIM:CalculiX"),
    (b"calculix", "SIM:CalculiX"),
    (b"abaqus", "SIM:Abaqus"),
    (b"$meshformat", "MESH:GMSH"),
    (b"<vtkfile", "VTK:XML"),
]
# Extensions whose header tag nobody uses: images, video, audio, documents, archives, fonts, web files, binaries and SketchUp (which has no
# content tag at all). Opening a file just to peek at 4 KB costs one network round trip each (about 0.37 s on a file server); on a real
# 385,000-file run those opens were 68% of the time and 62% of the files were of these types. They are listed with sig NOTAG and are NOT opened.
# The compiler classifies every one of these extensions by name (tests/test_notag_exts.py checks that). PROFILER_READ_ALL_HEADERS=1 reads them anyway.
NO_HEAD_EXTS = frozenset("""jpg jpeg jpe png gif bmp tif tiff webp heic heif ico psd psb cr2 nef arw dng raw svg
    mp4 mov avi mkv wmv flv m4v webm mp3 wav m4a aac flac ogg pdf doc docx xls xlsx ppt pptx zip rar 7z gz tar tgz
    dll exe msi so ttf otf woff woff2 js css map html htm skp skb""".split())
SHEET_EXTS = {"dwg", "dxf"}               # level 2: AutoCAD drawings - the number of sheets (layouts) is counted, with a confidence
LINK_READ_EXTS = {"sldasm", "asm", "pwd", "iam", "slddrw", "dft", "idw", "step", "stp", "catproduct", "catdrawing", "drw", "prt"}   # level 2: assemblies, drawings, STEP
def _load_ext_classes():
    """extension -> class, from ext_classes.csv next to this file (a copy of the compiler's extensions.csv; tests/test_header_policy.py keeps them equal).
    Without the file every header is read as before (only NO_HEAD_EXTS are skipped)."""
    out = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext_classes.csv"), encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                e, _, c = line.partition(",")
                if e:
                    out[e] = c
    except OSError:
        pass
    return out


EXT_CLASSES = _load_ext_classes()
CAD_CLASSES = frozenset(("PART", "ASSEMBLY", "DRAWING", "NEUTRAL3D", "SIM", "EDA"))
GENERIC_EXTS = frozenset("(none) bak tmp temp dat bin old orig back backup txt xml json".split())     # can hold anything (a backup of a drawing, a renamed STEP)
HIT_PREFIXES = ("STEP:", "IFC:", "IGES:", "DWG:", "DXF:", "STL:", "OLE2:SolidEdge", "OLE2:Inventor", "OLE2:Revit", "CATIA:", "CREO", "EDA:", "SIM:", "MESH:", "VTK:")
SAMPLE_PER_CHUNK = max(1, int(os.environ.get("PROFILER_SAMPLE_PER_CHUNK", "3")))


def head_policy(ext):
    """What to do with the 4 KB header of a file of this type: 'read' (CAD / drawings / assemblies: the tag names the software and what is inside),
    'sample' (a type the compiler does not know, or a container that can hold anything: only a few per chunk are read, and a type that shows CAD
    content is read in full at the end) or 'skip' (the compiler classifies the type by its name; opening the file would add nothing)."""
    if not EXT_CLASSES:
        return "skip" if ext in NO_HEAD_EXTS else "read"
    if ext in SHEET_EXTS or ext in LINK_READ_EXTS:
        return "read"
    if ext in NO_HEAD_EXTS:
        return "skip"
    c = EXT_CLASSES.get(ext)
    if c is None:
        return "sample"
    if c in CAD_CLASSES:
        return "read"
    return "sample" if ext in GENERIC_EXTS else "skip"


ASSEMBLY_EXTS = {"sldasm", "asm", "pwd", "iam", "catproduct"}   # the only files whose NAME is recorded (Assembly Breakdown tab)
SYSTEM_JUNK = {".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db"}  # OS clutter, not project files
MAGICS = [  # (prefix, tag)
    (b"%PDF", "PDF"), (b"\x89PNG\r\n\x1a\n", "PNG"), (b"\xff\xd8\xff", "JPEG"), (b"GIF8", "GIF"),
    (b"BM", "BMP"), (b"PK\x03\x04", "ZIP"), (b"\x1f\x8b", "GZIP"), (b"7z\xbc\xaf\x27\x1c", "7Z"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE2"), (b"SQLite format 3", "SQLITE"),
    (b"MZ", "WINEXE"), (b"\x7fELF", "ELF"), (b"II*\x00", "TIFF"), (b"MM\x00*", "TIFF"),
    (b"RIFF", "RIFF"), (b"<?xml", "XML"), (b"{\\rtf", "RTF"),
    (b"V5_CFV2", "CATIA:V5"), (b"#UGC:", "CREO"),     # CATIA V5 and Pro/ENGINEER-Creo native files start with these (not yet checked on real files)
]
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OLE_APPS = [  # (entry names inside an OLE2 compound file, tag): the closed list that tells the CAD / Office applications apart
    ({"PSMroots", "PSMclustertable", "PSMuserroots", "JSitesList", "PartsLiteData", "DocVersion2", "BuildVersions", "JBlocks"}, "OLE2:SolidEdge"),
    ({"RSeDb", "RSeSegInfo", "RSeStorage"}, "OLE2:Inventor"),
    ({"BasicFileInfo", "RevitPreview4.0"}, "OLE2:Revit"),
    ({"WordDocument", "Workbook", "PowerPoint Document"}, "OLE2:Office"),
]


def signature(path, ext, size, counter=None):
    """Closed-vocabulary tag describing the file's format. Never contains file text. On UNREADABLE the reason is left in diag.take()."""
    if size == 0:
        return "EMPTY"
    def go():
        with open(path, "rb") as f:
            return _signature_file(f, ext)
    try:
        return retry(go, counter)
    except OSError as exc:
        diag.note("header", reason_of(exc))
        return "UNREADABLE"


def signature_stream(f, ext, size):
    """Same tag for an already-open binary stream."""
    if size == 0:
        return "EMPTY"
    try:
        return _signature_file(f, ext)
    except Exception as exc:
        diag.note("header", reason_of(exc))
        return "UNREADABLE"


def _ole_tag(f, head):
    """Which application wrote an OLE2 compound file: the entry names of its directory (read from the file, only the names are
    compared with a fixed list; nothing is kept). 'OLE2' when the directory is unreadable or the application is not on the list."""
    try:
        shift = int.from_bytes(head[0x1E:0x20], "little")
        dir_start = int.from_bytes(head[0x30:0x34], "little")
        if shift not in (9, 12) or dir_start >= 0xFFFFFFF0:
            return "OLE2"
        ss = 1 << shift
        f.seek((dir_start + 1) * ss)
        raw = f.read(64 * 1024)
        names = set()
        for i in range(0, len(raw) - 127, 128):
            n = int.from_bytes(raw[i + 0x40:i + 0x42], "little")
            if 2 <= n <= 64:
                names.add(raw[i:i + n - 2].decode("utf-16le", "ignore"))
        for wanted, tag in OLE_APPS:
            if names & wanted or (tag == "OLE2:SolidEdge" and any(x.startswith("JSite") for x in names)):
                return tag
    except Exception:
        pass
    return "OLE2"


def _signature_file(f, ext):
    head = f.read(HEAD_BYTES)
    low = head.lower()
    if head.startswith(OLE_MAGIC):
        return _ole_tag(f, head)
    if head.lstrip()[:5] == b"<?xml" and b"<eagle" in low:        # Eagle schematics / boards / libraries are XML with an <eagle> root
        return "EDA:Eagle"
    if b"iso-10303-21" in low[:64]:                       # STEP / Part 21
        ap = "AP"
        m = re.search(rb"file_schema\s*\(\s*\(\s*'([^']*)'", low)
        if m:
            s = m.group(1)
            if b"ifc" in s:                                   # building models share STEP's wrapper
                ifc = next((v for k, v in ((b"ifc4x3", "IFC4X3"), (b"ifc4", "IFC4"), (b"ifc2x3", "IFC2X3"),
                                           (b"ifc2x2", "IFC2X2")) if k in s), "IFC-other")
                return f"IFC:{ifc}:" + next((tag for key, tag in CAD_SYSTEMS if key.encode() in low), "other")
            ap = ("AP203" if b"config_control" in s else "AP214" if b"automotive" in s
                  else "AP242" if b"ap242" in s or b"managed_model" in s
                  else "AP203" if b"203" in s else "AP214" if b"214" in s
                  else "AP242" if b"242" in s else "AP-other")
        sysname = next((tag for key, tag in CAD_SYSTEMS if key.encode() in low), "other")
        return f"STEP:{ap}:{sysname}"
    if ext in ("igs", "iges") or (len(head) > 80 and head[72:73] == b"S" and head[73:80].strip() == b"1"):
        return "IGES:" + next((tag for key, tag in CAD_SYSTEMS if key.encode() in low), "other")
    if head[:6].startswith(b"AC") and head[:6].decode("latin-1") in ACAD_VERSIONS:
        return "DWG:" + head[:6].decode("latin-1")        # binary DWG
    if ext == "dxf" or b"$acadver" in low:
        m = re.search(rb"\$ACADVER\s+1\s+(AC\d{4})", head.replace(b"\r", b""), re.I)
        if ext == "dxf" or m:
            ver = m.group(1).decode().upper() if m else "unknown"
            ver = ver if ver in ACAD_VERSIONS else "other"
            f.seek(0)
            body = f.read(DXF_SCAN_BYTES).upper()
            return f"DXF:{ver}:{'3D' if any(t in body for t in DXF_3D_ENTITIES) else '2D'}"
    if ext == "stl" or head[:5].lower() == b"solid":
        return "STL:ascii" if head[:5].lower() == b"solid" and b"facet" in low else "STL:binary"
    for magic, tag in MAGICS:
        if head.startswith(magic):
            return tag
    for pattern, tag in TEXT_FORMATS:
        if pattern in low:
            return tag
    if head.startswith(b"    1C"):
        return "SIM:FRD"
    if low.lstrip().startswith((b"*heading", b"*node", b"**")):
        return "SIM:INP"
    if b"\x00" in head:
        return "BINARY"
    try:
        head.decode("utf-8")
        return "TEXT"
    except UnicodeDecodeError:
        return "BINARY"


class MemGate:
    """Caps how many bytes of whole-file reads (DWG sheet counting, assembly references) are in flight at once, so many workers meeting
    several huge drawings together cannot exhaust a small machine's RAM. A file bigger than the whole budget is read alone."""
    def __init__(self, budget):
        self.budget, self.used, self.cv = int(budget), 0, threading.Condition()
        self.waits = 0

    def hold(self, cost):
        gate = self

        class _Held:
            def __enter__(self_):
                self_.cost = min(int(cost), gate.budget)
                with gate.cv:
                    if gate.used + self_.cost > gate.budget:
                        gate.waits += 1
                    while gate.used + self_.cost > gate.budget:
                        gate.cv.wait()
                    gate.used += self_.cost

            def __exit__(self_, *a):
                with gate.cv:
                    gate.used -= self_.cost
                    gate.cv.notify_all()
        return _Held()


def memory_budget():
    """Bytes of file data (times a safety factor, see MEM_FACTOR) that may be held in memory at once: 20% of the installed RAM, between
    256 MB and 2 GB; PROFILER_MEM_BUDGET_MB overrides. Unknown RAM -> 512 MB."""
    env = os.environ.get("PROFILER_MEM_BUDGET_MB", "")
    if env.isdigit() and int(env) > 0:
        return int(env) * 1024 * 1024
    try:
        import sysmetrics
        total = sysmetrics.total_ram_bytes()
    except Exception:
        total = 0
    if not total:
        return 512 * 1024 * 1024
    return max(256 * 1024 * 1024, min(2 * 1024 ** 3, total // 5))


MEM_FACTOR = 3        # a DWG / assembly held in memory costs about this many times its size while it is parsed (data + decompressed copies)


def _usable_cpus():
    """CPUs this process may use (container limit and affinity mask respected), 4 when unknown."""
    try:
        import sysmetrics
        return sysmetrics.effective_cpus()
    except Exception:
        return os.cpu_count() or 4


def _peak_mb():
    try:
        import sysmetrics
        return sysmetrics.peak_memory_mb()
    except Exception:
        return ""


def default_workers(paths=None):
    env = os.environ.get("PROFILER_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    if paths and quick_threads(paths) > 4:      # a network share: every file open waits on the network, so many more readers hide the latency
        return 48
    return min(16, _usable_cpus() * 2)          # file reads release the GIL, so threads scale


def build_units(root, depth):
    """Projects are the folders `depth` levels below root (1 = folders directly inside it; any depth is allowed). Files sitting in
    the folders above that level become small non-recursive units named "(root)" / "<folder>/(files)"."""
    projects, loose, level = [], [], [("", root)]
    for _ in range(depth):
        nxt = []
        for rel, ab in level:
            try:
                items = sorted(os.listdir(ab), key=str.lower)
            except OSError as exc:
                diag.warn("project folders", rel or "(root)", f"could not be listed ({reason_of(exc)}) - it and everything below it is missing from this run")
                continue
            if any(os.path.isfile(os.path.join(ab, f)) for f in items):
                loose.append(("(root)" if not rel else f"{rel}/(files)", ab, False))
            nxt += [(f"{rel}/{d}" if rel else d, os.path.join(ab, d)) for d in items if os.path.isdir(os.path.join(ab, d))]
        level = nxt
        if not level:
            break
    projects = [(rel, ab, True) for rel, ab in level]
    return projects + loose


def lp(path):
    """Windows: prefix with \\\\?\\ so CAD trees deeper than 260 characters can be read."""
    if sys.platform != "win32" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


VERSIONED_EXTS = {"prt", "asm", "drw", "frm", "lay", "mfg", "sec", "dgm"}       # Creo / Pro-E keep versions as part.prt.7


def ext_of(name):
    if name.startswith(".") and name.count(".") == 1:
        return "(none)"
    base, ext = os.path.splitext(name)
    ext = ext.lstrip(".").lower()
    if ext.isdigit():                                    # "bracket.prt.12" is a Creo part, not a ".12" file
        inner = os.path.splitext(base)[1].lstrip(".").lower()
        if inner in VERSIONED_EXTS:
            return inner
    return ext or "(none)"


def iso(ts):
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):         # a corrupt date (NTFS can hold the year 60369): no date, never a crash
        return ""


def walk_dirs(base, recurse, on_error, counts):
    """Depth-first, deterministic (sorted) walk with NO depth limit. Yields (dirpath, rel, depth, [file DirEntry]).
    Directory links / junctions are not followed (loops, double counting) and are counted in counts['links_skipped']."""
    stack = [(base, ".", 0)]
    while stack:
        dirpath, rel, depth = stack.pop()
        def _list():
            with os.scandir(dirpath) as it:
                return sorted(it, key=lambda e: e.name)
        try:
            entries = retry(_list, counts)
        except OSError as e:
            on_error(e)
            continue
        files, subdirs = [], []
        for e in entries:
            try:
                if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                    if e.is_dir():
                        counts["links_skipped"] += 1
                        continue
                    files.append(e)
                elif e.is_dir(follow_symlinks=False):
                    subdirs.append(e)
                elif e.is_file(follow_symlinks=False):
                    files.append(e)
            except OSError as err:
                on_error(err)
        yield dirpath, rel, depth, files
        if recurse:
            for e in reversed(subdirs):
                stack.append((e.path, e.name if rel == "." else rel + os.sep + e.name, depth + 1))


def chunk_fingerprints(base, recurse, chunk_files):
    """Stat-only pass (no file is opened): one crc per chunk of files over their relative path, size and modified time. The same
    formula is used when a chunk is saved, so a resumed run can tell whether a saved project still matches the disk."""
    fps, cur, n = [], 0, 0
    for _dirpath, rel, _depth, files in walk_dirs(lp(base), recurse, lambda e: None, collections.Counter()):
        for e in files:
            if e.name.lower() in SYSTEM_JUNK or e.name.startswith("._"):
                continue
            n += 1
            try:
                st = e.stat()
                relpath = e.name if rel == "." else (rel + os.sep + e.name).replace(os.sep, "/")
                cur = zlib.crc32(f"{relpath}\0{st.st_size}\0{iso(st.st_mtime)}\n".encode("utf-8", "surrogatepass"), cur)
            except OSError:
                pass
            if n % chunk_files == 0:
                fps.append(cur)
                cur = 0
    if n % chunk_files:
        fps.append(cur)
    return fps


PIPELINE_CHUNKS = max(0, int(os.environ.get("PROFILER_PIPELINE_CHUNKS", "4")))      # chunks left in flight while the next one is listed (0 = wait for each chunk, as before)
CHUNK_FILES = 5000           # files per checkpoint: after a crash at most this many files of one project are redone
_TEST_HOOK = None            # tests set this to a callable(unit_index, chunk_index) to simulate a crash after a chunk is saved


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _write_csv_atomic(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\r\n").writerows(rows)
    os.replace(tmp, path)


def resolve_units(root, folders, label, project_depth):
    """What a run covers: -> (units, label, data_paths). Shared by the quick count and the full profile so both always agree on what a
    'project' is. units = [(project name, folder, recurse?)]."""
    if folders:
        units, used = [], set()
        for f in folders:
            f = os.path.abspath(f)
            name = os.path.basename(f.rstrip("\\/")) or "folder"
            base, k = name, 2
            while name.lower() in used:                # two picked folders can share a name
                name, k = f"{base} ({k})", k + 1
            used.add(name.lower())
            units.append((name, f, True))
        label = label or "selected-folders"
        data_paths = [u[1] for u in units]
    else:
        root = os.path.abspath(root)
        label = label or os.path.basename(root) or "folder"
        units = build_units(root, project_depth)
        data_paths = [root]
    if not units:
        raise SystemExit("That folder is empty - nothing to profile.")
    return units, label, data_paths


def quick_ext(name):
    """ext_of() with a shortcut for the ordinary case (a name that does not start with a dot and ends in .something, not a number).
    Anything unusual goes to ext_of() itself, so the answer is always the same - the tests compare both on thousands of odd names."""
    i = name.rfind(".")
    if 0 < i < len(name) - 1 and name[0] != ".":
        ext = name[i + 1:].lower()
        if not ext.isdigit():
            return ext
    return ext_of(name)


# ---- Windows network shares (Windows Server file servers, Azure File Sync, deduplication, flaky links) -------------------------------
TIER_MASK = 0x1000 | 0x40000 | 0x100000 | 0x400000     # OFFLINE | RECALL_ON_OPEN | UNPINNED (online-only) | RECALL_ON_DATA_ACCESS
DEDUP_TAG = 0x80000013                                   # reparse tag of a Windows Server Data Deduplication file
TRANSIENT_WINERRORS = {53, 59, 64, 67, 121, 1231, 1232}  # bad network path, unexpected network error, name deleted, bad share, timeout, unreachable
TRANSIENT_ERRNOS = {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN, errno.ESTALE}
RETRY_DELAYS = (0.5, 2.0, 5.0)
class PathStore:
    """Step 1's per-type file lists. Rows are kept in memory up to `mem_rows`, then appended to temp files, so memory stays flat however many
    files there are (it was about 250 bytes per file, which killed a 512 MB container at 2 million files). One writer per scan thread, no locks.
    chunks(ext) hands the rows back as (unit, folder, [(name, size, mtime), ...]); close() deletes the temp files."""

    def __init__(self, nthreads, mem_rows):
        self.mem = [collections.defaultdict(list) for _ in range(nthreads)]
        self.held = [0] * nthreads
        self.n = [collections.Counter() for _ in range(nthreads)]
        self.limit = max(1, int(mem_rows) // max(1, nthreads))
        self.dir = None
        self._mk = threading.Lock()
        self.files = [dict() for _ in range(nthreads)]          # per thread: ext -> temp file
        self.spills = 0

    def add(self, k, ext, ui, rel, lst):
        self.mem[k][ext].append((ui, rel, lst))
        self.n[k][ext] += len(lst)
        self.held[k] += len(lst)
        if self.held[k] >= self.limit:
            self._spill(k)

    def _spill(self, k):
        import tempfile
        with self._mk:                                          # two threads can spill at the same moment: one folder, not two
            if self.dir is None:
                import atexit
                self.dir = tempfile.mkdtemp(prefix="profiler_paths_")      # TMPDIR / the OS temp folder; removed by close(), and at exit if an error got in the way
                atexit.register(self.close)
        for ext, chunks in self.mem[k].items():
            fn = self.files[k].get(ext)
            if fn is None:
                fn = self.files[k][ext] = os.path.join(self.dir, f"{k}_{len(self.files[k])}.csv")
            with open(fn, "a", encoding="utf-8", errors="surrogatepass", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                for ui, rel, lst in chunks:
                    w.writerows([ui, rel, nm, sz, mt] for nm, sz, mt in lst)
        self.mem[k].clear()
        self.held[k] = 0
        self.spills += 1

    def exts(self):
        return set().union(*[set(c) for c in self.n]) if self.n else set()

    def count(self, ext):
        return sum(c.get(ext, 0) for c in self.n)

    def chunks(self, ext):
        for k in range(len(self.mem)):
            fn = self.files[k].get(ext)
            if fn:
                cur, buf = None, []
                with open(fn, "r", encoding="utf-8", errors="surrogatepass", newline="") as f:
                    for ui, rel, nm, sz, mt in csv.reader(f):
                        if cur != (int(ui), rel):
                            if buf:
                                yield cur[0], cur[1], buf
                            cur, buf = (int(ui), rel), []
                        buf.append((nm, int(sz), float(mt)))
                if buf:
                    yield cur[0], cur[1], buf
            yield from self.mem[k].get(ext, ())

    def close(self):
        if self.dir:
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None


def paths_mem_rows():
    """File-path rows Step 1 may hold in memory before spilling to disk: about a thousandth of the memory budget in rows (a 256 MB budget
    -> 268,000 rows, roughly 70 MB). PROFILER_PATHS_MEM_ROWS overrides."""
    env = os.environ.get("PROFILER_PATHS_MEM_ROWS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return memory_budget() // 1000


PATHS_MAX_FILES = int(os.environ.get("PROFILER_PATHS_MAX_FILES", "6000000"))      # paths kept in memory for Step 1's file lists (about 130 bytes each)
MAX_UNREADABLE = 20000                                   # listed in Unreadable.csv; the counts stay exact beyond this


TIERED_SHEET = {"sheets": None, "confidence": 0, "method": "not-read", "note": "cloud-tiered file, not opened", "code": "not-read-tiered"}


def _file_attrs(st):
    """Windows file attributes (0 elsewhere). A file with the TIER_MASK bits is not really on this disk: reading even its first bytes makes
    the server / cloud fetch the WHOLE file (Azure File Sync cloud tiering, OneDrive online-only files), so the profiler never opens it."""
    return getattr(st, "st_file_attributes", 0)


def is_transient(exc):
    return isinstance(exc, OSError) and (getattr(exc, "winerror", None) in TRANSIENT_WINERRORS or exc.errno in TRANSIENT_ERRNOS)


def retry(fn, counter=None):
    """fn() again after a short pause when the network dropped for a moment; anything else (permission denied, not found) is raised at once."""
    for i, delay in enumerate(RETRY_DELAYS + (None,)):
        try:
            return fn()
        except OSError as exc:
            if delay is None or not is_transient(exc):
                raise
            if counter is not None:
                counter["retries"] += 1
            time.sleep(delay)


def reason_of(exc):
    n = type(exc).__name__
    return n if n != "OSError" else f"OSError {getattr(exc, 'winerror', None) or exc.errno}"


def quick_threads(paths):
    """Threads for listing / reading. Waiting on the disk or the network dominates, so a network location (Windows: a mapped drive or a UNC
    path) gets many threads to hide the latency, a local disk only a few (more just fight over Python's lock - measured: 4 threads beat
    32 on a local SSD)."""
    remote = False
    if sys.platform == "win32":
        try:
            import ctypes
            for p in paths:
                p = os.path.abspath(p)
                if p.startswith("\\\\"):
                    remote = True
                    break
                root = os.path.splitdrive(p)[0] + "\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 4:        # DRIVE_REMOTE
                    remote = True
                    break
        except Exception:
            remote = False
    return 32 if remote else 4


def scan_tree(units, nthreads, progress=None, only=None, paths=False):
    """The threaded folder listing used by BOTH steps: how many files of each type and how big, per unit, from folder listings only (no file is
    opened). units = [(name, folder, recurse)]; only = indexes of the units to scan (None = all). No depth limit; folder links / junctions
    are not followed. A folder is listed atomically (retried as a whole after a network hiccup), so nothing is ever counted twice.
    paths=True also keeps, per file type, every file's name, size and date (taken from the stat the scan makes anyway): res["paths"] =
    a PathStore (in memory up to paths_mem_rows(), then temp files; .chunks(ext) -> (unit, folder, [(name, size, mtime), ...])), capped at PATHS_MAX_FILES (counts["paths_dropped"] says how many were not kept).
    -> {"tally": {(unit, ext): [files, bytes]}, "unit": {unit: Counter(junk, tiered, dedup)}, "counts": Counter, "unreadable": [(unit, rel, kind, reason)]}"""
    import queue
    progress = progress or (lambda *a: None)
    q, lock, finished = queue.Queue(), threading.Lock(), threading.Event()
    todo = list(range(len(units))) if only is None else sorted(only)
    if not todo:
        return {"tally": {}, "unit": {}, "counts": collections.Counter(), "unreadable": []}
    pending = [len(todo)]
    tallies = [collections.defaultdict(lambda: [0, 0]) for _ in range(nthreads)]
    extras = [collections.defaultdict(collections.Counter) for _ in range(nthreads)]
    ctr = [collections.Counter() for _ in range(nthreads)]
    bad = [[] for _ in range(nthreads)]
    pstore = PathStore(nthreads, paths_mem_rows()) if paths else None
    kept = [0]                                               # files whose path was kept (shared; guarded by `lock`)
    bases = {ui: lp(units[ui][1]) for ui in todo}
    for ui in todo:
        q.put((ui, bases[ui], 0, units[ui][2]))
    S_IFMT, S_IFREG, S_IFDIR, S_IFLNK = 0o170000, 0o100000, 0o040000, 0o120000
    JUNCTION = 0xA0000003                                    # Windows reparse tag of a junction (mount point)

    def relpath(ui, path):
        return path[len(bases[ui]):].lstrip("\\/").replace("\\", "/")

    def list_dir(path, recurse):
        """One folder: ONE stat per entry (on Windows it arrives with the directory listing, no extra disk access) gives file / folder / link
        and the size. Everything is gathered locally and returned, so a retry after a dropped connection starts clean."""
        subdirs, loc, ferr = [], {}, []
        locp = {} if paths else None
        nfiles = nbytes = njunk = nerr = nlinks = ntier = ndedup = 0
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    kind = st.st_mode & S_IFMT
                    if kind == S_IFREG:
                        size = st.st_size
                    elif kind == S_IFDIR:
                        if getattr(st, "st_reparse_tag", 0) == JUNCTION:
                            nlinks += 1                          # a junction is never followed
                        elif recurse:
                            subdirs.append(e.path)
                        continue
                    elif kind == S_IFLNK:
                        if e.is_dir():                           # a link to a folder: not followed (it could loop)
                            nlinks += 1
                            continue
                        size = e.stat().st_size                  # a link to a file counts as that file
                    else:
                        continue                                 # sockets, devices ...
                    name = e.name
                    if len(name) <= 11 and name.lower() in SYSTEM_JUNK or name.startswith("._"):
                        njunk += 1
                        continue
                    if _file_attrs(st) & TIER_MASK:
                        ntier += 1
                    elif getattr(st, "st_reparse_tag", 0) == DEDUP_TAG:
                        ndedup += 1
                    ext = quick_ext(name)
                    t = loc.get(ext)
                    if t is None:
                        t = loc[ext] = [0, 0]
                    t[0] += 1
                    t[1] += size
                    nfiles += 1
                    nbytes += size
                    if locp is not None:
                        lst = locp.get(ext)
                        if lst is None:
                            lst = locp[ext] = []
                        lst.append((name, size, st.st_mtime))
                except OSError as ex:
                    nerr += 1
                    ferr.append((e.name, reason_of(ex)))
        return subdirs, loc, (nfiles, nbytes, njunk, nerr, nlinks, ntier, ndedup), ferr, locp

    def work(k):
        tally, c, ex_ = tallies[k], ctr[k], extras[k]
        while True:
            item = q.get()
            if item is None:
                return
            ui, path, depth, recurse = item
            subdirs = []
            try:
                subdirs, loc, nums, ferr, locp = retry(lambda: list_dir(path, recurse), c)
                nfiles, nbytes, njunk, nerr, nlinks, ntier, ndedup = nums
                if locp:
                    with lock:
                        room = kept[0] + nfiles <= PATHS_MAX_FILES
                        if room:
                            kept[0] += nfiles
                    if room:
                        rel_ = relpath(ui, path)
                        for ext, lst in locp.items():
                            pstore.add(k, ext, ui, rel_, lst)
                    else:
                        c["paths_dropped"] += nfiles
                for ext, (n, b) in loc.items():
                    t = tally[(ui, ext)]
                    t[0] += n
                    t[1] += b
                if njunk or ntier or ndedup:
                    e_ = ex_[ui]
                    e_["junk"] += njunk
                    e_["tiered"] += ntier
                    e_["dedup"] += ndedup
                c["dirs"] += 1
                if depth > c["max_depth"]:
                    c["max_depth"] = depth
                c["files"] += nfiles
                c["bytes"] += nbytes
                c["junk"] += njunk
                c["errors"] += nerr
                c["links_skipped"] += nlinks
                c["tiered_files"] += ntier
                c["dedup_files"] += ndedup
                if ferr and len(bad[k]) < MAX_UNREADABLE:
                    rel = relpath(ui, path)
                    bad[k] += [(ui, f"{rel}/{nm}" if rel else nm, "file", why) for nm, why in ferr]
                elif ferr:
                    c["unreadable_not_listed"] += len(ferr)
            except OSError as ex:
                c["dir_errors"] += 1
                if len(bad[k]) < MAX_UNREADABLE:
                    bad[k].append((ui, relpath(ui, path), "folder", reason_of(ex)))
                else:
                    c["unreadable_not_listed"] += 1
            with lock:
                pending[0] += len(subdirs) - 1
                done = pending[0] == 0
            for sd in subdirs:
                q.put((ui, sd, depth + 1, recurse))
            if done:
                finished.set()

    threads = [threading.Thread(target=work, args=(k,), daemon=True) for k in range(nthreads)]
    for t in threads:
        t.start()
    try:
        while not finished.wait(0.3):
            progress("scan", sum(c["files"] for c in ctr), sum(c["files"] for c in ctr), 0, sum(c["bytes"] for c in ctr))
    finally:
        for _ in threads:
            q.put(None)
    counts = collections.Counter()
    for c in ctr:
        for key, v in c.items():
            counts[key] = max(counts[key], v) if key == "max_depth" else counts[key] + v
    tally = collections.defaultdict(lambda: [0, 0])
    for tl in tallies:
        for key, (n, b) in tl.items():
            tally[key][0] += n
            tally[key][1] += b
    unit = collections.defaultdict(collections.Counter)
    for ex_ in extras:
        for ui, cn in ex_.items():
            unit[ui].update(cn)
    flat = [x for lst in bad for x in lst]
    counts["unreadable_not_listed"] += max(0, len(flat) - MAX_UNREADABLE)
    return {"tally": dict(tally), "unit": dict(unit), "counts": counts, "unreadable": flat[:MAX_UNREADABLE], "paths": pstore}


def unreadable_csv(units, rows, with_paths=True, files_with_paths=True):
    """Unreadable.csv rows: project, kind (folder / file), path inside the project, reason. Paths are only the part below the project folder
    (never the full path of the data owner's machine)."""
    out = []
    for ui, rel, kind, why in rows:
        keep = with_paths and (kind == "folder" or files_with_paths)
        out.append([units[ui][0], kind, rel if keep else "", why])
    return out


def _name_map(flag):
    """A NameMap when name anonymisation is asked for (argument, else env PROFILER_ANON_NAMES), else None."""
    if flag is None:
        flag = os.environ.get("PROFILER_ANON_NAMES", "").lower() in ("1", "true", "yes")
    if not flag:
        return None
    if name_anon is None:
        raise SystemExit("name anonymisation needs name_anon.py next to file_profiler.py")
    return name_anon.NameMap()


def _safe_member(ext, used):
    """File name for the list of one file type inside the zip: lower case, safe characters, no Windows device names, unique."""
    n = "no_extension" if ext == "(none)" else re.sub(r"[^a-z0-9_+.\-]", "_", ext.lower())[:40] or "_"
    if n.split(".")[0] in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(com|lpt)\d", n.split(".")[0]) or n.endswith("."):
        n += "_"
    base, k = n, 1
    while n in used:
        k += 1
        n = f"{base}_{k}"
    used.add(n)
    return n


def write_path_lists(z, units, paths, nm=None):
    """Paths/<type>.csv for every file type: project, path inside the project, size_bytes, modified_utc (from the listing; no file is opened).
    Names are the real ones, or the codes of `nm` when names are anonymised. -> (number of files written, {type: member name})"""
    used, index, written = set(), {}, 0
    for ext in sorted(paths.exts(), key=lambda e: (-paths.count(e), e)):
        member = _safe_member(ext, used)
        index[ext] = member
        with z.open(f"Paths/{member}.csv", "w", force_zip64=True) as raw:
            out = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            out.write("\ufeff")
            w = csv.writer(out, lineterminator="\r\n")
            w.writerow(["project", "path", "size_bytes", "modified_utc"])
            for ui, rel, lst in paths.chunks(ext):
                proj = units[ui][0]
                if nm:
                    proj = nm.project(proj)
                pre = (rel + "/") if rel else ""
                rows = [[proj, nm.file_path(pre + nm_) if nm else pre + nm_, sz, iso(mt)] for nm_, sz, mt in lst]
                w.writerows(rows)
                written += len(rows)
            out.flush()
            out.detach()
    return written, index


def quick_scan(root, out_dir, log=print, label=None, folders=None, workers=None, progress=None, project_depth=None, anonymize_names=None, name_map=True, list_paths=None):
    """STEP 1 - the quick count: how many files of each type, and how big, per project and overall. Metadata only: no file is opened,
    no archive is looked into, nothing is read - just the folder listings the operating system already holds (see scan_tree). Writes
    quick_<name>_<stamp>.zip: Extensions.csv (per project), Extensions_All.csv, Projects.csv, Unreadable.csv, Run_info.csv.
    -> (zip_path, number of projects, number of files, unreadable folders + files)."""
    t0 = time.time()
    project_depth = max(1, int(os.environ.get("PROFILER_PROJECT_DEPTH", 1)) if project_depth is None else int(project_depth))
    diag.drain_warnings()
    units, label, data_paths = resolve_units(root, folders, label, project_depth)
    setup_warnings = diag.drain_warnings()
    for where, path_, why in setup_warnings:
        log(f"WARNING ({where}): {path_}: {why}")
    nthreads = int(workers) if workers else quick_threads(data_paths)
    want_paths = (os.environ.get("PROFILER_PATHS", "1").lower() not in ("0", "false", "no")) if list_paths is None else bool(list_paths)
    res = scan_tree(units, nthreads, progress, paths=want_paths)
    total = res["counts"]
    ext_stats = res["tally"]
    secs = time.time() - t0
    proj, overall = collections.defaultdict(lambda: [0, 0, 0]), collections.defaultdict(lambda: [0, 0])
    for (ui, ext), (n, b) in ext_stats.items():
        pr = proj[ui]
        pr[0] += n; pr[1] += b; pr[2] += 1
        overall[ext][0] += n
        overall[ext][1] += b
    nfiles, nbytes = total["files"], total["bytes"]
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(out_dir, f"quick_{re.sub(r'[^A-Za-z0-9_.-]', '_', label)}_{stamp}.zip")

    def csv_bytes(header, rows):
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(header)
        w.writerows(rows)
        return ("\ufeff" + buf.getvalue()).encode("utf-8")
    nm = _name_map(anonymize_names)
    pname = (lambda n: nm.project(n)) if nm else (lambda n: n)
    ext_rows = [[units[ui][0], ext, n, b, b // max(1, n)] for (ui, ext), (n, b) in sorted(ext_stats.items(), key=lambda kv: (units[kv[0][0]][0].lower(), -kv[1][0]))]
    all_rows = [[ext, n, b, f"{100 * n / max(1, nfiles):.2f}", f"{100 * b / max(1, nbytes):.2f}"] for ext, (n, b) in sorted(overall.items(), key=lambda kv: -kv[1][0])]
    proj_rows = [[units[ui][0], v[0], v[1], v[2]] for ui, v in sorted(proj.items(), key=lambda kv: units[kv[0]][0].lower())]
    # step 1 records no file names: unreadable FOLDERS are listed (the owner needs to know which), unreadable files only counted per project
    bad_rows = unreadable_csv(units, res["unreadable"], with_paths=True, files_with_paths=False)
    if nm:                                  # only the output is renamed: project names, folder paths of unreadable folders, the root folder name
        for u in units:
            nm.project(u[0])                # codes follow the order of the projects
        ext_rows = [[pname(r[0])] + r[1:] for r in ext_rows]
        proj_rows = [[pname(r[0])] + r[1:] for r in proj_rows]
        bad_rows = [[pname(r[0]), r[1], nm.folder_path(r[2]) if r[1] == "folder" else r[2], r[3]] for r in bad_rows]
    info = [["tool_version", VERSION], ["run_utc", now_utc()], ["mode", "quick count (step 1)"], ["os", platform.platform()], ["folder_name", nm.root(label) if nm else label],
            ["projects", len(units)], ["project_depth", project_depth], ["files", nfiles], ["total_bytes", nbytes], ["folders", total["dirs"]],
            ["deepest_folder_level", total["max_depth"]], ["distinct_extensions", len(overall)], ["unreadable_folders", total["dir_errors"]],
            ["read_errors", total["errors"]], ["linked_folders_skipped", total["links_skipped"]], ["ignored_system_files", total["junk"]],
            ["cloud_tiered_files", total["tiered_files"]], ["deduplicated_files", total["dedup_files"]], ["network_retries", total["retries"]],
            ["unreadable_not_listed_cap", total["unreadable_not_listed"]], ["setup_warnings", len(setup_warnings)],
            ["threads", nthreads], ["usable_cpus", _usable_cpus()], ["seconds", round(secs, 1)], ["files_per_s", round(nfiles / max(secs, 0.01))],
            ["files_opened", 0], ["hashing", "none"], ["names_anonymized", "yes" if nm else "no"],
            ["file_paths_listed", "yes" if want_paths else "no"], ["file_paths_not_kept_over_limit", total["paths_dropped"] if want_paths else 0]]
    what = ("This archive was made by the Folder Profiler QUICK COUNT (step 1).\r\n"
            "It lists, per project folder and overall, how many files of each type there are and how big they are. No file was opened,\r\n"
            "no archive was looked into, and no file names are recorded unless a Paths folder is present - only the project (folder) names, the file types, and the names of\r\n"
            "any folders that could not be read (Unreadable.csv).\r\n"
            "Extensions.csv (per project), Extensions_All.csv (whole folder), Projects.csv, Unreadable.csv, Run_info.csv. Open them with Excel or Notepad.\r\n"
            + ("Names are ANONYMISED: project and folder names are codes (P-xxxx-nnnn, D-xxxx-nnnnnn). The owner keeps the name map separately.\r\n" if nm else ""))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Extensions.csv", csv_bytes(["project", "ext", "file_count", "total_bytes", "avg_bytes"], ext_rows))
        z.writestr("Extensions_All.csv", csv_bytes(["ext", "file_count", "total_bytes", "share_of_files_pct", "share_of_bytes_pct"], all_rows))
        z.writestr("Projects.csv", csv_bytes(["project", "file_count", "total_bytes", "distinct_extensions"], proj_rows))
        z.writestr("Unreadable.csv", csv_bytes(["project", "kind", "path", "reason"], bad_rows))
        if setup_warnings:
            z.writestr("Setup_Warnings.csv", csv_bytes(["where", "path", "reason"], [[w, nm.folder_path(pp) if nm else pp, why] for w, pp, why in setup_warnings]))
        t_paths = time.time()
        n_paths, p_index = write_path_lists(z, units, res["paths"], nm) if want_paths and res.get("paths") else (0, {})
        if want_paths:
            z.writestr("Paths_Index.csv", csv_bytes(["file_type", "list", "files"], [[e, f"Paths/{m}.csv", res["paths"].count(e)] for e, m in p_index.items()]))
        paths_s = time.time() - t_paths
        z.writestr("Run_info.csv", csv_bytes(["key", "value"], info + ([["seconds_writing_path_lists", round(paths_s, 1)], ["path_list_spills_to_disk", res["paths"].spills]] if want_paths else []) + [["peak_memory_mb", _peak_mb()]]))
        z.writestr("WHAT_IS_IN_THIS_ZIP.txt", (what + ("Paths/<type>.csv lists EVERY file of that type: project, path inside the project, size and modified date (read from the folder listing; "
                                                       "no file was opened). Paths_Index.csv says how many files each list has.\r\n" if want_paths else "")).encode("utf-8"))
    if res.get("paths"):
        res["paths"].close()
    map_zip = ""
    if nm and name_map:
        map_zip = nm.write_zip(os.path.join(out_dir, f"name_map_{re.sub(r'[^A-Za-z0-9_.-]', '_', label)}_{stamp}.zip"), label)
    LAST_RUN.clear()
    LAST_RUN.update(map_zip=map_zip, quick_zip=zip_path, top=[[e, n, b] for e, n, b, *_ in all_rows[:12]], total_bytes=nbytes, distinct_extensions=len(overall),
                    seconds=round(secs, 1))
    log(f"quick count: {nfiles:,} files, {nbytes / 1e9:.1f} GB, {len(overall)} file types in {len(units)} projects, {secs:.1f}s "
        f"({nfiles / max(secs, 0.01):,.0f} files/s; unreadable folders {total['dir_errors']}, linked folders skipped {total['links_skipped']}"
        + (f", cloud-tiered files {total['tiered_files']}" if total["tiered_files"] else "") + (f", network retries {total['retries']}" if total["retries"] else "") + ")")
    return zip_path, len(units), nfiles, total["dir_errors"] + total["errors"]


def profile(root, out_dir, log=print, label=None, folders=None, workers=None, progress=None,
            anonymize_assembly_names=None, tier=None, project_depth=None,
            resume=True, metrics_interval=None, system_info=True, chunk_files=None, anonymize_names=None, name_map=True):
    """Profile a folder tree. Streaming and resumable: every project is processed in chunks that are saved to
    <out_dir>/.resume/<run id>/ as they finish; a run that stops (crash, power cut, Ctrl+C) continues from the last saved chunk when
    started again with the same settings. The state is deleted when the run completes. Nothing is ever hashed except folder
    NAMES (salted, to make anonymous folder ids); file contents are never hashed.
    progress(phase, files_done, files_total, bytes_done, bytes_total): phase "scan" (counting), "process" or "links"."""
    progress = progress or (lambda *a: None)
    t_run0 = time.time()
    chunk_files = int(chunk_files or CHUNK_FILES)
    env_int = lambda k, d: int(os.environ[k]) if os.environ.get(k, "").isdigit() else d
    tier = env_int("PROFILER_TIER", 2) if tier is None else int(tier)
    if tier not in (0, 1, 2):
        raise SystemExit("detail level must be 0, 1 or 2")
    if tier == 2 and cad_links is None:
        raise SystemExit("detail level 2 needs cad_links.py next to file_profiler.py")
    project_depth = max(1, env_int("PROFILER_PROJECT_DEPTH", 1) if project_depth is None else int(project_depth))
    nm = _name_map(anonymize_names)
    if tier >= 1 or nm:                     # with name anonymisation the assembly names follow the same map as every other name
        anonymize_assembly_names = False
    if anonymize_assembly_names is None:
        anonymize_assembly_names = os.environ.get("PROFILER_ANON_ASM", "").lower() in ("1", "true", "yes")

    # ---- what to profile
    diag.drain_warnings()
    units, label, data_paths = resolve_units(root, folders, label, project_depth)
    setup_warnings = diag.drain_warnings()
    workers = max(1, workers or default_workers(data_paths))
    if nm:
        for u in units:
            nm.project(u[0])                # codes follow the order of the projects
    shown = (lambda n: nm.project(n)) if nm else (lambda n: n)      # project name as it may appear in the run log

    gate = MemGate(memory_budget())
    copies = {}                                  # (ext, name, size, date) -> (tag, sheet result): exact copies of a drawing are not read twice
    read_all = os.environ.get("PROFILER_READ_ALL_HEADERS", "").lower() in ("1", "true", "yes")
    no_head = frozenset() if read_all else NO_HEAD_EXTS             # (kept: part of the run's identity, see cfg)
    policy = (lambda ext: "read") if read_all else head_policy
    hit_types = set()                                               # (project, ext) of sampled types whose sample showed CAD content
    # ---- run identity, checkpoint state
    cfg = json.dumps({"v": VERSION, "nohead": bool(no_head), "units": [[u[0], u[1], u[2]] for u in units], "tier": tier, "depth": project_depth,
                      "anon": bool(anonymize_assembly_names), "anon_names": bool(nm),
                      "chunk": chunk_files}, sort_keys=True)
    run_id = f"{zlib.crc32(cfg.encode()):08x}{zlib.adler32(cfg.encode()):08x}"
    state_dir = os.path.join(out_dir, ".resume", run_id)
    LAST_RUN["state_dir"] = state_dir           # so a failed run's logs can be bundled (make_fail_log)
    if not resume:
        shutil.rmtree(state_dir, ignore_errors=True)
    for sub in ("rows", "links", "dirs", "problems"):
        os.makedirs(os.path.join(state_dir, sub), exist_ok=True)
    mpath = os.path.join(state_dir, "manifest.json")
    man, man_problem = None, ""
    if os.path.exists(mpath):
        try:
            man = json.load(open(mpath, encoding="utf-8"))
            if man.get("cfg") != cfg:
                man = None
        except Exception as exc:
            man, man_problem = None, type(exc).__name__
    resumed = man is not None
    if man is None:
        man = {"cfg": cfg, "salt": os.urandom(8).hex(), "created": now_utc(), "restarts": 0, "elapsed": 0.0, "units": {},
               "prescan_s": 0.0, "process_s": 0.0, "c": collections.Counter()}
    else:
        man["restarts"] += 1
        man["c"] = collections.Counter(man["c"])
    C = man["c"]
    elapsed_before = man["elapsed"]
    salt = man["salt"]

    def ident(*parts, n=16):
        return hashlib.sha256((salt + "\0" + "\0".join(parts)).encode("utf-8", "surrogatepass")).hexdigest()[:n]
    dirs = {}                                           # dir_id -> (project, parent_dir_id, depth, name); a project's entries are dropped once it is checkpointed
    dirs_of = collections.defaultdict(list)             # project -> its dir ids (so finishing a project does not scan every folder of the run)

    def reg(proj, rel, depth, name, parent=None):
        i = ident(proj, rel)
        if i not in dirs:
            if parent is None:
                parent = "" if rel == "." else ident(proj, os.path.dirname(rel) or ".")
            dirs[i] = (proj, parent, depth, name)
            dirs_of[proj].append(i)
        return i

    # ---- logs, metrics, system info (Run_log.csv / Run_metrics.csv live in the state dir so they survive a restart)
    t0 = time.time()
    rlog = sysmetrics.RunLog(os.path.join(state_dir, "Run_log.csv"), t0=t0, echo=log)
    act = sysmetrics.Activity()
    state = {"phase": "start", "files": 0, "bytes": 0}
    mon = sysmetrics.Monitor(os.path.join(state_dir, "Run_metrics.csv"), state,
                             interval=metrics_interval or env_int("PROFILER_METRICS_INTERVAL", 5), t0=t0)
    mon.start()
    si_box = [None]                                      # machine facts (PowerShell on Windows takes seconds): gathered while the run works
    si_thread = None
    if system_info:
        os.makedirs(out_dir, exist_ok=True)
        def _si():
            si_box[0] = sysmetrics.system_info(data_paths, out_dir, {"workers": workers, "detail_level": tier, 
                                                                      "hashing": "none"}, deep=True)
        si_thread = threading.Thread(target=_si, daemon=True)
        si_thread.start()
    rlog.event("start", ("RESUMED run (restart #%d) " % man["restarts"] if resumed else "started ") +
               f"v{VERSION}: level {tier}, project depth {project_depth}, {workers} workers, "
               f"{len(units)} projects, hashing: none")
    if man_problem:
        rlog.event("start", f"the saved progress of an earlier run could not be read ({man_problem}) - starting from the beginning", "WARN")
    for where, path_, why in setup_warnings:
        rlog.event("start", f"{where}: {(nm.folder_path(path_) if nm else path_)}: {why}", "WARN")

    manifest_clock = [0.0, 0.0]               # when the progress file was last written, and how long that took

    def save_manifest(force=False):
        """Write the progress file. It holds every project, so on a run with thousands of projects rewriting it after every chunk made the run
        slower and slower (quadratic). Unless forced, it is not rewritten before 20x the time the last write took has passed: the bookkeeping
        stays below ~5% of the run, and a stop loses at most those few seconds of progress (the chunks are simply redone)."""
        now = time.time()
        if not force and now - manifest_clock[0] < 20 * manifest_clock[1]:
            return
        man["elapsed"] = elapsed_before + (now - t_run0)
        _save_json(mpath, man)
        manifest_clock[0] = time.time()
        manifest_clock[1] = manifest_clock[0] - now

    def on_walk_error(e):
        C["dir_errors"] += 1
        C["errors"] += 1
        rlog.event(state["phase"], f"unreadable folder skipped ({reason_of(e)})", "WARN")

    walk_counts = collections.Counter()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        # ================================================================= scan (count) - cached per project in the manifest
        state["phase"] = "scan"
        total_files = total_bytes = 0
        need = [ui for ui, (proj, _b, _r) in enumerate(units) if "files" not in man["units"].setdefault(proj, {})]
        if need:                                          # the same threaded folder listing as step 1 (cached per project for a resumed run)
            def scan_progress(phase, files, _tf, _db, nbytes):
                state["files"] = files
                progress("scan", files, files, 0, nbytes)
            res = scan_tree(units, quick_threads(data_paths) if not os.environ.get("PROFILER_WORKERS", "").isdigit() else workers, scan_progress, only=set(need))
            by_unit = collections.defaultdict(lambda: [0, 0])
            for (ui, _ext), (n_, b_) in res["tally"].items():
                by_unit[ui][0] += n_
                by_unit[ui][1] += b_
            for ui in need:
                u = man["units"][units[ui][0]]
                ex_ = res["unit"].get(ui, collections.Counter())
                lst = [[rel, kind, why] for (i, rel, kind, why) in res["unreadable"] if i == ui]
                C["unreadable_not_listed"] += max(0, len(lst) - 5000)
                u.update(files=by_unit[ui][0], bytes=by_unit[ui][1], junk=ex_["junk"], tiered=ex_["tiered"], dedup=ex_["dedup"],
                         unreadable=lst[:5000])
            cnt = res["counts"]
            C["unreadable_not_listed"] += cnt["unreadable_not_listed"]
            C["dir_errors"] += cnt["dir_errors"]
            C["errors"] += cnt["dir_errors"] + cnt["errors"]
            C["retries"] += cnt["retries"]
            walk_counts["links_skipped"] += cnt["links_skipped"]
            if cnt["dir_errors"] or cnt["errors"]:
                why = collections.Counter(w for (_i, _r, _k, w) in res["unreadable"])
                rlog.event("scan", f"{cnt['dir_errors']} folder(s) and {cnt['errors']} file(s) could not be read ("
                                   + ", ".join(f"{k} x{v}" for k, v in why.most_common(4)) + ") - listed in Unreadable.csv", "WARN")
            if cnt["tiered_files"]:
                rlog.event("scan", f"{cnt['tiered_files']} cloud-tiered / online-only file(s) found: they are counted from the listing but NOT opened "
                                   "(opening them would download them)")
            if cnt["retries"]:
                rlog.event("scan", f"the network dropped {cnt['retries']} time(s); the folder was listed again each time")
            save_manifest(force=True)
        for proj, _b, _r in units:
            u = man["units"][proj]
            total_files += u["files"]
            total_bytes += u["bytes"]
        scan_s = time.time() - t0
        man["prescan_s"] += scan_s
        rlog.event("scan", f"counted {total_files:,} files, {total_bytes / 1e9:.1f} GB in {len(units)} projects "
                           f"({scan_s:.1f}s; unreadable folders {C['dir_errors']}, linked folders skipped {walk_counts['links_skipped']})")
        done_files = sum(man["units"][p[0]].get("files", 0) for p in units if man["units"][p[0]].get("complete"))
        done_bytes = sum(man["units"][p[0]].get("bytes", 0) for p in units if man["units"][p[0]].get("complete"))
        state.update(phase="process", files=done_files, bytes=done_bytes)
        progress("process", done_files, total_files, done_bytes, total_bytes)
        t_proc0 = time.time()
        last_prog = [0.0]

        def tick():
            now = time.time()
            if now - last_prog[0] > 0.3:
                progress("process", state["files"], max(total_files, state["files"]), state["bytes"], max(total_bytes, state["bytes"]))
                last_prog[0] = now

        def columns():
            return (["project", "file_no", "ext", "size_bytes", "modified_utc", "depth", "header_signature", "dir_id", "asm_name"]
                    + (["rel_path"] if tier >= 1 else []) + (["sheets", "sheets_conf", "sheets_status"] if tier >= 2 else []))

        def sheet_cols(sh):
            """Row cells for the sheet count: '' = not attempted (not a drawing / level < 2); confidence 0 with no count = attempted, failed."""
            if tier < 2:
                return []
            if sh is None:
                return ["", "", ""]
            return [sh["sheets"] if sh["sheets"] is not None else "", sh["confidence"], sh["code"]]

        def link_rows(proj, n, ext, res):
            if res[0] in ("native", "scan"):
                return [[proj, n, ext, res[0], "", c] for c in res[1]]
            return [[proj, n, ext, "step", par, c] for par, kids in res[1].items() for c in kids]

        # ================================================================= process each project, chunk by chunk
        for ui, (proj, base, recurse) in enumerate(units):
            u = man["units"][proj]
            if u.get("complete") and resumed and u.get("fps") is not None:
                t_v = time.time()
                with act.timer("resume_verify"):
                    same = chunk_fingerprints(base, recurse, chunk_files) == u["fps"]
                if not same:
                    u["complete"] = False                         # changed since it was saved: the chunk check below redoes what differs
                    C["stale_projects"] += 1
                    rlog.event("process", f"[{ui + 1}/{len(units)}] {shown(proj)}: changed since it was saved in an earlier run - checking it again", "WARN")
                else:
                    rlog.event("process", f"[{ui + 1}/{len(units)}] {shown(proj)}: unchanged since an earlier run ({time.time() - t_v:.1f}s check) - skipped")
                    continue
            elif u.get("complete"):
                rlog.event("process", f"[{ui + 1}/{len(units)}] {shown(proj)}: already done in an earlier run - skipped")
                continue
            t_unit = time.time()
            chunks_done = u.get("chunks_done", 0)
            if chunks_done:
                rlog.event("process", f"[{ui + 1}/{len(units)}] {shown(proj)}: resuming after chunk {chunks_done}")
                state["files"] += min(u.get("files", 0), chunks_done * chunk_files)
            n = 0
            pending, chunk_idx = [], 0
            wbase = lp(base)

            retries_seen = [C["retries"]]
            inflight = collections.deque()               # chunks whose reads were submitted but whose rows are not yet written (kept in order)

            def flush_chunk():
                """Submit this chunk's reads and go straight on listing the next chunk; the oldest chunk is finished (rows built, checkpoint
                written) once PIPELINE_CHUNKS are in flight. Before, each chunk was waited for, so the readers idled while the next 5,000 files
                were being listed."""
                nonlocal pending, chunk_idx
                fp = 0                                   # what this chunk's files looked like (names, sizes, dates) when seen now
                for e in pending:
                    fp = zlib.crc32(f"{e[8]}\0{e[3]}\0{e[4]}\n".encode("utf-8", "surrogatepass"), fp)
                fps = u.setdefault("fps", [])
                if chunk_idx < chunks_done:
                    if chunk_idx < len(fps) and fps[chunk_idx] == fp:
                        chunk_idx += 1                   # same files as when it was saved: keep the saved rows
                        pending = []
                        return
                    C["stale_chunks"] += 1               # the folder changed since this chunk was saved: profile it again
                    state["files"] -= min(len(pending), state["files"])
                    rlog.event("process", f"{shown(proj)}: chunk {chunk_idx + 1} was saved before the folder changed - profiled again", "WARN")
                t_c = time.time()

                def work(e, allow=True):
                    diag.take()                                # forget the note of the previous file read by this thread
                    r = work_core(e, allow)
                    return r + (diag.take(),)                  # why the file was not fully read, '' when it was

                def work_core(e, allow=True):
                    path, name, ext, size, mtime, dir_id, depth, asm, relpath, nn, tiered = e
                    if tiered:                                 # not really on this disk (cloud-tiered / online-only): opening it would download it
                        return "TIERED", None, False, (TIERED_SHEET if tier >= 2 and ext in SHEET_EXTS and dwg_sheets is not None else None)
                    pol = policy(ext)
                    if pol == "skip" or (pol == "sample" and not allow):      # nothing in the header would be used: do not open the file at all
                        return ("NOTAG" if size else "EMPTY"), None, False, None
                    if tier >= 2 and ext in SHEET_EXTS and dwg_sheets is not None and size > 0:
                        ck = (ext, name.lower(), size, mtime)          # an exact copy (same name, size, date: the compiler's own duplicate rule) was already read
                        hit = copies.get(ck)
                        if hit is not None:
                            return hit[0], None, False, dict(hit[1], reused=True)
                        def read_sheets():                     # ONE open: format tag and sheet count from the same read
                            with gate.hold(size * MEM_FACTOR), open(path, "rb") as fh:
                                with act.timer("format_tag_read"):
                                    sig = signature_stream(fh, ext, size)
                                fh.seek(0)
                                with act.timer("sheet_count"):
                                    known = sig[4:10].encode() if ext == "dwg" and sig.startswith("DWG:") else None      # version already read for the tag
                                    return sig, dwg_sheets.count_sheets_file(fh, ext, size, head=known)
                        try:
                            sig, sh = retry(read_sheets, C)
                        except OSError as exc:
                            diag.note("header", reason_of(exc))
                            return "UNREADABLE", None, False, None
                        if len(copies) < 500_000:
                            copies[ck] = (sig, sh)
                        return sig, None, False, sh
                    if tier >= 2 and ext in LINK_READ_EXTS and cad_links is not None and size > 0:
                        def read_refs():                       # ONE open: the format tag and the references come from the same read
                            with gate.hold((min(size, 8 << 20) if ext in ("step", "stp") else size) * MEM_FACTOR), open(path, "rb") as fh:
                                with act.timer("format_tag_read"):
                                    sig = signature_stream(fh, ext, size)
                                fh.seek(0)
                                with act.timer("reference_read"):
                                    return sig, cad_links.read_links_file(fh, ext, name, size)
                        try:
                            sig, res = retry(read_refs, C)
                        except OSError as exc:
                            diag.note("header", reason_of(exc))
                            return "UNREADABLE", None, True, None
                        return sig, res, True, None
                    with act.timer("format_tag_read"):
                        sig = signature(path, ext, size, C)
                    return sig, None, False, None

                seen = collections.Counter()
                allow = []
                for e in pending:                                    # sampled types: only the first few of each type in this chunk are read
                    if policy(e[2]) == "sample":
                        seen[e[2]] += 1
                        allow.append(seen[e[2]] <= SAMPLE_PER_CHUNK)
                    else:
                        allow.append(True)
                inflight.append((chunk_idx, pending, [pool.submit(work, e, a) for e, a in zip(pending, allow)], fp, t_c))
                chunk_idx += 1
                pending = []
                while len(inflight) > PIPELINE_CHUNKS:
                    finish_chunk(inflight.popleft())

            def finish_chunk(job):
                chunk_no, pend, futs, fp, t_c = job
                fps = u.setdefault("fps", [])
                rows, lrows, prows = [], [], []
                for e, fut in zip(pend, futs):
                    sig, res, was_link, sh, why = fut.result()
                    path, name, ext, size, mtime, dir_id, depth, asm, relpath, nn, tiered = e
                    if sig == "NOTAG":
                        C["head_skipped"] += 1
                    elif sig != "EMPTY" and policy(ext) == "sample":
                        C["head_sampled"] += 1
                    why_txt = why.split(": ", 1)[-1] if why else ""
                    if "UNREADABLE" in sig:
                        C["errors"] += 1
                        if len(u.setdefault("unreadable", [])) < 5000:
                            u["unreadable"].append([relpath if tier >= 1 else "", "file", why_txt or "could not be read"])
                        else:
                            C["unreadable_not_listed"] += 1
                        prows.append([proj, nn, ext, "header", why_txt or "could not be read"])
                    if sh is not None:
                        if sh.get("reused"):
                            C["sheets_reused"] += 1
                        C["sheets_counted" if sh["sheets"] is not None else "sheets_not_counted"] += 1
                        C["sheet_status:" + sh["code"]] += 1
                    rows.append([proj, nn, ext, size, mtime, depth, sig, dir_id, asm] + ([relpath] if tier >= 1 else []) + sheet_cols(sh if ext in SHEET_EXTS else None))
                    if was_link:
                        if res is None:
                            C["link_unreadable"] += 1
                            if "UNREADABLE" not in sig:
                                prows.append([proj, nn, ext, "references", why_txt or "not understood"])
                        else:
                            C["link_read"] += 1
                            lrows += link_rows(proj, nn, ext, res)
                    state["files"] += 1
                    state["bytes"] += size
                    tick()
                if prows:
                    kinds = collections.Counter(f"{r[3]}: {r[4]}" for r in prows)
                    rlog.event("process", f"{shown(proj)}: chunk {chunk_no + 1}: {len(prows)} file(s) not fully read ("
                                          + ", ".join(f"{k} x{v}" for k, v in kinds.most_common(4)) + ") - listed in Read_Problems.csv", "WARN")
                if C["retries"] > retries_seen[0]:             # reads run in several chunks at once: count what dropped since the previous chunk was written
                    rlog.event("process", f"{shown(proj)}: chunk {chunk_no + 1}: the network dropped {C['retries'] - retries_seen[0]} time(s) while reading; "
                                          "each read was tried again", "WARN")
                retries_seen[0] = C["retries"]
                with act.timer("write_checkpoint"):
                    _write_csv_atomic(os.path.join(state_dir, "rows", f"u{ui}_c{chunk_no:06d}.csv"), rows)
                    _write_csv_atomic(os.path.join(state_dir, "links", f"u{ui}_c{chunk_no:06d}.csv"), lrows)
                    _write_csv_atomic(os.path.join(state_dir, "problems", f"u{ui}_c{chunk_no:06d}.csv"), prows)
                    del fps[chunk_no:chunk_no + 1]
                    fps.insert(chunk_no, fp)
                    u["chunks_done"] = max(u.get("chunks_done", 0), chunk_no + 1)
                    save_manifest()
                rlog.event("process", f"{shown(proj)}: chunk {chunk_no + 1} saved ({len(rows):,} files, {time.time() - t_c:.1f}s from submit)")
                if _TEST_HOOK:
                    _TEST_HOOK(ui, chunk_no)

            proc_counts = collections.Counter()
            for dirpath, rel, depth, files in walk_dirs(wbase, recurse, lambda e: None, proc_counts):   # folders already counted by the scan
                dir_id = reg(proj, rel, depth, proj if rel == "." else os.path.basename(rel))
                for e in files:
                    name = e.name
                    if name.lower() in SYSTEM_JUNK or name.startswith("._"):
                        continue
                    n += 1
                    relpath = name if rel == "." else (rel + os.sep + name).replace(os.sep, "/")
                    try:
                        st = e.stat()
                    except OSError as ex:
                        C["errors"] += 1
                        if len(u.setdefault("unreadable", [])) < 5000:
                            u["unreadable"].append([relpath if tier >= 1 else "", "file", reason_of(ex)])
                        else:
                            C["unreadable_not_listed"] += 1
                        st = None
                    if st is not None:
                        ext = ext_of(name)
                        asm = ""
                        if ext in ASSEMBLY_EXTS:
                            asm = (f"ASM-{ident(proj, rel, name, n=8).upper()}.{ext.upper()}" if anonymize_assembly_names else name)
                        pending.append((e.path, name, ext, st.st_size, iso(st.st_mtime), dir_id, depth, asm, relpath, n,
                                        bool(_file_attrs(st) & TIER_MASK)))
                    if n % chunk_files == 0:
                        flush_chunk()
            C["retries"] += proc_counts["retries"]
            if pending or (n and n % chunk_files):
                flush_chunk()
            while inflight:                                     # the project is only done when every chunk submitted for it has been written
                finish_chunk(inflight.popleft())
            if chunk_idx < u.get("chunks_done", 0):             # the project shrank: drop saved chunks past its new end
                for k in range(chunk_idx, u["chunks_done"]):
                    for sub in ("rows", "links", "problems"):
                        try:
                            os.remove(os.path.join(state_dir, sub, f"u{ui}_c{k:06d}.csv"))
                        except OSError:
                            pass
                u["chunks_done"] = chunk_idx
                del u.setdefault("fps", [])[chunk_idx:]
            n_loose = n
            if n != u.get("files", n):
                C["changed_projects"] += 1
                rlog.event("process", f"{shown(proj)}: the folder changed while profiling or between restarts ({u.get('files')} files when counted, "
                                       f"{n} now) - run with --fresh if you need an exact result for this project", "WARN")
            # ---- project finished: remember its folder tree, mark it complete
            with act.timer("write_checkpoint"):
                _write_csv_atomic(os.path.join(state_dir, "dirs", f"u{ui}.csv"),
                                  [[i, dirs[i][1], dirs[i][2], dirs[i][3]] for i in dirs_of[proj]])
                for i in dirs_of.pop(proj, ()):                  # saved to disk: free the memory (Dirs.csv is built from the saved files at the end)
                    dirs.pop(i, None)
                u["complete"] = True
                u["loose_files"] = n_loose
                save_manifest()
            rlog.event("process", f"[{ui + 1}/{len(units)}] {shown(proj)}: done ({n_loose:,} files, {time.time() - t_unit:.1f}s)")
        man["process_s"] += time.time() - t_proc0
        save_manifest(force=True)
    except BaseException as exc:
        rlog.event(state["phase"], f"run stopped: {type(exc).__name__}. Progress is saved - start again with the same settings to resume.", "ERROR")
        save_manifest(force=True)
        mon.stop()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass

    # ===================================================================== finalize: stream the saved chunks into the zip
    state["phase"] = "finalize"
    t_fin = time.time()
    progress("process", total_files, total_files, total_bytes, total_bytes)
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
    zip_path = os.path.join(out_dir, f"profile_{safe}_{stamp}.zip")
    stats_zip = os.path.join(out_dir, f"run_stats_{safe}_{stamp}.zip")
    proj_stats, ext_stats, nfiles_out, bytes_out = {}, {}, 0, 0

    chunk_index = {}                                    # sub folder -> {unit index: [chunk file names]}: listed ONCE (listing it per project was quadratic)

    def chunk_files_of(sub, ui):
        d = os.path.join(state_dir, sub)
        if sub not in chunk_index:
            groups = collections.defaultdict(list)
            for f in os.listdir(d):
                m = re.match(r"u(\d+)_([ca])", f)
                if m and f.endswith(".csv"):
                    groups[int(m.group(1))].append(f)
            chunk_index[sub] = groups
        pre = f"u{ui}_"
        names = sorted(chunk_index[sub].get(ui, ()), key=lambda f: (f[len(pre)] != "c", f))          # chunks in order
        return [os.path.join(d, f) for f in names]

    def dir_rows():
        """Dirs.csv rows, one project at a time (sorted by project, depth, dir_id) from the saved per-project files: the folder table of a
        big drive (400,000 folders) is never held whole in memory - doing so cost more than 100 MB."""
        left = collections.defaultdict(dict)
        for i, v in dirs.items():
            left[v[0]][i] = v
        by_name = {u[0]: ui for ui, u in enumerate(units)}
        for pname in sorted(by_name):
            cur = dict(left.get(pname, ()))
            fpath = os.path.join(state_dir, "dirs", f"u{by_name[pname]}.csv")
            if os.path.exists(fpath):
                with open(fpath, newline="", encoding="utf-8") as fh:
                    for d in csv.reader(fh):
                        cur.setdefault(d[0], (pname, d[1], int(d[2]), d[3]))
            for d, v in sorted(cur.items(), key=lambda kv: (kv[1][2], kv[0])):
                yield d, v
    hit_types = set()                                    # sampled types whose sample showed CAD content: rebuilt from the saved rows (also right after a restart)
    hit_read = collections.Counter()                     # (project, ext) -> files of such a type read in full now
    if EXT_CLASSES and not read_all:
        for ui in range(len(units)):
            for fpath in chunk_files_of("rows", ui):
                with open(fpath, encoding="utf-8", newline="") as fh:
                    for row in csv.reader(fh):
                        if row[6].startswith(HIT_PREFIXES) and head_policy(row[2]) == "sample":
                            hit_types.add((row[0], row[2]))
    bases = {u[0]: u[1] for u in units}
    post_pool = None
    tmp_zip = zip_path + ".tmp"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("Files.csv", "w", force_zip64=True) as out:
            out.write(("﻿" + ",".join(columns()) + "\r\n").encode("utf-8"))
            for ui in range(len(units)):
                for fpath in chunk_files_of("rows", ui):
                    text = open(fpath, encoding="utf-8", newline="").read()
                    rows_in = csv.reader(io.StringIO(text))
                    rewritten = False
                    if hit_types and tier >= 1:                    # a sampled type showed CAD content: read the files of that type that were skipped
                        rows_in = list(rows_in)
                        todo = [r for r in rows_in if r[6] == "NOTAG" and (r[0], r[2]) in hit_types and int(r[3]) > 0]
                        if todo:
                            if post_pool is None:
                                post_pool = ThreadPoolExecutor(max_workers=workers)
                            sigs = list(post_pool.map(lambda r: signature(lp(os.path.join(bases[r[0]], *r[9].split("/"))), r[2], int(r[3])), todo))
                            for r, sg in zip(todo, sigs):
                                r[6] = sg
                                hit_read[(r[0], r[2])] += 1
                            rewritten = True
                    if nm:                                         # rename on the way out: project, assembly name, path
                        renamed = []
                        for row in rows_in:
                            row[0] = nm.project(row[0])
                            if row[8]:
                                row[8] = nm.file(row[8])
                            if tier >= 1:
                                row[9] = nm.file_path(row[9])
                            renamed.append(row)
                        buf_ = io.StringIO(newline="")
                        csv.writer(buf_, lineterminator="\r\n").writerows(renamed)
                        out.write(buf_.getvalue().encode("utf-8"))
                        rows_in = renamed
                    elif rewritten:
                        buf_ = io.StringIO(newline="")
                        csv.writer(buf_, lineterminator="\r\n").writerows(rows_in)
                        out.write(buf_.getvalue().encode("utf-8"))
                    else:
                        out.write(text.encode("utf-8"))
                    for row in rows_in:
                        proj_, ext_, size_, mod_ = row[0], row[2], int(row[3]), row[4]
                        p = proj_stats.setdefault(proj_, [0, 0, mod_, mod_, set()])
                        p[0] += 1; p[1] += size_; p[4].add(ext_)
                        if mod_:                                   # entries of rar / 7z listings have no date: they must not blank the project's range
                            p[2] = min(p[2], mod_) if p[2] else mod_; p[3] = max(p[3], mod_)
                        e = ext_stats.setdefault((proj_, ext_), [0, 0])
                        e[0] += 1; e[1] += size_
                        nfiles_out += 1; bytes_out += size_

        if post_pool is not None:
            post_pool.shutdown(wait=True)

        def put(name, header, rows):
            buf = io.StringIO(newline="")
            w = csv.writer(buf, lineterminator="\r\n")
            w.writerow(header); w.writerows(rows)
            z.writestr(name, ("﻿" + buf.getvalue()).encode("utf-8"))

        if tier >= 2:
            with z.open("Links.csv", "w", force_zip64=True) as out:
                out.write(("﻿" + "project,file_no,ext,kind,parent,child\r\n").encode("utf-8"))
                for ui in range(len(units)):
                    for fpath in chunk_files_of("links", ui):
                        ltext = open(fpath, encoding="utf-8", newline="").read()
                        if nm:                                     # project, the referencing product (STEP) and the referenced name
                            lrows = [[nm.project(r[0]), r[1], r[2], r[3], nm.file(r[4]), nm.file(r[5])] for r in csv.reader(io.StringIO(ltext))]
                            lbuf = io.StringIO(newline="")
                            csv.writer(lbuf, lineterminator="\r\n").writerows(lrows)
                            ltext = lbuf.getvalue()
                        out.write(ltext.encode("utf-8"))
        rp_rows = []
        for ui in range(len(units)):
            for fpath in chunk_files_of("problems", ui):
                for r in csv.reader(open(fpath, newline="", encoding="utf-8")):
                    rp_rows.append([nm.project(r[0]) if nm else r[0]] + r[1:])
        if rp_rows:
            put("Read_Problems.csv", ["project", "file_no", "ext", "stage", "reason"], rp_rows)
            rlog.event("finalize", f"{len(rp_rows):,} file(s) could not be fully read - Read_Problems.csv gives the file number (see Files.csv) and the reason", "WARN")
        if setup_warnings:
            put("Setup_Warnings.csv", ["where", "path", "reason"], [[w, nm.folder_path(pp) if nm else pp, why] for w, pp, why in setup_warnings])
        if C["unreadable_not_listed"]:
            rlog.event("finalize", f"{C['unreadable_not_listed']:,} more unreadable item(s) than the Unreadable.csv lists hold; the counts in Run_info.csv are exact", "WARN")
        put("Unreadable.csv", ["project", "kind", "path", "reason"],
            [[nm.project(proj_) if nm else proj_, kind,
              ((nm.folder_path(rel) if kind == "folder" else nm.file_path(rel)) if nm else rel) if tier >= 1 else "", why]
             for proj_, uu in man["units"].items() for rel, kind, why in uu.get("unreadable", [])])
        with z.open("Dirs.csv", "w", force_zip64=True) as out:                  # streamed: written 5,000 rows at a time
            out.write(("\ufeff" + ",".join(["dir_id", "project", "parent_dir_id", "depth"] + (["name"] if tier >= 1 else [])) + "\r\n").encode("utf-8"))
            buf_, w_, n_ = io.StringIO(newline=""), None, 0
            w_ = csv.writer(buf_, lineterminator="\r\n")
            for d, v in dir_rows():
                w_.writerow([d, nm.project(v[0]) if nm else v[0], v[1], v[2]]
                            + ([(nm.project(v[3]) if v[2] == 0 and v[3] == v[0] else nm.folder(v[3])) if nm else v[3]] if tier >= 1 else []))
                n_ += 1
                if n_ % 5000 == 0:
                    out.write(buf_.getvalue().encode("utf-8")); buf_.seek(0); buf_.truncate()
            out.write(buf_.getvalue().encode("utf-8"))
        if hit_read:
            put("Content_Hits.csv", ["project", "ext", "files_read_after_the_sample_showed_cad_content"],
                [[nm.project(pj) if nm else pj, ex, n_] for (pj, ex), n_ in sorted(hit_read.items())])
        put("Projects.csv", ["project", "file_count", "total_bytes", "distinct_extensions", "first_modified_utc", "last_modified_utc"],
            [[p, s[0], s[1], len(s[4]), s[2], s[3]] for p, s in sorted(proj_stats.items())])
        put("Extensions.csv", ["project", "ext", "file_count", "total_bytes", "avg_bytes"],
            [[p, e, s[0], s[1], s[1] // s[0]] for (p, e), s in sorted(ext_stats.items())])
        fin_s = time.time() - t_fin
        active = elapsed_before + (time.time() - t_run0)
        busy, calls = act.snapshot()
        proc_s = man["process_s"]
        info = [["tool_version", VERSION], ["run_utc", now_utc()], ["os", platform.platform()], ["python", platform.python_version()],
                ["folder_name", nm.root(label) if nm else label], ["mode", "docker" if os.environ.get("PROFILER_IN_DOCKER") else "native"],
                ["projects", len(units)], ["workers", workers], ["files", nfiles_out], ["total_bytes", bytes_out],
                ["hashing", "none"], ["assembly_names", "anonymized" if (anonymize_assembly_names or nm) else "real"],
                ["names_anonymized", "yes" if nm else "no"],
                ["detail_level", tier], ["project_depth", project_depth],
                ["header_reads_skipped", C["head_skipped"]], ["header_reads_sampled", C["head_sampled"]],
                ["types_with_cad_content_read_in_full", len(hit_types)], ["files_read_after_a_cad_hit", sum(hit_read.values())], ["memory_budget_MB", gate.budget >> 20], ["reads_that_waited_for_memory", gate.waits], ["dwg_sheets_reused_from_identical_copies", C["sheets_reused"]], ["link_files_read", C["link_read"]],
                ["link_files_unreadable", C["link_unreadable"]], ["read_errors", C["errors"]],
                ["read_problems_listed", len(rp_rows)], ["unreadable_not_listed_cap", C["unreadable_not_listed"]], ["setup_warnings", len(setup_warnings)],
                ["cloud_tiered_files_not_opened", sum(uu.get("tiered", 0) for uu in man["units"].values())],
                ["deduplicated_files", sum(uu.get("dedup", 0) for uu in man["units"].values())], ["network_retries", C["retries"]],
                ["unreadable_folders", C["dir_errors"]], ["linked_folders_skipped", walk_counts["links_skipped"]],
                ["ignored_system_files", C["junk"] + sum(u.get("junk", 0) for u in man["units"].values())],
                ["projects_changed_during_run", C["changed_projects"]], ["projects_changed_between_restarts", C["stale_projects"]], ["chunks_redone_after_change", C["stale_chunks"]],
                ["drawing_sheets_counted", C["sheets_counted"]], ["drawing_sheets_not_counted", C["sheets_not_counted"]],
                ["drawing_sheets_status", "; ".join(f"{k.split(':', 1)[1]} x{v}" for k, v in sorted(C.items(), key=lambda kv: -kv[1]) if k.startswith("sheet_status:"))], ["restarts_resumed", man["restarts"]],
                ["peak_memory_mb", _peak_mb()], ["usable_cpus", _usable_cpus()], ["seconds", round(active, 1)], ["seconds_scan", round(man["prescan_s"], 1)], ["seconds_process", round(proc_s, 1)],
                ["seconds_finalize", round(fin_s, 1)],
                ["avg_MB_per_s", round(bytes_out / 1e6 / max(1.0, proc_s + man["prescan_s"]), 1)],
                ["avg_files_per_s", round(nfiles_out / max(1.0, proc_s + man["prescan_s"]))]]
        put("Run_info.csv", ["key", "value"], info)
        z.writestr("WHAT_IS_IN_THIS_ZIP.txt", what_is(tier, anonymize_assembly_names, bool(nm)).encode("utf-8"))
        # ---- shareable run statistics
        phases = [["phase", "wall_seconds", "files", "GB", "MB_per_s", "files_per_s", "note"],
                  ["scan (count files and bytes)", round(man["prescan_s"], 1), total_files, round(total_bytes / 1e9, 2),
                   round(total_bytes / 1e6 / max(0.1, man["prescan_s"]), 1), round(total_files / max(0.1, man["prescan_s"])), "metadata only, no file is opened"],
                  ["process (read tags / references, save chunks)", round(proc_s, 1), nfiles_out, round(bytes_out / 1e9, 2),
                   round(bytes_out / 1e6 / max(0.1, proc_s), 1), round(nfiles_out / max(0.1, proc_s)), "includes any time before a restart"],
                  ["finalize (build the zip)", round(fin_s, 1), nfiles_out, "", "", "", ""]]
        phases += [["", "", "", "", "", "", ""], ["activity (busy seconds summed over all workers)", "busy_seconds", "calls", "", "", "", "share of process wall time x workers"]]
        for k in sorted(busy, key=lambda k: -busy[k]):
            phases.append([k, round(busy[k], 1), calls[k], "", "", "", f"{100 * busy[k] / max(0.1, proc_s * workers):.0f}%"])
        if si_thread is not None:
            si_thread.join(timeout=60)
        sysrows = si_box[0] or [("system_info", "skipped" if not system_info else "not finished in time")]
        rlog.event("finalize", f"done: {nfiles_out:,} files, {bytes_out / 1e9:.1f} GB, scan {man['prescan_s']:.0f}s + process {proc_s:.0f}s + finalize {fin_s:.0f}s")
        mon.stop(); rlog.close()
        log_files = {"Run_log.csv": open(os.path.join(state_dir, "Run_log.csv"), "rb").read(),
                     "Run_metrics.csv": open(os.path.join(state_dir, "Run_metrics.csv"), "rb").read()}
        buf = io.StringIO(newline=""); csv.writer(buf, lineterminator="\r\n").writerows(phases); log_files["Run_phases.csv"] = ("﻿" + buf.getvalue()).encode("utf-8")
        buf = io.StringIO(newline=""); w = csv.writer(buf, lineterminator="\r\n"); w.writerow(["key", "value"]); w.writerows(sysrows)
        log_files["System_info.csv"] = ("﻿" + buf.getvalue()).encode("utf-8")
        for name, data in log_files.items():
            z.writestr(name, data)
    os.replace(tmp_zip, zip_path)
    with zipfile.ZipFile(stats_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in log_files.items():
            z.writestr(name, data)
        z.writestr("README.txt", "Run statistics from the Folder Profiler: timings per step, a CPU / memory / read-speed sample every few seconds, and the "
                                  "machine description. No file names or file contents; project folder names appear in Run_log.csv unless the run was made with name anonymisation.\r\n".encode("utf-8"))
    shutil.rmtree(state_dir, ignore_errors=True)
    try:
        os.rmdir(os.path.join(out_dir, ".resume"))
    except OSError:
        pass
    map_zip = ""
    if nm and name_map:                     # SEPARATE from the profile zip: whoever gets the profile cannot reverse the codes
        map_zip = nm.write_zip(os.path.join(out_dir, f"name_map_{safe}_{stamp}.zip"), label)
    LAST_RUN.update(zip=zip_path, stats_zip=stats_zip, map_zip=map_zip)
    return zip_path, len(units), nfiles_out, C["errors"]


LAST_RUN = {}                # paths of the most recent run's zip and run-statistics zip (used by the browser UI)


def what_is(tier, anonymized, names_anon=False):
    head = ("This archive was made by the Folder Profiler (detail level %d). You can open every file in it with Excel or Notepad.\r\n\r\n" % tier)
    if tier == 0:
        body = ("It contains METRICS only: extensions, sizes, dates and short format tags (no file is hashed), plus anonymous\r\n"
                "folder ids (Dirs.csv) so files can be grouped by folder. No folder names, no paths below the project name, no file\r\n"
                "contents. " + ("The only file names are anonymous codes (ASM-xxxxxxxx) for assembly files.\r\n" if anonymized else
                "The only file names are those of ASSEMBLY files (sldasm, asm, iam, catproduct) in the asm_name column.\r\n"))
    elif tier == 1:
        body = ("Level 1 adds NAMES: Files.csv has rel_path (the file's path inside its project, with folder and file names) and\r\n"
                "Dirs.csv has folder names. Still NO file contents.\r\n")
    else:
        body = ("Level 2 = level 1 (names and paths) plus Links.csv: for assembly, drawing and STEP files, the FILE NAMES of the\r\n"
                "other CAD files they reference (or, for STEP, the product-structure names). Nothing else was copied from inside the\r\n"
                "files - no geometry, no text, no properties. For AutoCAD drawings (.dwg/.dxf) the number of sheets (layouts) is\r\n"
                "counted (columns sheets, sheets_conf of Files.csv); the layout names are not kept.\r\n"
                "Unreadable.csv lists the folders and files that could not be read (so their access can be fixed); files that exist only in the cloud\r\n"
                "(tiered / online-only) are listed but never opened.\r\n")
    if names_anon:
        body += ("NAMES ARE ANONYMISED: project, folder and file names (and the file names referenced in Links.csv) are replaced by codes - "
                 "P-xxxx-nnnn, D-xxxx-nnnnnn, F-xxxx-nnnnnn - with the file extension kept. The same name always has the same code, so the structure is intact. "
                 "The table of codes and real names is NOT in this zip; the data owner keeps it.\r\n")
    return head + body + ("Read_Problems.csv (only when needed) lists files that could not be fully read - file number, type, step and the reason (an error type,\r\n"
                          "never file text). Setup_Warnings.csv (only when needed) lists folders that could not be listed when the projects were chosen.\r\n"
                          "Run_log.csv, Run_phases.csv, Run_metrics.csv and System_info.csv describe how the run went and what machine it ran\r\n"
                          "on (no file names; project folder names appear in Run_log.csv unless the names are anonymised). "
                          "Compressed files (zip, rar, 7z, tar ...) are counted as one file each; nothing is looked into them.\r\n")


def scrub_paths(text, scanned=""):
    """Error logs go to someone else: strip the user's name, home folder and the scanned folder's path."""
    here = os.path.dirname(os.path.abspath(__file__))
    for real, label in ((os.path.abspath(scanned) if scanned else "", "<scanned-folder>"), (here, "<tool-folder>"), (os.path.expanduser("~"), "<home>"),
                        (os.environ.get("USERNAME") or os.environ.get("USER") or "", "<user>")):
        if real and len(real) > 2:
            text = text.replace(real, label).replace(real.replace("\\", "/"), label)
    return text


def make_fail_log(out_dir, problem, scanned="", context=None, ui_log=None, redact_paths=False):
    """After a failed run: ONE zip the data owner can send for remote debugging. It holds the error (traceback or message), the run's own
    log and metrics, the settings and the machine description - never file contents. Paths of the owner's machine are replaced
    (<scanned-folder>, <home>, <user>, <tool-folder>), access tokens are removed, and with redact_paths everything below the scanned
    folder is hidden too. Names of files or folders can still appear inside error messages, so the README tells the owner to look first.
    -> path of the zip."""
    import json as _json
    import tempfile
    try:
        os.makedirs(out_dir, exist_ok=True)
        if not os.access(out_dir, os.W_OK):
            raise OSError("not writable")
    except OSError:                               # the failure may BE the output folder (read-only, full, share gone): use the temp folder
        out_dir = tempfile.gettempdir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, f"fail_log_{stamp}.zip")

    def clean(text):
        text = scrub_paths(str(text), scanned)
        text = re.sub(r"(?i)(bearer|token|authorization)([\s:=\"']+)[A-Za-z0-9._~+/=-]{12,}", r"\1\2<removed>", text)
        if redact_paths:
            text = re.sub(r"(<scanned-folder>)[^\s'\"<>|]*", r"\1/<hidden>", text)
        return text
    files = {}
    files["problem.txt"] = clean(problem or "(no message)")
    ctx = dict(context or {})
    try:
        import sysmetrics
        ram = sysmetrics.total_ram_bytes()
    except Exception:
        ram = 0
    lines = [f"tool_version: {VERSION}", f"time_utc: {now_utc()}", f"python: {platform.python_version()}", f"platform: {platform.platform()}",
             f"cpus: {os.cpu_count()} (usable {_usable_cpus()})", f"ram_gb: {ram / 1024 ** 3:.1f}" if ram else "ram_gb: unknown",
             f"memory_budget_MB: {memory_budget() >> 20}", f"frozen_exe: {bool(getattr(sys, 'frozen', False))}",
             f"in_docker: {bool(os.environ.get('PROFILER_IN_DOCKER'))}"]
    lines += [f"{k}: {clean(v)}" for k, v in sorted(ctx.items())]
    lines += [f"env {k}={clean(v)}" for k, v in sorted(os.environ.items()) if k.startswith("PROFILER_")]
    files["settings.txt"] = "\r\n".join(lines) + "\r\n"
    if ui_log:
        files["screen_log.txt"] = clean("\n".join(ui_log)) + "\n"
    sd = LAST_RUN.get("state_dir") or ""
    for name, tail in (("Run_log.csv", 2000), ("Run_metrics.csv", 600)):
        fp_ = os.path.join(sd, name) if sd else ""
        if fp_ and os.path.exists(fp_):
            try:
                rows = open(fp_, encoding="utf-8", errors="replace").read().splitlines()
                files[name] = clean("\n".join(rows[:1] + rows[1:][-tail:])) + "\n"
            except OSError:
                pass
    mp = os.path.join(sd, "manifest.json") if sd else ""
    if mp and os.path.exists(mp):
        try:
            man_ = _json.load(open(mp, encoding="utf-8"))
            us = man_.get("units", {}).values()
            files["progress.txt"] = (f"projects: {len(man_.get('units', {}))}\r\nprojects complete: {sum(1 for u in us if u.get('complete'))}\r\n"
                                     f"chunks saved: {sum(u.get('chunks_done', 0) for u in us)}\r\nrestarts: {man_.get('restarts')}\r\n"
                                     f"unreadable entries listed: {sum(len(u.get('unreadable', [])) for u in us)}\r\n")
        except Exception:
            pass
    try:
        import sysmetrics
        rows = sysmetrics.system_info((), out_dir, deep=False)
        files["System_info.txt"] = clean("\r\n".join(f"{k}: {v}" for k, v in rows)) + "\r\n"
    except Exception:
        pass
    files["README.txt"] = ("Fail log of the Folder Profiler. It lets the developer see why a run stopped; it contains no file contents.\r\n"
                           "Your computer's paths, user name and any access tokens are removed. The names of files or folders can still appear\r\n"
                           "inside error messages: open problem.txt and screen_log.txt in Notepad and look before you send it.\r\n"
                           + ("Everything below the scanned folder was hidden because name anonymisation was on.\r\n" if redact_paths else ""))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, t in files.items():
            z.writestr(n, t.encode("utf-8"))
    LAST_RUN["fail_log"] = path
    return path


def ask_folder():
    try:
        import tkinter
        from tkinter import filedialog
        r = tkinter.Tk(); r.withdraw()
        p = filedialog.askdirectory(title="Choose the folder to profile")
        r.destroy()
        if p:
            return p
    except Exception:
        pass
    print("Drag the folder into this window (or paste its path), then press Enter:")
    return input("> ").strip().strip('"').strip("'")


def reveal(path):
    if os.environ.get("PROFILER_IN_DOCKER"):
        return
    try:
        if sys.platform == "win32":
            os.system(f'explorer /select,"{path}"')
        elif sys.platform == "darwin":
            os.system(f'open -R "{path}"')
    except Exception:
        pass


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Collect file metrics (never contents) for a folder.")
    ap.add_argument("folder", nargs="?", help="parent folder to profile (asked for if omitted)")
    ap.add_argument("--out", help="where to write the zip (default: ./output next to this script)")
    ap.add_argument("--workers", type=int, help="parallel reader threads (default: auto, max 16; env PROFILER_WORKERS)")
    ap.add_argument("--anonymize-assembly-names", action="store_true",
                    help="write codes like ASM-3F9A1C2B.SLDASM instead of the real assembly file names")
    ap.add_argument("--anonymize-names", action="store_true",
                    help="optional: replace project, folder and file names in the OUTPUT by codes (extensions kept); the code-to-name table is written "
                         "to a separate name_map_*.zip, never into the profile. Works for step 1 and step 2; files are never touched")
    ap.add_argument("--no-paths", action="store_true",
                    help="step 1 only: do NOT write the Paths/<type>.csv lists of every file (project, path, size, date); by default they are written (env PROFILER_PATHS=0 does the same)")
    ap.add_argument("--no-name-map", action="store_true",
                    help="with --anonymize-names: do not write the name_map_*.zip (the codes can then never be turned back into names)")
    ap.add_argument("--level", "--tier", dest="level", type=int, choices=[0, 1, 2], default=None,
                    help="detail level: 0 = metrics only, 1 = + file/folder names and paths, "
                         "2 (default) = + names of files referenced inside assemblies/drawings/STEP (env PROFILER_TIER)")
    ap.add_argument("--project-depth", type=int, default=None,
                    help="projects are the folders this many levels below the chosen one (default 1; env PROFILER_PROJECT_DEPTH)")
    ap.add_argument("--quick", action="store_true",
                    help="STEP 1 only: a very fast count of files per type and size (no file is opened); the full profile is step 2 (default)")
    ap.add_argument("--fresh", action="store_true", help="ignore saved progress of an earlier run and start from the beginning")
    ap.add_argument("--metrics-interval", type=float, default=None, help="seconds between system samples in Run_metrics.csv (default 5)")
    ap.add_argument("--no-system-info", action="store_true", help="do not ask the OS about disks / antivirus (System_info.csv is shorter)")
    ap.add_argument("--chunk-files", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--name", help="folder label for the report (default: the folder's own name; "
                                   "env PROFILER_FOLDER_NAME is used when running in Docker)")
    a = ap.parse_args(argv)
    folder = (a.folder or ask_folder()).strip().strip('"')
    if not folder or not os.path.isdir(folder):
        print(f"Not a folder: {folder!r}")
        return 1
    out_dir = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    label = a.name or os.environ.get("PROFILER_FOLDER_NAME") or None
    if a.quick:
        print(f"Quick count (step 1): {label or folder}\n(read-only; no file is opened; nothing is copied or sent anywhere)\n")
        try:
            qz, nproj, nfiles, errors = quick_scan(folder, out_dir, label=label, workers=a.workers, project_depth=a.project_depth,
                                                   anonymize_names=a.anonymize_names or None, name_map=not a.no_name_map,
                                                   list_paths=False if a.no_paths else None)
        except SystemExit as e:
            print(e)
            return 1
        print(f"\nDone: {nproj} folders, {nfiles:,} files" + (f", {errors} unreadable" if errors else ""))
        print("Top file types: " + ", ".join(f".{e} {n:,}" for e, n, _b in LAST_RUN.get("top", [])[:8]))
        shown = os.path.basename(qz) if os.environ.get("PROFILER_IN_DOCKER") else qz
        print(f"\nPlease send us this ONE file{' (in the output folder)' if os.environ.get('PROFILER_IN_DOCKER') else ''}:\n  {shown}")
        print("Step 2 (the full profile) is the same command without --quick.")
        reveal(qz)
        return 0
    print(f"Profiling: {label or folder}\n(read-only; nothing is copied or sent anywhere)\n")
    try:
        zip_path, nproj, nfiles, errors = profile(folder, out_dir, label=label, workers=a.workers,
                                                    anonymize_assembly_names=a.anonymize_assembly_names or None,
                                                    tier=a.level, project_depth=a.project_depth,
                                                    resume=not a.fresh, metrics_interval=a.metrics_interval,
                                                    system_info=not a.no_system_info, chunk_files=a.chunk_files,
                                                    anonymize_names=a.anonymize_names or None, name_map=not a.no_name_map)
    except SystemExit as e:
        print(e)
        return 1
    except Exception:
        log = os.path.join(out_dir, "error_log.txt")
        os.makedirs(out_dir, exist_ok=True)
        tb = traceback.format_exc()
        open(log, "w").write(scrub_paths(tb, folder))
        try:
            fl = make_fail_log(out_dir, tb, scanned=folder, context={"mode": "full profile", "level": a.level, "workers": a.workers,
                                                                      "anonymize_names": a.anonymize_names}, redact_paths=a.anonymize_names)
            print(f"\nSomething went wrong. Please send us this file: {fl}")
        except Exception:
            print(f"\nSomething went wrong. Please send us this file: {log}")
        return 1
    print(f"\nDone: {nproj} folders, {nfiles:,} files" + (f", {errors} unreadable" if errors else ""))
    if LAST_RUN.get("stats_zip"):
        print(f"Run statistics (timings + system info, safe to share): {os.path.basename(LAST_RUN['stats_zip'])}")
    if os.environ.get("PROFILER_IN_DOCKER"):  # /out is the container's view; the user knows it as ./output
        print(f"\nPlease send us this ONE file (in the 'output' folder next to run-docker):\n  {os.path.basename(zip_path)}")
    else:
        print(f"\nPlease send us this ONE file:\n  {zip_path}")
    reveal(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
