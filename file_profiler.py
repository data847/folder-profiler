"""Folder Profiler — collects file METRICS only. It never copies, uploads or stores file contents, and (since 1.6) it never
hashes a file: nothing is read in full except, at detail level 2, the assembly / drawing / STEP files whose references are listed.

What it writes (one zip of CSVs, nothing else):
  Files.csv       one row per file: project, file_no, ext, size_bytes, modified, depth, header_signature, dir_id, asm_name,
                  (rel_path at level >= 1), archive
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
  * level 2 only: assembly / drawing / STEP files are read to list the FILE NAMES they reference.

Resumable: progress is saved in <out>/.resume/ after every chunk of files; if the run stops for any reason, start it again with the
same settings and it continues from the last saved chunk (--fresh ignores saved progress). No depth limit on folders.

Standard library only. No network code. Files are opened read-only.

Usage:  python file_profiler.py [folder] [--out DIR] [--name LABEL]
        (no folder given -> you are asked for one)
Detail levels: 0 = metrics only (default) | 1 = + file/folder names and paths (rel_path) | 2 = + Links.csv, the NAMES of
files that assemblies/drawings/STEP files reference (read with cad_links.py; nothing else is taken from inside files).
--archives off|list|full looks inside .zip / .tar(.gz) archives (7z/rar listed only): their CAD files become normal rows.
--project-depth N makes the folders N levels down the projects (any N).
Docker: see Dockerfile / run-docker.* - read-only input mount, no network, non-root user.
"""
import collections
import csv
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

try:                                   # detail level 2 / archives only; the profiler works without them
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # also under `python -I` and when frozen
    import cad_links
except ImportError:
    cad_links = None
try:
    import archives
except ImportError:
    archives = None
import sysmetrics
from sysmetrics import now_utc

VERSION = "1.7"
HEAD_BYTES = 4096            # header peek for every file
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
    (b"1upgm               calculix", "SIM:CalculiX"),
    (b"summary of c0nvergence", "SIM:CalculiX"),
    (b"summary of job information", "SIM:CalculiX"),
    (b"calculix", "SIM:CalculiX"),
    (b"abaqus", "SIM:Abaqus"),
    (b"$meshformat", "MESH:GMSH"),
    (b"<vtkfile", "VTK:XML"),
]
LINK_READ_EXTS = {"sldasm", "asm", "pwd", "iam", "slddrw", "dft", "idw", "step", "stp", "catproduct", "catdrawing", "drw", "prt"}   # level 2: assemblies, drawings, STEP
ENTRY_LINK_MAX = 128 * 1024 * 1024      # level 2: archived assemblies/drawings/STEP up to this size are searched for references
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


def signature(path, ext, size):
    """Closed-vocabulary tag describing the file's format. Never contains file text."""
    if size == 0:
        return "EMPTY"
    try:
        with open(path, "rb") as f:
            return _signature_file(f, ext)
    except OSError:
        return "UNREADABLE"


def signature_stream(f, ext, size):
    """Same tag for an already-open binary stream (an entry of an archive)."""
    if size == 0:
        return "EMPTY"
    try:
        return _signature_file(f, ext)
    except Exception:
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


