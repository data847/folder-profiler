"""Folder Profiler — collects file METRICS only. It never copies, uploads or stores file contents.

What it writes (one zip of CSVs, nothing else):
  Files.csv       one row per file: project, file_no, ext, size_bytes, modified, depth, sha256, header_signature
  Projects.csv    one row per project folder: counts, bytes, date range
  Extensions.csv  one row per project + extension: counts, bytes
  Run_info.csv    tool version, date, OS, folder name, totals, read errors

What it does NOT write: folder names or paths below the project name, file names (except ASSEMBLY files, see
below), or any file text.

Assembly Breakdown support (1.2): each file carries `dir_id`, an anonymous folder identifier (salted hash, the salt
is thrown away, so folder names cannot be recovered from it) and Dirs.csv holds the folder tree as ids only. Files
whose extension is an assembly format (sldasm asm iam catproduct) also carry `asm_name` - the real file name, or an
anonymous code such as ASM-3F9A1C2B.SLDASM with --anonymize-assembly-names. No other file name is ever written.

How the file contents are touched (and nothing else):
  * sha256   - one-way fingerprint of the whole file (cannot be turned back into the file)
  * header_signature - computed from the first few KB of certain file types and reduced to a tag
    from the fixed lists in this file (e.g. "STEP:AP214:SolidWorks", "DXF:AC1027:3D", "PDF").
    The raw header text is never kept.

Standard library only. No network code. Files are opened read-only.

Usage:  python file_profiler.py [folder] [--out DIR] [--name LABEL]
        (no folder given -> you are asked for one)
Detail levels: 0 = metrics only (default) | 1 = + file/folder names and paths (rel_path) | 2 = + Links.csv, the NAMES of
files that assemblies/drawings/STEP files reference (read with cad_links.py; nothing else is taken from inside files).
--archives off|list|full looks inside .zip / .tar(.gz) archives (7z/rar listed only): their CAD files become normal rows.
--project-depth N makes the folders N levels down the projects. Smart hashing skips full reads where a hash cannot matter.
Docker: see Dockerfile / run-docker.* - read-only input mount, no network, non-root user.
"""
import collections
import csv
import hashlib
import io
import os
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
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

VERSION = "1.5"
HEAD_BYTES = 4096            # header peek for every file
DXF_SCAN_BYTES = 1_048_576   # DXF only: scan up to 1 MB for 3D entity names (flag only, text discarded)
HASH_CHUNK = 1024 * 1024
PARALLEL_MIN_AVG_BYTES = 256 * 1024   # below this average file size a batch is hashed serially
BATCH = 2000                 # files hashed per parallel batch (bounds memory on huge trees)

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
LINK_READ_EXTS = {"sldasm", "asm", "iam", "slddrw", "dft", "idw", "step", "stp"}   # level 2: assemblies, drawings, STEP
NO_HASH_EXTS = {"zip", "rar", "7z", "gz", "tgz", "tar", "iso", "mp4", "mov", "avi", "mkv", "png", "jpg", "jpeg", "gif",
                "bmp", "tif", "tiff", "dll", "exe", "bak", "old", "tmp"}   # never CAD: not worth reading in full just to hash
CAD_HASH_EXTS = {"sldprt", "sldasm", "slddrw", "par", "psm", "asm", "pwd", "dft", "iam", "ipt", "ipn", "idw", "prt", "catpart",
                 "catproduct", "catdrawing", "x_t", "x_b", "step", "stp", "igs", "iges", "stl", "obj", "3mf", "ifc", "3dm", "f3d",
                 "fcstd", "skp", "rvt", "rfa", "dwg", "dxf", "dwf", "pdf"}   # archive entries of these types are read and fingerprinted