def default_workers():
    env = os.environ.get("PROFILER_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return min(16, (os.cpu_count() or 4) * 2)   # file reads release the GIL, so threads scale


def build_units(root, depth):
    """Projects are the folders `depth` levels below root (1 = folders directly inside it; any depth is allowed). Files sitting in
    the folders above that level become small non-recursive units named "(root)" / "<folder>/(files)"."""
    projects, loose, level = [], [], [("", root)]
    for _ in range(depth):
        nxt = []
        for rel, ab in level:
            try:
                items = sorted(os.listdir(ab), key=str.lower)
            except OSError:
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
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def walk_dirs(base, recurse, on_error, counts):
    """Depth-first, deterministic (sorted) walk with NO depth limit. Yields (dirpath, rel, depth, [file DirEntry]).
    Directory links / junctions are not followed (loops, double counting) and are counted in counts['links_skipped']."""
    stack = [(base, ".", 0)]
    while stack:
        dirpath, rel, depth = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                entries = sorted(it, key=lambda e: e.name)
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


def profile(root, out_dir, log=print, label=None, folders=None, workers=None, progress=None,
            anonymize_assembly_names=None, tier=None, project_depth=None, archive_mode=None,
            archive_budget_gb=None, resume=True, metrics_interval=None, system_info=True, chunk_files=None):
    """Profile a folder tree. Streaming and resumable: every project is processed in chunks that are saved to
    <out_dir>/.resume/<run id>/ as they finish; a run that stops (crash, power cut, Ctrl+C) continues from the last saved chunk when
    started again with the same settings. The state is deleted when the run completes. Nothing is ever hashed except folder
    NAMES (salted, to make anonymous folder ids); file contents are never hashed.
    progress(phase, files_done, files_total, bytes_done, bytes_total): phase "scan" (counting), "process" or "links"."""
    progress = progress or (lambda *a: None)
    t_run0 = time.time()
    workers = max(1, workers or default_workers())
    chunk_files = int(chunk_files or CHUNK_FILES)
    env_int = lambda k, d: int(os.environ[k]) if os.environ.get(k, "").isdigit() else d
    tier = env_int("PROFILER_TIER", 2) if tier is None else int(tier)
    if tier not in (0, 1, 2):
        raise SystemExit("detail level must be 0, 1 or 2")
    if tier == 2 and cad_links is None:
        raise SystemExit("detail level 2 needs cad_links.py next to file_profiler.py")
    project_depth = max(1, env_int("PROFILER_PROJECT_DEPTH", 1) if project_depth is None else int(project_depth))
    archive_mode = archive_mode or os.environ.get("PROFILER_ARCHIVES", "full")
    if archive_mode not in ("off", "list", "full"):
        raise SystemExit("archives must be 'off', 'list' or 'full'")
    if archives is None:
        archive_mode = "off"
    try:
        budget_gb = float(archive_budget_gb if archive_budget_gb is not None else os.environ.get("PROFILER_ARCHIVE_GB", 20))
    except ValueError:
        raise SystemExit("archive budget must be a number of GB")
    if tier >= 1:
        anonymize_assembly_names = False
    if anonymize_assembly_names is None:
        anonymize_assembly_names = os.environ.get("PROFILER_ANON_ASM", "").lower() in ("1", "true", "yes")

    # ---- what to profile
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

    # ---- run identity, checkpoint state
    cfg = json.dumps({"v": VERSION, "units": [[u[0], u[1], u[2]] for u in units], "tier": tier, "depth": project_depth,
                      "archives": archive_mode, "anon": bool(anonymize_assembly_names), "budget": budget_gb,
                      "chunk": chunk_files}, sort_keys=True)
    run_id = f"{zlib.crc32(cfg.encode()):08x}{zlib.adler32(cfg.encode()):08x}"
    state_dir = os.path.join(out_dir, ".resume", run_id)
    if not resume:
        shutil.rmtree(state_dir, ignore_errors=True)
    for sub in ("rows", "links", "dirs"):
        os.makedirs(os.path.join(state_dir, sub), exist_ok=True)
    mpath = os.path.join(state_dir, "manifest.json")
    man = None
    if os.path.exists(mpath):
        try:
            man = json.load(open(mpath, encoding="utf-8"))
            if man.get("cfg") != cfg:
                man = None
        except Exception:
            man = None
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
    dirs = {}                                           # dir_id -> (project, parent_dir_id, depth, name)

    def reg(proj, rel, depth, name, parent=None):
        i = ident(proj, rel)
        if i not in dirs:
            if parent is None:
                parent = "" if rel == "." else ident(proj, os.path.dirname(rel) or ".")
            dirs[i] = (proj, parent, depth, name)
        return i

    # ---- logs, metrics, system info (Run_log.csv / Run_metrics.csv live in the state dir so they survive a restart)
    t0 = time.time()
    rlog = sysmetrics.RunLog(os.path.join(state_dir, "Run_log.csv"), t0=t0, echo=log)
    act = sysmetrics.Activity()
    state = {"phase": "start", "files": 0, "bytes": 0}
    mon = sysmetrics.Monitor(os.path.join(state_dir, "Run_metrics.csv"), state,
                             interval=metrics_interval or env_int("PROFILER_METRICS_INTERVAL", 5), t0=t0)
    mon.start()
    rlog.event("start", ("RESUMED run (restart #%d) " % man["restarts"] if resumed else "started ") +
               f"v{VERSION}: level {tier}, project depth {project_depth}, archives {archive_mode}, {workers} workers, "
               f"{len(units)} projects, hashing: none")

    def save_manifest():
        man["elapsed"] = elapsed_before + (time.time() - t_run0)
        _save_json(mpath, man)

    def on_walk_error(e):
        C["dir_errors"] += 1
        C["errors"] += 1
        rlog.event(state["phase"], f"unreadable folder skipped ({type(e).__name__})", "WARN")

    walk_counts = collections.Counter()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        # ================================================================= scan (count) - cached per project in the manifest
        state["phase"] = "scan"
        total_files = total_bytes = 0
        t_scan = time.time()
        for ui, (proj, base, recurse) in enumerate(units):
            u = man["units"].setdefault(proj, {})
            if "files" not in u:
                nfiles = nbytes = narch = njunk = 0
                for dirpath, rel, depth, files in walk_dirs(lp(base), recurse, on_walk_error, walk_counts):
                    for e in files:
                        if e.name.lower() in SYSTEM_JUNK or e.name.startswith("._"):
                            njunk += 1
                            continue
                        try:
                            nbytes += e.stat().st_size
                        except OSError:
                            pass
                        nfiles += 1
                        if archive_mode != "off" and archives.kind_of(e.name):
                            narch += 1
                    state["files"] = total_files + nfiles
                    if time.time() - t_scan > 0.3:
                        progress("scan", total_files + nfiles, total_files + nfiles, 0, total_bytes + nbytes)
                        t_scan = time.time()
                u.update(files=nfiles, bytes=nbytes, archives=narch, junk=njunk)
                save_manifest()
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

        budget_left = [int(budget_gb * 1024 ** 3) - C["arch_spent"]]
        budget_lock = threading.Lock()

        def columns():
            return (["project", "file_no", "ext", "size_bytes", "modified_utc", "depth", "header_signature", "dir_id", "asm_name"]
                    + (["rel_path"] if tier >= 1 else []) + ["archive"])

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
                    rlog.event("process", f"[{ui + 1}/{len(units)}] {proj}: changed since it was saved in an earlier run - checking it again", "WARN")
                else:
                    rlog.event("process", f"[{ui + 1}/{len(units)}] {proj}: unchanged since an earlier run ({time.time() - t_v:.1f}s check) - skipped")
                    continue
            elif u.get("complete"):
                rlog.event("process", f"[{ui + 1}/{len(units)}] {proj}: already done in an earlier run - skipped")
                continue
            t_unit = time.time()
            chunks_done = u.get("chunks_done", 0)
            if chunks_done:
                rlog.event("process", f"[{ui + 1}/{len(units)}] {proj}: resuming after chunk {chunks_done}")
                state["files"] += min(u.get("files", 0), chunks_done * chunk_files)
            n = 0
            pending, chunk_idx = [], 0
            arch_jobs = []                                                    # (relpath, path, dir_id, depth, size, mtime) in walk order
            wbase = lp(base)

            def flush_chunk():
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
                    rlog.event("process", f"{proj}: chunk {chunk_idx + 1} was saved before the folder changed - profiled again", "WARN")
                t_c = time.time()

                def work(e):
                    path, name, ext, size, mtime, dir_id, depth, asm, relpath, nn = e
                    if tier >= 2 and ext in LINK_READ_EXTS and cad_links is not None and size > 0:
                        try:                                   # ONE open: the format tag and the references come from the same read
                            with open(path, "rb") as fh:
                                with act.timer("format_tag_read"):
                                    sig = signature_stream(fh, ext, size)
                                fh.seek(0)
                                with act.timer("reference_read"):
                                    res = cad_links.read_links_file(fh, ext, name, size)
                        except OSError:
                            return "UNREADABLE", None, True
                        return sig, res, True
                    with act.timer("format_tag_read"):
                        sig = signature(path, ext, size)
                    return sig, None, False

                rows, lrows = [], []
                for e, (sig, res, was_link) in zip(pending, pool.map(work, pending)):
                    path, name, ext, size, mtime, dir_id, depth, asm, relpath, nn = e
                    if "UNREADABLE" in sig:
                        C["errors"] += 1
                    rows.append([proj, nn, ext, size, mtime, depth, sig, dir_id, asm] + ([relpath] if tier >= 1 else []) + [""])
                    if was_link:
                        if res is None:
                            C["link_unreadable"] += 1
                        else:
                            C["link_read"] += 1
                            lrows += link_rows(proj, nn, ext, res)
                    state["files"] += 1
                    state["bytes"] += size
                    tick()
                with act.timer("write_checkpoint"):
                    _write_csv_atomic(os.path.join(state_dir, "rows", f"u{ui}_c{chunk_idx:06d}.csv"), rows)
                    _write_csv_atomic(os.path.join(state_dir, "links", f"u{ui}_c{chunk_idx:06d}.csv"), lrows)
                    del fps[chunk_idx:chunk_idx + 1]
                    fps.insert(chunk_idx, fp)
                    u["chunks_done"] = max(u.get("chunks_done", 0), chunk_idx + 1)
                    save_manifest()
                rlog.event("process", f"{proj}: chunk {chunk_idx + 1} saved ({len(rows):,} files, {time.time() - t_c:.1f}s)")
                if _TEST_HOOK:
                    _TEST_HOOK(ui, chunk_idx)
                chunk_idx += 1
                pending = []

            for dirpath, rel, depth, files in walk_dirs(wbase, recurse, lambda e: None, collections.Counter()):   # already counted by the scan
                dir_id = reg(proj, rel, depth, proj if rel == "." else os.path.basename(rel))
                for e in files:
                    name = e.name
                    if name.lower() in SYSTEM_JUNK or name.startswith("._"):
                        continue
                    n += 1
                    relpath = name if rel == "." else (rel + os.sep + name).replace(os.sep, "/")
                    try:
                        st = e.stat()
                    except OSError:
                        C["errors"] += 1
                        st = None
                    if archive_mode != "off" and archives.kind_of(name):
                        arch_jobs.append((relpath, e.path, dir_id, depth, st.st_size if st else -1, iso(st.st_mtime) if st else ""))
                    if st is not None:
                        ext = ext_of(name)
                        asm = ""
                        if ext in ASSEMBLY_EXTS:
                            asm = (f"ASM-{ident(proj, rel, name, n=8).upper()}.{ext.upper()}" if anonymize_assembly_names else name)
                        pending.append((e.path, name, ext, st.st_size, iso(st.st_mtime), dir_id, depth, asm, relpath, n))
                    if n % chunk_files == 0:
                        flush_chunk()
            if pending or (n and n % chunk_files):
                flush_chunk()
            if chunk_idx < u.get("chunks_done", 0):             # the project shrank: drop saved chunks past its new end
                for k in range(chunk_idx, u["chunks_done"]):
                    for sub in ("rows", "links"):
                        try:
                            os.remove(os.path.join(state_dir, sub, f"u{ui}_c{k:06d}.csv"))
                        except OSError:
                            pass
                u["chunks_done"] = chunk_idx
                del u.setdefault("fps", [])[chunk_idx:]
            n_loose = n
            if n != u.get("files", n):
                C["changed_projects"] += 1
                rlog.event("process", f"{proj}: the folder changed while profiling or between restarts ({u.get('files')} files when counted, "
                                       f"{n} now) - run with --fresh if you need an exact result for this project", "WARN")
            # ---- archives of this project (processed in batches, saved one archive at a time)
            a_done = u.get("archives_done", 0)
            counts_list = u.setdefault("arch_counts", [])
            afp = zlib.crc32((f"{n_loose}\n" + "\n".join(f"{j[0]}\0{j[4]}\0{j[5]}" for j in arch_jobs)).encode("utf-8", "surrogatepass"))
            if a_done and u.get("arch_fp") != afp:                            # archives changed since they were read: read them all again
                C["stale_archives"] += 1
                rlog.event("process", f"{proj}: the archives changed since they were saved - reading them again", "WARN")
                for sub in ("rows", "links", "dirs"):
                    for j in range(a_done):
                        try:
                            os.remove(os.path.join(state_dir, sub, f"u{ui}_a{j:06d}.csv"))
                        except OSError:
                            pass
                a_done, u["archives_done"] = 0, 0
                counts_list.clear()
            u["arch_fp"] = afp
            base_n = n_loose + sum(counts_list[:a_done])
            for j in range(a_done):                                           # re-register folders of archives finished earlier
                fpath = os.path.join(state_dir, "dirs", f"u{ui}_a{j:06d}.csv")
                if os.path.exists(fpath):
                    for d in csv.reader(open(fpath, newline="", encoding="utf-8")):
                        dirs.setdefault(d[0], (proj, d[1], int(d[2]), d[3]))
            def arch_worker(job):
                relp, apath, adir, adepth = job[:4]
                with act.timer("archive_listing"):
                    ents = archives.list_archive(apath)
                if ents is None:
                    return job, None, {}
                reads = {}
                if archive_mode == "full" and tier >= 2 and cad_links is not None and archives.kind_of(apath) in ("zip", "tar"):
                    want = [(e["chain"], e["inner"]) for e in ents
                            if e["readable"] and ext_of(e["inner"].rsplit("/", 1)[-1]) in LINK_READ_EXTS and e["size"] <= ENTRY_LINK_MAX]
                    if want:
                        for chain, inner, f in archives.read_entries(apath, want):
                            ext = ext_of(inner.rsplit("/", 1)[-1])
                            size = next(x["size"] for x in ents if x["chain"] == chain and x["inner"] == inner)
                            if f is None:
                                continue
                            with budget_lock:
                                ok = budget_left[0] - size >= 0
                                if ok:
                                    budget_left[0] -= size
                            if not ok:
                                reads[(chain, inner)] = "BUDGET"
                                continue
                            with act.timer("archive_entry_read"):
                                try:
                                    data = f.read()
                                    reads[(chain, inner)] = (signature_stream(io.BytesIO(data), ext, size),
                                                             cad_links.read_links_bytes(data, ext, inner.rsplit("/", 1)[-1]), size)
                                except Exception:
                                    reads[(chain, inner)] = ("UNREADABLE", None, size)
                return job, ents, reads

            todo = arch_jobs[a_done:]
            for b in range(0, len(todo), workers):
                batch = todo[b:b + workers]
                for job, ents, reads in pool.map(arch_worker, batch):
                    relp, apath, adir, adepth = job[:4]
                    j = a_done
                    rows, lrows, adirs = [], [], {}
                    if ents is None:
                        C["arch_unreadable"] += 1
                        rlog.event("process", f"{proj}: archive could not be opened ({relp.rsplit('/', 1)[-1]})", "WARN")
                        ents = []
                    else:
                        C["arch_opened"] += 1
                    kind = archives.kind_of(apath)

                    def areg(rel_, parent_, depth_, name_):
                        i = ident(proj, rel_)
                        dirs.setdefault(i, (proj, parent_, depth_, name_))
                        adirs[i] = dirs[i]
                        return i
                    cnt = 0
                    for e in ents:
                        inner = e["inner"]; bname = inner.rsplit("/", 1)[-1]
                        if bname.lower() in SYSTEM_JUNK or bname.startswith("._"):
                            C["junk"] += 1
                            continue
                        cnt += 1
                        nn = base_n + cnt
                        rel_base, d = relp + "!", adepth + 1
                        cur = areg(rel_base, adir, d, os.path.basename(apath))
                        for c in e["chain"]:
                            rel_base += "/" + c + "!"; d += 1
                            cur = areg(rel_base, cur, d, c)
                        for part in inner.split("/")[:-1]:
                            rel_base += "/" + part; d += 1
                            cur = areg(rel_base, cur, d, part)
                        ext = ext_of(bname)
                        asm = ""
                        if ext in ASSEMBLY_EXTS:
                            asm = (f"ASM-{ident(proj, rel_base, bname, n=8).upper()}.{ext.upper()}" if anonymize_assembly_names else bname)
                        sig, lres = "ARCHIVED", None
                        rd = reads.get((e["chain"], inner))
                        if rd == "BUDGET":
                            C["arch_budget_hit"] += 1
                        elif rd:
                            sig, lres = rd[0], rd[1]
                            C["arch_read"] += 1
                            C["arch_spent"] += rd[2]
                            if lres is not None:
                                C["link_read"] += 1
                                lrows += link_rows(proj, nn, ext, lres)
                        rows.append([proj, nn, ext, e["size"], e["mtime"], d + 1, sig, cur, asm] + ([rel_base + "/" + bname] if tier >= 1 else []) + [kind])
                        state["files"] += 1
                        state["bytes"] += e["size"]
                    C["arch_entries"] += cnt
                    counts_list.append(cnt)
                    base_n += cnt
                    with act.timer("write_checkpoint"):
                        _write_csv_atomic(os.path.join(state_dir, "rows", f"u{ui}_a{j:06d}.csv"), rows)
                        _write_csv_atomic(os.path.join(state_dir, "links", f"u{ui}_a{j:06d}.csv"), lrows)
                        _write_csv_atomic(os.path.join(state_dir, "dirs", f"u{ui}_a{j:06d}.csv"),
                                          [[i, v[1], v[2], v[3]] for i, v in adirs.items()])
                        a_done += 1
                        u["archives_done"] = a_done
                        save_manifest()
                    tick()
                    if _TEST_HOOK:
                        _TEST_HOOK(ui, "a%d" % (a_done - 1))
            # ---- project finished: remember its folder tree, mark it complete
            with act.timer("write_checkpoint"):
                _write_csv_atomic(os.path.join(state_dir, "dirs", f"u{ui}.csv"),
                                  [[i, v[1], v[2], v[3]] for i, v in dirs.items() if v[0] == proj])
                u["complete"] = True
                u["loose_files"] = n_loose
                save_manifest()
            rlog.event("process", f"[{ui + 1}/{len(units)}] {proj}: done ({n_loose:,} files + {sum(counts_list):,} in archives, {time.time() - t_unit:.1f}s)")
        man["process_s"] += time.time() - t_proc0
        save_manifest()
    except BaseException as exc:
        rlog.event(state["phase"], f"run stopped: {type(exc).__name__}. Progress is saved - start again with the same settings to resume.", "ERROR")
        save_manifest()
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

    def chunk_files_of(sub, ui):
        d = os.path.join(state_dir, sub)
        pre = f"u{ui}_"
        names = sorted((f for f in os.listdir(d) if f.startswith(pre) and f.endswith(".csv") and f[len(pre)] in "ca"),
                       key=lambda f: (f[len(pre)] != "c", f))          # loose-file chunks first, then archives, each in order
        return [os.path.join(d, f) for f in names]

    all_dirs = dict(dirs)
    for ui in range(len(units)):
        fpath = os.path.join(state_dir, "dirs", f"u{ui}.csv")
        if os.path.exists(fpath):
            for d in csv.reader(open(fpath, newline="", encoding="utf-8")):
                all_dirs.setdefault(d[0], (units[ui][0], d[1], int(d[2]), d[3]))
    tmp_zip = zip_path + ".tmp"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("Files.csv", "w", force_zip64=True) as out:
            out.write(("﻿" + ",".join(columns()) + "\r\n").encode("utf-8"))
            for ui in range(len(units)):
                for fpath in chunk_files_of("rows", ui):
                    text = open(fpath, encoding="utf-8", newline="").read()
                    out.write(text.encode("utf-8"))
                    for row in csv.reader(io.StringIO(text)):
                        proj_, ext_, size_, mod_ = row[0], row[2], int(row[3]), row[4]
                        p = proj_stats.setdefault(proj_, [0, 0, mod_, mod_, set()])
                        p[0] += 1; p[1] += size_; p[2] = min(p[2], mod_); p[3] = max(p[3], mod_); p[4].add(ext_)
                        e = ext_stats.setdefault((proj_, ext_), [0, 0])
                        e[0] += 1; e[1] += size_
                        nfiles_out += 1; bytes_out += size_

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
                        out.write(open(fpath, encoding="utf-8", newline="").read().encode("utf-8"))
        put("Dirs.csv", ["dir_id", "project", "parent_dir_id", "depth"] + (["name"] if tier >= 1 else []),
            [[d, v[0], v[1], v[2]] + ([v[3]] if tier >= 1 else [])
             for d, v in sorted(all_dirs.items(), key=lambda kv: (kv[1][0], kv[1][2], kv[0]))])
        put("Projects.csv", ["project", "file_count", "total_bytes", "distinct_extensions", "first_modified_utc", "last_modified_utc"],
            [[p, s[0], s[1], len(s[4]), s[2], s[3]] for p, s in sorted(proj_stats.items())])
        put("Extensions.csv", ["project", "ext", "file_count", "total_bytes", "avg_bytes"],
            [[p, e, s[0], s[1], s[1] // s[0]] for (p, e), s in sorted(ext_stats.items())])
        fin_s = time.time() - t_fin
        active = elapsed_before + (time.time() - t_run0)
        busy, calls = act.snapshot()
        proc_s = man["process_s"]
        info = [["tool_version", VERSION], ["run_utc", now_utc()], ["os", platform.platform()], ["python", platform.python_version()],
                ["folder_name", label], ["mode", "docker" if os.environ.get("PROFILER_IN_DOCKER") else "native"],
                ["projects", len(units)], ["workers", workers], ["files", nfiles_out], ["total_bytes", bytes_out],
                ["hashing", "none"], ["assembly_names", "anonymized" if anonymize_assembly_names else "real"],
                ["detail_level", tier], ["project_depth", project_depth], ["archives_mode", archive_mode],
                ["archives_opened", C["arch_opened"]], ["archives_unreadable", C["arch_unreadable"]],
                ["archive_entries", C["arch_entries"]], ["archive_entries_read", C["arch_read"]],
                ["archive_budget_hit", C["arch_budget_hit"]], ["link_files_read", C["link_read"]],
                ["link_files_unreadable", C["link_unreadable"]], ["read_errors", C["errors"]],
                ["unreadable_folders", C["dir_errors"]], ["linked_folders_skipped", walk_counts["links_skipped"]],
                ["ignored_system_files", C["junk"] + sum(u.get("junk", 0) for u in man["units"].values())],
                ["projects_changed_during_run", C["changed_projects"]], ["projects_changed_between_restarts", C["stale_projects"]], ["chunks_redone_after_change", C["stale_chunks"]],
                ["archive_sets_redone_after_change", C["stale_archives"]], ["restarts_resumed", man["restarts"]],
                ["seconds", round(active, 1)], ["seconds_scan", round(man["prescan_s"], 1)], ["seconds_process", round(proc_s, 1)],
                ["seconds_finalize", round(fin_s, 1)],
                ["avg_MB_per_s", round(bytes_out / 1e6 / max(1.0, proc_s + man["prescan_s"]), 1)],
                ["avg_files_per_s", round(nfiles_out / max(1.0, proc_s + man["prescan_s"]))]]
        put("Run_info.csv", ["key", "value"], info)
        z.writestr("WHAT_IS_IN_THIS_ZIP.txt", what_is(tier, anonymize_assembly_names).encode("utf-8"))
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
        sysrows = sysmetrics.system_info(data_paths, out_dir, {"workers": workers, "detail_level": tier, "archives_mode": archive_mode,
                                                                "hashing": "none"}, deep=system_info) if system_info else [("system_info", "skipped")]
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
                                  "machine description. No file names, folder names or file contents. Safe to share.\r\n".encode("utf-8"))
    shutil.rmtree(state_dir, ignore_errors=True)
    try:
        os.rmdir(os.path.join(out_dir, ".resume"))
    except OSError:
        pass
    LAST_RUN.update(zip=zip_path, stats_zip=stats_zip)
    return zip_path, len(units), nfiles_out, C["errors"]


LAST_RUN = {}                # paths of the most recent run's zip and run-statistics zip (used by the browser UI)


def what_is(tier, anonymized):
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
                "files - no geometry, no text, no properties.\r\n")
    return head + body + ("Run_log.csv, Run_phases.csv, Run_metrics.csv and System_info.csv describe how the run went and what machine it ran\r\n"
                          "on (no file or folder names). "
                          "Files found INSIDE archives (zip, tar; 7z/rar listed only) appear as normal rows with the archive column set; their\r\n"
                          "names are shown as <archive>!/<inner path> at level >= 1.\r\n")


def scrub_paths(text, scanned=""):
    """Error logs go to someone else: strip the user's name, home folder and the scanned folder's path."""
    here = os.path.dirname(os.path.abspath(__file__))
    for real, label in ((os.path.abspath(scanned) if scanned else "", "<scanned-folder>"), (here, "<tool-folder>"), (os.path.expanduser("~"), "<home>"),
                        (os.environ.get("USERNAME") or os.environ.get("USER") or "", "<user>")):
        if real and len(real) > 2:
            text = text.replace(real, label).replace(real.replace("\\", "/"), label)
    return text


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
    ap.add_argument("--level", "--tier", dest="level", type=int, choices=[0, 1, 2], default=None,
                    help="detail level: 0 = metrics only, 1 = + file/folder names and paths, "
                         "2 (default) = + names of files referenced inside assemblies/drawings/STEP (env PROFILER_TIER)")
    ap.add_argument("--project-depth", type=int, default=None,
                    help="projects are the folders this many levels below the chosen one (default 1; env PROFILER_PROJECT_DEPTH)")
    ap.add_argument("--archives", choices=["off", "list", "full"], default=None,
                    help="look inside .zip/.tar(.gz) archives (7z/rar are listed only, needs bsdtar/tar.exe): off; list = names and "
                         "sizes only; full (default) = also read assemblies/drawings/STEP inside at level 2 (env PROFILER_ARCHIVES)")
    ap.add_argument("--archive-budget-gb", type=float, default=None,
                    help="stop decompressing archive entries after this many GB in total (default 20; env PROFILER_ARCHIVE_GB)")
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
    print(f"Profiling: {label or folder}\n(read-only; nothing is copied or sent anywhere)\n")
    try:
        zip_path, nproj, nfiles, errors = profile(folder, out_dir, label=label, workers=a.workers,
                                                    anonymize_assembly_names=a.anonymize_assembly_names or None,
                                                    tier=a.level, project_depth=a.project_depth,
                                                    archive_mode=a.archives, archive_budget_gb=a.archive_budget_gb,
                                                    resume=not a.fresh, metrics_interval=a.metrics_interval,
                                                    system_info=not a.no_system_info, chunk_files=a.chunk_files)
    except SystemExit as e:
        print(e)
        return 1
    except Exception:
        log = os.path.join(out_dir, "error_log.txt")
        os.makedirs(out_dir, exist_ok=True)
        open(log, "w").write(scrub_paths(traceback.format_exc(), folder))
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