ENTRY_HASH_MAX = 2 * 1024 ** 3          # an archive entry larger than this is listed, never decompressed
ENTRY_MEMORY_MAX = 64 * 1024 * 1024     # entries up to this size are read into memory once (hash + format tag + references together)
ENTRY_LINK_MAX = 128 * 1024 * 1024      # level 2: archived assemblies/drawings/STEP up to this size are searched for references
ASSEMBLY_EXTS = {"sldasm", "asm", "iam", "catproduct"}   # the only files whose NAME is recorded (Assembly Breakdown tab)
SYSTEM_JUNK = {".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db"}  # OS clutter, not project files
MAGICS = [  # (prefix, tag)
    (b"%PDF", "PDF"), (b"\x89PNG\r\n\x1a\n", "PNG"), (b"\xff\xd8\xff", "JPEG"), (b"GIF8", "GIF"),
    (b"BM", "BMP"), (b"PK\x03\x04", "ZIP"), (b"\x1f\x8b", "GZIP"), (b"7z\xbc\xaf\x27\x1c", "7Z"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE2"), (b"SQLite format 3", "SQLITE"),
    (b"MZ", "WINEXE"), (b"\x7fELF", "ELF"), (b"II*\x00", "TIFF"), (b"MM\x00*", "TIFF"),
    (b"RIFF", "RIFF"), (b"<?xml", "XML"), (b"{\\rtf", "RTF"),
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


def _signature_file(f, ext):
    head = f.read(HEAD_BYTES)
    low = head.lower()
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


def sha256_of(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return "UNREADABLE"
    return h.hexdigest()


def default_workers():
    env = os.environ.get("PROFILER_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return min(16, (os.cpu_count() or 4) * 2)   # hashing is I/O + hashlib (releases the GIL), so threads scale


def fingerprint(path, ext, size, do_hash=True):
    return (sha256_of(path) if do_hash else ""), signature(path, ext, size)


def build_units(root, depth):
    """Projects are the folders `depth` levels below root (1 = folders directly inside it). Files sitting in the
    folders above that level become small non-recursive units named "(root)" / "<folder>/(files)"."""
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
    projects = [(rel, ab, True) for rel, ab in level]
    return projects + loose


def lp(path):
    """Windows: prefix with \\\\?\\ so CAD trees deeper than 260 characters can be read."""
    if sys.platform != "win32" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def ext_of(name):
    if name.startswith(".") and name.count(".") == 1:
        return "(none)"
    return os.path.splitext(name)[1].lstrip(".").lower() or "(none)"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def profile(root, out_dir, log=print, label=None, folders=None, workers=None, progress=None,
            anonymize_assembly_names=None, tier=None, project_depth=None, hash_mode=None, archive_mode=None,
            archive_budget_gb=None):
    """root: parent folder (each sub-folder = one project). folders: instead, an explicit list of
    project folders (each one = one project; root is ignored).
    progress(phase, files_done, files_total, bytes_done, bytes_total): phase "scan" (totals still growing)
    then "hash"."""
    progress = progress or (lambda *a: None)
    env_int = lambda k, d: int(os.environ[k]) if os.environ.get(k, "").isdigit() else d
    tier = env_int("PROFILER_TIER", 0) if tier is None else int(tier)
    if tier not in (0, 1, 2):
        raise SystemExit("detail level must be 0, 1 or 2")
    if tier == 2 and cad_links is None:
        raise SystemExit("detail level 2 needs cad_links.py next to file_profiler.py")
    project_depth = max(1, min(6, env_int("PROFILER_PROJECT_DEPTH", 1) if project_depth is None else int(project_depth)))
    hash_mode = hash_mode or os.environ.get("PROFILER_HASH", "smart")
    if hash_mode not in ("smart", "all"):
        raise SystemExit("hash mode must be 'smart' or 'all'")
    archive_mode = archive_mode or os.environ.get("PROFILER_ARCHIVES", "full")
    if archive_mode not in ("off", "list", "full"):
        raise SystemExit("archives must be 'off', 'list' or 'full'")
    if archives is None:
        archive_mode = "off"
    try:
        budget_left = int(float(archive_budget_gb if archive_budget_gb is not None else os.environ.get("PROFILER_ARCHIVE_GB", 20)) * 1024 ** 3)
    except ValueError:
        raise SystemExit("archive budget must be a number of GB")
    if tier >= 1:
        anonymize_assembly_names = False          # names are recorded for every file at level >= 1
    if anonymize_assembly_names is None:
        anonymize_assembly_names = os.environ.get("PROFILER_ANON_ASM", "").lower() in ("1", "true", "yes")
    salt = os.urandom(8).hex()                      # per run, never stored: folder ids cannot be reversed or linked across runs

    def ident(*parts, n=10):
        return hashlib.sha256((salt + "\0" + "\0".join(parts)).encode("utf-8", "surrogatepass")).hexdigest()[:n]
    dirs = {}                                       # dir_id -> (project, parent_dir_id, depth)
    t0 = time.time()
    workers = max(1, workers or default_workers())
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
    else:
        root = os.path.abspath(root)
        label = label or os.path.basename(root) or "folder"
        units = build_units(root, project_depth)
    if not units:
        raise SystemExit("That folder is empty - nothing to profile.")

    files_rows, errors, seen, junk, link_jobs = [], 0, 0, 0, []
    hashed_n = skipped_n = dir_errors = 0
    done_bytes = 0
    last_print = last_log = 0.0

    # Phase 1: list every file (cheap, metadata only) so the second phase knows the real totals.
    #   entry layout: 0 path, 1 file_no, 2 depth, 3 ext, 4 size, 5 mtime, 6 dir_id, 7 asm_name, 8 rel_path, 9 do_hash, 10 archive info|None
    scanned, total_files, total_bytes = [], 0, 0
    counters, gsizes, arch_jobs = {}, collections.Counter(), []
    pool = ThreadPoolExecutor(max_workers=workers)
    for proj, base, recurse in units:
        n, entries = 0, []
        wbase = lp(base)
        def walk_error(e):                                  # an unreadable folder must be reported, not silently skipped
            nonlocal errors, dir_errors
            errors += 1; dir_errors += 1
        for dirpath, _dirs, names in os.walk(wbase, onerror=walk_error):
            rel = "." if dirpath == wbase else os.path.relpath(dirpath, wbase)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            dir_id = ident(proj, rel)
            parent_id = "" if rel == "." else ident(proj, os.path.dirname(rel) or ".")
            dirs[dir_id] = (proj, parent_id, depth, proj if rel == "." else os.path.basename(rel))
            for name in sorted(names):
                if name.lower() in SYSTEM_JUNK or name.startswith("._"):
                    junk += 1
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    errors += 1
                    continue
                n += 1
                ext = ext_of(name)
                asm = ""
                if ext in ASSEMBLY_EXTS:
                    asm = f"ASM-{ident(proj, rel, name, n=8).upper()}.{ext.upper()}" if anonymize_assembly_names else name
                relpath = name if rel == "." else (rel + os.sep + name).replace(os.sep, "/")
                gsizes[st.st_size] += 1
                entries.append([path, n, depth, ext, st.st_size, iso(st.st_mtime), dir_id, asm, relpath, None, None])
                if archive_mode != "off" and archives.kind_of(name):
                    arch_jobs.append((proj, path, relpath, dir_id, depth))
                total_files += 1
                total_bytes += st.st_size
                if time.time() - last_print > 0.3:
                    progress("scan", total_files, total_files, 0, total_bytes)
                    last_print = time.time()
            if not recurse:
                break
        counters[proj] = n
        scanned.append((proj, entries))

    # Phase 1b: look inside archives (listing only - cheap for zip, a full decompression pass for .tar.gz) ...
    arch_entries = {}                                       # project -> archive entries (same layout as above)
    arch_opened = arch_unreadable = arch_listed = 0
    if arch_jobs:
        log(f"  looking inside {len(arch_jobs):,} archive(s) ...")
        for job, ents in zip(arch_jobs, pool.map(lambda j: archives.list_archive(j[1]), arch_jobs)):
            proj, apath, arel, adir, adepth = job
            if ents is None:
                arch_unreadable += 1
                continue
            arch_opened += 1

            def reg(rel, parent, depth, name, proj=proj):
                i = ident(proj, rel)
                dirs.setdefault(i, (proj, parent, depth, name))
                return i
            kind = archives.kind_of(apath)
            for e in ents:
                inner = e["inner"]
                base_name = inner.rsplit("/", 1)[-1]
                if base_name.lower() in SYSTEM_JUNK or base_name.startswith("._"):
                    junk += 1
                    continue
                counters[proj] += 1
                n = counters[proj]
                rel_base, d = arel + "!", adepth + 1
                cur = reg(rel_base, adir, d, os.path.basename(apath))
                for c in e["chain"]:
                    rel_base += "/" + c + "!"; d += 1
                    cur = reg(rel_base, cur, d, c)
                for part in inner.split("/")[:-1]:
                    rel_base += "/" + part; d += 1
                    cur = reg(rel_base, cur, d, part)
                ext = ext_of(base_name)
                asm = ""
                if ext in ASSEMBLY_EXTS:
                    asm = f"ASM-{ident(proj, rel_base, base_name, n=8).upper()}.{ext.upper()}" if anonymize_assembly_names else base_name
                gsizes[e["size"]] += 1
                arch_entries.setdefault(proj, []).append(
                    [apath, n, d + 1, ext, e["size"], e["mtime"], cur, asm, rel_base + "/" + base_name, None,
                     {"kind": kind, "chain": e["chain"], "inner": inner, "readable": e["readable"]}])
                arch_listed += 1
                total_files += 1
                total_bytes += e["size"]

    # ... then decide, with every size in the whole run known, which files need a full read to be fingerprinted.
    # A size that occurs only once in the ENTIRE run cannot have a duplicate anywhere (so copies of whole projects in other
    # folders are still found); never-CAD types are skipped. Hash left blank = not read; the compiler treats blank as unique.
    hashed_arch = 0
    for proj, entries in scanned:
        for e in entries:
            e[9] = hash_mode == "all" or e[4] == 0 or (e[3] not in NO_HASH_EXTS and gsizes[e[4]] > 1)
        hashed_n += sum(1 for e in entries if e[9]); skipped_n += sum(1 for e in entries if not e[9])
    for proj, entries in arch_entries.items():
        for e in entries:
            a_ = e[10]
            want = (archive_mode == "full" and a_["readable"] and a_["kind"] in ("zip", "tar") and e[4] <= ENTRY_HASH_MAX
                    and (e[4] == 0 or (e[3] in CAD_HASH_EXTS and (hash_mode == "all" or gsizes[e[4]] > 1
                                                                 or (tier >= 2 and e[3] in LINK_READ_EXTS)))))
            e[9] = bool(want)
        hashed_arch += sum(1 for e in entries if e[9])
    progress("hash", 0, total_files, 0, total_bytes)


    def flush(batch, proj):
        """Hash/fingerprint one batch in parallel; map() keeps input order so output is deterministic."""
        if workers == 1 or sum(m[4] for m in batch) / len(batch) < PARALLEL_MIN_AVG_BYTES:
            # tiny files are dominated by Python-level work that threads cannot overlap (GIL): serial is faster
            results = (fingerprint(m[0], m[3], m[4], m[9]) for m in batch)
            return _consume(batch, results, proj)
        step = max(1, -(-len(batch) // (workers * 4)))      # a few chunks per worker: per-file tasks cost more than tiny files
        chunks = [batch[i:i + step] for i in range(0, len(batch), step)]
        results = (r for part in pool.map(lambda c: [fingerprint(m[0], m[3], m[4], m[9]) for m in c], chunks) for r in part)
        _consume(batch, results, proj)

    def _consume(batch, results, proj):
        nonlocal errors, seen, last_print, last_log, done_bytes
        for (_p, n, depth, ext, size, mtime, dir_id, asm, relpath, _dh, _arch), (digest, sig) in zip(batch, results):
            if "UNREADABLE" in (digest, sig):
                errors += 1
            files_rows.append([proj, n, ext, size, mtime, depth, digest, sig, dir_id, asm] + ([relpath] if tier >= 1 else []) + [""])
            if tier >= 2 and ext in LINK_READ_EXTS:
                link_jobs.append((proj, n, ext, _p))
            seen += 1
            done_bytes += size
            now = time.time()
            if now - last_print > 0.3:
                progress("hash", seen, total_files, done_bytes, total_bytes)
                last_print = now
            if now - last_log > 1.0:
                log(f"  scanned {seen:,} files ... (now in {proj})")
                last_log = now

    try:
        for proj, entries in scanned:
            for i in range(0, len(entries), BATCH):
                flush(entries[i:i + BATCH], proj)
            log(f"  {proj}: {len(entries):,} files")
        links, arch_link_files = [], 0
        arch_hashed = arch_budget_hit = 0
        if arch_entries:
            # ---- archive entries: one pass per archive; content is read ONCE for hash + format tag (+ references at level 2) ----
            lock = threading.Lock()
            spent = [0]

            def process_archive(job):
                aproj, apath, items = job
                out = {}
                wanted = [(it[10]["chain"], it[10]["inner"]) for it in items if it[9] and it[4] > 0]
                for chain, inner, f in archives.read_entries(apath, wanted):
                    ext = ext_of(inner.rsplit("/", 1)[-1])
                    res = ("", "ARCHIVED", None)
                    if f is not None:
                        size = next(it[4] for it in items if it[10]["chain"] == chain and it[10]["inner"] == inner)
                        with lock:
                            ok = spent[0] + size <= budget_left
                            if ok:
                                spent[0] += size
                        if ok:
                            try:
                                h = hashlib.sha256()
                                if size <= ENTRY_MEMORY_MAX:
                                    data = f.read()
                                    h.update(data)
                                    sig = signature_stream(io.BytesIO(data), ext, size)
                                    lk = None
                                    if tier >= 2 and cad_links is not None and ext in LINK_READ_EXTS and size <= ENTRY_LINK_MAX:
                                        lk = cad_links.read_links_bytes(data, ext, inner.rsplit("/", 1)[-1])
                                else:
                                    head = f.read(HEAD_BYTES)
                                    h.update(head)
                                    for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
                                        h.update(chunk)
                                    sig, lk = signature_stream(io.BytesIO(head), ext, size), None
                                res = (h.hexdigest(), sig, lk)
                            except Exception:
                                res = ("", "UNREADABLE", None)
                        else:
                            res = ("", "ARCHIVED", "BUDGET")
                    out[(chain, inner)] = res
                return job, out

            by_arch = {}
            for aproj, ents in arch_entries.items():
                for it in ents:
                    by_arch.setdefault((aproj, it[0]), []).append(it)
            jobs = [(aproj, apath, items) for (aproj, apath), items in by_arch.items()]
            for (job, out) in pool.map(process_archive, jobs):
                aproj, apath, items = job
                for it in items:
                    a_ = it[10]
                    digest, sig, lk = out.get((a_["chain"], a_["inner"]), ("", "ARCHIVED", None))
                    if it[4] == 0:
                        digest, sig = hashlib.sha256(b"").hexdigest(), "EMPTY"
                    if lk == "BUDGET":
                        arch_budget_hit += 1; lk = None
                    if "UNREADABLE" in (digest, sig):
                        errors += 1
                    if digest:
                        arch_hashed += 1
                    if sig == "ARCHIVED" and not digest:
                        sig = "ARCHIVED"
                    files_rows.append([aproj, it[1], it[3], it[4], it[5], it[2], digest, sig, it[6], it[7]]
                                      + ([it[8]] if tier >= 1 else []) + [a_["kind"]])
                    if lk is not None:
                        arch_link_files += 1
                        if lk[0] == "native":
                            links += [[aproj, it[1], it[3], "native", "", c] for c in lk[1]]
                        else:
                            links += [[aproj, it[1], it[3], "step", par, c] for par, kids in lk[1].items() for c in kids]
                    seen += 1
                    done_bytes += it[4]
            log(f"  archives: {arch_opened:,} opened, {arch_listed:,} entries, {arch_hashed:,} read in full")
    finally:
        pool.shutdown()
    progress("hash", seen, total_files, done_bytes, total_bytes)

    # ---- level 2: references inside assemblies / drawings / STEP (names of referenced files only) ----
    link_read, link_fail = arch_link_files, 0
    if tier >= 2 and link_jobs:
        progress("links", 0, len(link_jobs), 0, 0)
        log(f"  reading references inside {len(link_jobs):,} assembly/drawing/STEP files ...")
        with ThreadPoolExecutor(max_workers=workers) as lpool:
            for k, (job, res) in enumerate(zip(link_jobs, lpool.map(lambda j: cad_links.read_links(j[3], j[2]), link_jobs))):
                lproj, ln, lext, _lp = job
                if res is None:
                    link_fail += 1
                else:
                    link_read += 1
                    if res[0] == "native":
                        links += [[lproj, ln, lext, "native", "", c] for c in res[1]]
                    else:
                        links += [[lproj, ln, lext, "step", par, c] for par, kids in res[1].items() for c in kids]
                if (k + 1) % 200 == 0 or k + 1 == len(link_jobs):
                    progress("links", k + 1, len(link_jobs), 0, 0)

    # ---- aggregates ----
    proj_stats, ext_stats = {}, {}
    for row in files_rows:
        proj, ext, size, mod = row[0], row[2], row[3], row[4]
        p = proj_stats.setdefault(proj, [0, 0, mod, mod, set()])
        p[0] += 1; p[1] += size; p[2] = min(p[2], mod); p[3] = max(p[3], mod); p[4].add(ext)
        e = ext_stats.setdefault((proj, ext), [0, 0])
        e[0] += 1; e[1] += size

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(out_dir, f"profile_{re.sub(r'[^A-Za-z0-9_.-]', '_', label)}_{stamp}.zip")

    def table(header, rows):
        import io
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(header); w.writerows(rows)
        return buf.getvalue()

    tables = {
        "Files.csv": table(["project", "file_no", "ext", "size_bytes", "modified_utc", "depth", "sha256",
                            "header_signature", "dir_id", "asm_name"] + (["rel_path"] if tier >= 1 else []) + ["archive"], files_rows),
        "Dirs.csv": table(["dir_id", "project", "parent_dir_id", "depth"] + (["name"] if tier >= 1 else []),
                          [[d, v[0], v[1], v[2]] + ([v[3]] if tier >= 1 else [])
                           for d, v in sorted(dirs.items(), key=lambda kv: (kv[1][0], kv[1][2], kv[0]))]),
        **({"Links.csv": table(["project", "file_no", "ext", "kind", "parent", "child"], links)} if tier >= 2 else {}),
        "Projects.csv": table(["project", "file_count", "total_bytes", "distinct_extensions",
                               "first_modified_utc", "last_modified_utc"],
                              [[p, s[0], s[1], len(s[4]), s[2], s[3]] for p, s in sorted(proj_stats.items())]),
        "Extensions.csv": table(["project", "ext", "file_count", "total_bytes", "avg_bytes"],
                                [[p, e, s[0], s[1], s[1] // s[0]] for (p, e), s in sorted(ext_stats.items())]),
        "Run_info.csv": table(["key", "value"], [
            ["tool_version", VERSION], ["run_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")],
            ["os", platform.platform()], ["python", platform.python_version()],
            ["folder_name", label], ["mode", "docker" if os.environ.get("PROFILER_IN_DOCKER") else "native"],
            ["projects", len(units)], ["workers", workers],
            ["assembly_names", "anonymized" if anonymize_assembly_names else "real"],
            ["detail_level", tier], ["project_depth", project_depth], ["hash_mode", hash_mode],
            ["files_hashed", hashed_n], ["files_hash_skipped", skipped_n],
            ["archives_mode", archive_mode], ["archives_opened", arch_opened], ["archives_unreadable", arch_unreadable],
            ["archive_entries", arch_listed], ["archive_entries_read", arch_hashed], ["archive_budget_hit", arch_budget_hit],
            ["link_files_read", link_read], ["link_files_unreadable", link_fail], ["link_rows", len(links)],
            ["files", len(files_rows)], ["total_bytes", sum(r[3] for r in files_rows)],
            ["read_errors", errors], ["unreadable_folders", dir_errors], ["ignored_system_files", junk], ["seconds", round(time.time() - t0, 1)]]),
        "WHAT_IS_IN_THIS_ZIP.txt": what_is(tier, anonymize_assembly_names),
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in tables.items():
            z.writestr(name, text.encode("utf-8-sig") if name.endswith(".csv") else text.encode("utf-8"))
    return zip_path, len(units), len(files_rows), errors


def what_is(tier, anonymized):
    head = ("This archive was made by the Folder Profiler (detail level %d). You can open every file in it with Excel or Notepad.\r\n\r\n" % tier)
    if tier == 0:
        body = ("It contains METRICS only: extensions, sizes, dates, SHA-256 fingerprints and short format tags, plus anonymous\r\n"
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
    return head + body + ("Files found INSIDE archives (zip, tar; 7z/rar listed only) appear as normal rows with the archive column set; their\r\n"
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
    ap.add_argument("--workers", type=int, help="parallel hashing threads (default: auto, max 16; env PROFILER_WORKERS)")
    ap.add_argument("--anonymize-assembly-names", action="store_true",
                    help="write codes like ASM-3F9A1C2B.SLDASM instead of the real assembly file names")
    ap.add_argument("--level", "--tier", dest="level", type=int, choices=[0, 1, 2], default=None,
                    help="detail level: 0 = metrics only (default), 1 = + file/folder names and paths, "
                         "2 = + names of files referenced inside assemblies/drawings/STEP (env PROFILER_TIER)")
    ap.add_argument("--project-depth", type=int, default=None,
                    help="projects are the folders this many levels below the chosen one (default 1; env PROFILER_PROJECT_DEPTH)")
    ap.add_argument("--hash", dest="hash_mode", choices=["smart", "all"], default=None,
                    help="smart (default): skip full-file hashing where it cannot matter (unique size in project, non-CAD types); "
                         "all: hash every file")
    ap.add_argument("--archives", choices=["off", "list", "full"], default=None,
                    help="look inside .zip/.tar(.gz) archives (7z/rar are listed only, needs bsdtar/tar.exe): off; list = names and "
                         "sizes only; full (default) = also fingerprint CAD files inside (env PROFILER_ARCHIVES)")
    ap.add_argument("--archive-budget-gb", type=float, default=None,
                    help="stop decompressing archive entries after this many GB in total (default 20; env PROFILER_ARCHIVE_GB)")
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
                                                    tier=a.level, project_depth=a.project_depth, hash_mode=a.hash_mode,
                                                    archive_mode=a.archives, archive_budget_gb=a.archive_budget_gb)
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
    if os.environ.get("PROFILER_IN_DOCKER"):  # /out is the container's view; the user knows it as ./output
        print(f"\nPlease send us this ONE file (in the 'output' folder next to run-docker):\n  {os.path.basename(zip_path)}")
    else:
        print(f"\nPlease send us this ONE file:\n  {zip_path}")
    reveal(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
