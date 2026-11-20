"""Step 2 of the Folder Profiler: the CAD project scan. Implements the rules of the 'Field Logic' tab of the LH2 CAD Project Scan sheet.

Reads every file (content, not just names), expands archives, splits the folder tree into projects, finds blank / duplicate / import-debris /
junk files, and writes ONE workbook: Read Me, Field Logic, Projects, Real Files, Excluded Files, Project Map, Not Checked.
Only names, paths, sizes, dates, hashes and counts are written; no geometry or document text leaves the machine. Files are opened read-only.
Standard library only (cad_links.py and xlsx_min.py sit next to this file).

Rule numbers in the comments (0.1, 1.2 ...) are the row numbers of the Field Logic tab; scan_docs.json holds that tab verbatim.
Deviations from, and gaps in, those rules are listed in LIMITS (written to the workbook's Read Me tab) - the script claims no more than it did.
"""
import bisect
import collections
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cad_links  # noqa: E402
import xlsx_min  # noqa: E402
from xlsx_min import F, MAX_ROWS  # noqa: E402

SCAN_VERSION = "2.0"
MAX_DEPTH = 5                                   # 0.3: archive inside archive
READ_CAP = 256 * 1024 * 1024                    # files above this are hashed but their content is not read
MAX_NESTED_BYTES = 1024 * 1024 * 1024           # a nested archive is held in memory while it is opened
PART_EXTS = {"par", "psm", "sldprt", "ipt", "prt", "catpart"}
ASM_EXTS = {"asm", "sldasm", "iam", "catproduct"}
DRAW_EXTS = {"dft", "dwg", "dxf", "slddrw", "idw", "drw", "catdrawing"}
EXCH_EXTS = {"stp", "step", "igs", "iges", "x_t", "x_b", "xmt_txt", "sat", "jt", "stl", "3mf", "obj"}
VERSION_RE = re.compile(r"\.(prt|asm|drw)\.(\d+)$", re.I)                     # Creo: name.prt.7
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
JUNK_EXTS = {"selock", "lck", "tmp", "crdownload", "download", "partial",       # 5.2 (b) system, lock and temp files
             "exe", "dll", "msi", "mst", "cab", "sys",                           # (d) installers and binaries
             "lnk"}                                                              # (e) shortcuts
SIDE_EXTS = {"cfg", "log", "err", "bak"}                                         # (c) CAD side-files, only next to a same-named CAD file
LIB_WORDS = ("std", "standard", "library", "toolbox", "vendor", "bought out", "downloads")
ROLE_NAMES = {"3d", "2d", "cad", "step", "iges", "drafts", "drawings", "dwg", "dxf", "pdf", "parts", "components", "asm", "assembly",
              "sub assembly", "std parts", "bom", "docs", "images", "renders", "old", "backup", "archive", "final", "concept", "fixture", "layout"}
ROLE_RE = re.compile(r"^(rev\s*\d+|r\d+|v\d+|new folder.*)$", re.I)
FINAL_WORDS = ("final", "complete", "main", "total")
DEBRIS_RE = re.compile(r"^(fillet \d+|product_id|imported)", re.I)
STEP_GEOM = (b"MANIFOLD_SOLID_BREP", b"BREP_WITH_VOIDS", b"SHELL_BASED_SURFACE_MODEL", b"FACETED_BREP", b"GEOMETRIC_CURVE_SET")
SE_BODY_STREAMS = ("STREAM0.X_B", "STREAM0.P_B")
PARASOLID_MARK = b"**ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TIER_MASK = 0x1000 | 0x40000 | 0x100000 | 0x400000      # OFFLINE | RECALL_ON_OPEN | UNPINNED | RECALL_ON_DATA_ACCESS
SF_DATALESS = 0x40000000                                 # macOS: online-only file
FUZZY_PCT = 2.0                                          # 1.4: same body length, under 2% of bytes different
FUZZY_MAX_CLUSTERS = 40                                  # comparisons per length group are capped (cost)
GA_WORDS = ("general arrangement", "final", "complete", "overall", "layout")
CAD_CATS = ("Part", "Assembly", "Drawing", "Exchange")

LIMITS = [
    "Duplicates are found WITHIN a project only (a part copied into two projects is real in both).",
    "Component names are matched by file name, nearest folder first; the full path stored in an assembly is not read, so two different parts that share a file name can be confused (7.3).",
    "Part Linkage: a referenced file that is Not Checked (cloud-only, unreadable) counts as Found - Real.",
    "Blank test for parts is made for Solid Edge only (STREAM0.X_B / STREAM0.P_B); other formats are never Blank unless 0-byte (1.2).",
    "Drawings: DXF are tested for an empty ENTITIES section; DWG only for 0-byte / bad header (no reliable empty-sheet test without a CAD kernel, 3.2).",
    "Exchange files: only STEP, STL and IGES are tested for geometry; other formats are Blank only when 0-byte.",
    "The Parasolid-marker self-check (1.2) skips streams whose name contains 'sketch'.",
    "A same-length Solid Edge body that differs by under 2% is compared byte by byte when the file can be re-opened (disk or zip); inside rar / 7z / tar it falls back to exact-body matches only.",
    "rar and 7z archives need 7-Zip, unrar or bsdtar on the machine (or py7zr for 7z); without them each such archive is listed in Not Checked.",
    "Files above 256 MB are hashed but their content is not read: assemblies of that size go to Not Checked, other files are kept as real.",
    "A folder whose name marks it as a library (std, standard, library ...) and that holds any CAD file is always its own project, even below the 20-file / 1-assembly candidate size.",
    "Library rule 2 (referenced by assemblies in 3+ other projects) is applied to the finished projects, after the split, not before it.",
    "Folders that cannot be listed are in Not Checked as '(folder)' rows; they are not files, so they are not in any Total Files.",
    "Not resumable: a stopped scan starts again from the beginning.",
]


# =====================================================================================================  records
class Rec:
    __slots__ = ("pdir", "arch", "inner", "dir", "name", "name_l", "ext", "cat", "size", "mtime", "md5", "status", "reason", "same_as",
                 "refs", "kind", "body_md5", "body_len", "check", "pid", "comp", "res")

    def __init__(self, pdir, arch, inner, name, size, mtime):
        self.pdir, self.arch, self.inner, self.name, self.size, self.mtime = pdir, arch, inner, name, size, mtime
        self.name_l = name.lower()
        self.ext, self.cat = classify(name)
        self.dir = virtual_dir(pdir, arch, inner)
        self.md5 = self.body_md5 = self.same_as = self.refs = self.kind = self.comp = self.res = None
        self.body_len = 0
        self.status, self.reason, self.check, self.pid = "real", "", "", None


def virtual_dir(pdir, arch, inner):
    """Folder a file belongs to for the project split. 0.3: archive members are files of the folder the archive sits in, so the archive's own
    name is NOT a path component (a copy of a folder inside an archive lands on the same folder as the extracted copy). Folders inside an
    archive (and the folders a nested archive sits in) are kept."""
    d = tuple(pdir)
    for k, a in enumerate(arch):
        if k:                                              # a nested archive: the folders it sits in inside its parent archive
            d += tuple(c for c in re.split(r"[\\/]", a) if c)[:-1]
    return d + tuple(inner)


def classify(name):
    low = name.lower()
    m = VERSION_RE.search(low)
    ext = (m.group(1) if m else low.rsplit(".", 1)[-1]) if "." in low else ""
    if ext in PART_EXTS:
        return ext, "Part"
    if ext in ASM_EXTS:
        return ext, "Assembly"
    if ext in DRAW_EXTS:
        return ext, "Drawing"
    if ext in EXCH_EXTS:
        return ext, "Exchange"
    return ext, "Other"


def lp(path):
    """Long-path form on Windows."""
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        path = os.path.abspath(path)
        return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
    return path


def iso(ts):
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def is_cloud(st):
    return bool(getattr(st, "st_file_attributes", 0) & TIER_MASK or getattr(st, "st_flags", 0) & SF_DATALESS)


# =====================================================================================================  content readers
def _entry_bytes(cfb, e):
    if e["size"] < cfb.cutoff:
        buf = b"".join(cfb.ministream[s * cfb.ms:(s + 1) * cfb.ms] for s in cfb._mini_chain(e["start"]))
        return buf[:e["size"]]
    return cfb._read_chain(e["start"], e["size"])


def se_body(data):
    """1.2: Solid Edge part -> ("body", md5, length) | ("none",) | ("marker",) | ("bad",)."""
    if data[:8] != cad_links.OLE_MAGIC:
        return ("bad",)
    try:
        cfb = cad_links._CFB(data)
    except Exception:
        return ("bad",)
    try:
        for e in cfb.entries:
            if e["type"] == 2 and e["name"].upper() in SE_BODY_STREAMS and e["size"] > 0:
                body = _entry_bytes(cfb, e)
                if body:
                    return ("body", hashlib.md5(body).hexdigest(), len(body))
        for name, blob in cfb.streams():                 # no body stream: is a body hiding somewhere that is not a sketch?
            if "sketch" not in name.lower() and PARASOLID_MARK in blob:
                return ("marker",)
    except Exception:
        return ("bad",)
    return ("none",)


def se_body_bytes(data):
    try:
        cfb = cad_links._CFB(data)
        for e in cfb.entries:
            if e["type"] == 2 and e["name"].upper() in SE_BODY_STREAMS and e["size"] > 0:
                return _entry_bytes(cfb, e)
    except Exception:
        pass
    return None


def step_has_geometry(f):
    tail = b""
    while True:
        blk = f.read(16 * 1024 * 1024)
        if not blk:
            return False
        buf = tail + blk
        if any(k in buf for k in STEP_GEOM):
            return True
        tail = buf[-40:]


def iges_has_geometry(f):
    """IGES: fixed 80-column records; the D section (type 'D' in column 73) holds two lines per entity, entity type in columns 1-8 of the first."""
    n = 0
    for raw in f:
        line = raw.rstrip(b"\r\n")
        if len(line) >= 73 and line[72:73] == b"D":
            n += 1
            if n % 2 == 1:
                try:
                    t = int(line[:8])
                except ValueError:
                    continue
                if (100 <= t <= 199 and t != 124) or 500 <= t <= 520:
                    return True
        elif len(line) >= 73 and line[72:73] in (b"P", b"T"):
            break
    return False


def dxf_has_entities(f):
    """ASCII DXF: is there anything between ENTITIES and ENDSEC? Binary DXF is not judged (reported as having content)."""
    head = f.read(22)
    if head.startswith(b"AutoCAD Binary DXF"):
        return True
    f.seek(0)
    prev = b""
    for raw in f:
        s = raw.strip()
        if prev == b"2" and s == b"ENTITIES":
            nxt = []
            for raw2 in f:
                nxt.append(raw2.strip())
                if len(nxt) == 2:
                    break
            return not (len(nxt) == 2 and nxt[0] == b"0" and nxt[1] == b"ENDSEC")
        prev = s
    return True                                           # no ENTITIES section found: not claimed empty


def stl_triangles(f, size):
    head = f.read(256)
    f.seek(0)
    if head[:5].lower() == b"solid" and b"facet" in head.lower():
        n = 0
        for raw in f:
            if raw.lstrip()[:5].lower() == b"facet":
                return 1
        return n
    if size < 84:
        return 0
    f.seek(80)
    return struct.unpack("<I", f.read(4))[0]


class Gate:
    """Caps the bytes of whole-file reads in flight."""

    def __init__(self, budget):
        self.budget, self.used, self.cv = int(budget), 0, threading.Condition()

    def hold(self, cost):
        gate = self

        class _H:
            def __enter__(s):
                s.c = min(int(cost), gate.budget)
                with gate.cv:
                    while gate.used + s.c > gate.budget:
                        gate.cv.wait()
                    gate.used += s.c

            def __exit__(s, *a):
                with gate.cv:
                    gate.used -= s.c
                    gate.cv.notify_all()
        return _H()


def md5_stream(f, first=b""):
    h = hashlib.md5(first)
    while True:
        b = f.read(4 * 1024 * 1024)
        if not b:
            return h.hexdigest()
        h.update(b)


# =====================================================================================================  scan context
class Ctx:
    def __init__(self, root, workers, download_cloud, host):
        self.root, self.workers, self.download_cloud, self.host = root, workers, download_cloud, host
        self.gate = Gate(host.memory_budget() if host else 512 * 1024 * 1024)
        self.lock = threading.Lock()
        self.recs, self.nc_dirs = [], []          # nc_dirs: (dir tuple, reason) for folders that could not be listed
        self.files_seen = self.bytes_seen = 0
        self.total_files = self.total_bytes = 0
        self.archives = self.archive_members = 0
        self.cloud_skipped = 0
        self.read_errors = collections.Counter()
        self.tools = find_archive_tools()
        self.tmp_root = None
        self.zip_cache = _ZipCache()
        self.progress = lambda *a: None
        self.checkpoint = lambda: None


def find_archive_tools():
    t = {}
    cands = ["7z", "7zz", "7za", "7zr"]
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            p = os.path.join(base, "7-Zip", "7z.exe")
            if os.path.isfile(p):
                t["7z"] = p
    for c in cands:
        if "7z" not in t and shutil.which(c):
            t["7z"] = shutil.which(c)
    for c in ("unrar", "bsdtar"):
        if shutil.which(c):
            t[c] = shutil.which(c)
    try:
        import py7zr  # noqa: F401
        t["py7zr"] = True
    except Exception:
        pass
    return t


# =====================================================================================================  per-file analysis
def analyse(ctx, rec, opener, size):
    """Fills status / reason / hashes of one file. `opener()` returns a fresh binary file object."""
    cat, ext = rec.cat, rec.ext
    if size == 0:
        rec.md5 = hashlib.md5(b"").hexdigest()
        if cat == "Other":
            rec.status, rec.reason = "junk", "0-byte file"
        else:
            rec.status, rec.reason, rec.check = "blank", "0-byte file", "0 bytes"
        return
    need_data = cat in ("Part", "Assembly") or (cat == "Drawing" and ext in ("dft", "idw", "slddrw")) \
        or (cat == "Exchange" and ext in ("stp", "step"))
    try:
        with opener() as f:
            if need_data and size <= READ_CAP:
                with ctx.gate.hold(size * 2):
                    data = f.read()
                rec.md5 = hashlib.md5(data).hexdigest()
                _judge(ctx, rec, data, None)
            else:
                rec.md5 = md5_stream(f)
                f.seek(0)
                _judge(ctx, rec, None, f)
    except (OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, ValueError, RuntimeError) as exc:
        rec.status, rec.reason = "nc", f"could not be read ({type(exc).__name__})"
        ctx.read_errors[type(exc).__name__] += 1


def _judge(ctx, rec, data, f):
    cat, ext, size = rec.cat, rec.ext, rec.size
    if cat == "Part":
        if ext in ("par", "psm") and data is not None:
            r = se_body(data)
            if r[0] == "body":
                rec.body_md5, rec.body_len, rec.check = r[1], r[2], "3D body found"
            elif r[0] == "none":
                rec.status, rec.reason, rec.check = "blank", "No 3D body (no STREAM0.X_B / STREAM0.P_B)", "No 3D body"
            elif r[0] == "marker":
                rec.status, rec.reason = "nc", "No body stream, but a Parasolid body marker outside a sketch stream"
            else:
                rec.status, rec.reason, rec.check = "blank", "Bad file header / cannot be opened as a Solid Edge document", "Bad header"
        elif ext in ("par", "psm"):
            rec.status, rec.reason = "nc", "Too large to read (over 256 MB): body not checked"
        else:
            rec.check = "Byte hash (no body check)"
    elif cat == "Assembly":
        if data is None:
            rec.status, rec.reason = "nc", "Too large to read (over 256 MB)"
            return
        rd = ext if ext in ("sldasm", "iam", "catproduct", "asm") else "asm"
        r = cad_links.read_links_bytes(data, rd, rec.name)
        if r is None:
            rec.status, rec.reason = "nc", "Assembly could not be parsed"
        else:
            rec.kind, refs = r[0], r[1]
            if not isinstance(refs, list):
                rec.status, rec.reason = "nc", "Assembly could not be parsed"
            elif not refs:
                rec.status, rec.reason, rec.check = "blank", "No component names stored in the assembly", "No components"
            else:
                rec.refs, rec.comp = tuple(refs), tuple(refs)
                rec.check = "Has components" + (" (names found by text scan)" if rec.kind == "scan" else "")
    elif cat == "Drawing":
        rec.check = "Byte hash"
        if ext in ("dft", "idw") and data is not None and data[:8] != cad_links.OLE_MAGIC:
            rec.status, rec.reason, rec.check = "blank", "Bad file header / cannot be opened", "Bad header"
        elif ext in ("dft", "slddrw", "idw") and data is not None:
            r = cad_links.read_links_bytes(data, ext, rec.name)
            if r is not None and isinstance(r[1], list):
                rec.refs = tuple(r[1])
        elif ext == "dxf" and f is not None:
            if not dxf_has_entities(f):
                rec.status, rec.reason, rec.check = "blank", "DXF has no entities", "Empty DXF"
        elif ext == "dwg" and f is not None:
            head = f.read(6)
            if not (head.startswith(b"AC") and head[2:6].isdigit()):
                rec.status, rec.reason, rec.check = "blank", "Bad DWG header", "Bad header"
    elif cat == "Exchange":
        rec.check = "Byte hash"
        if ext in ("stp", "step"):
            has = (step_has_geometry(io.BytesIO(data)) if data is not None else step_has_geometry(f))
            if not has:
                rec.status, rec.reason, rec.check = "blank", "STEP file holds no geometry entity", "No geometry"
        elif ext == "stl" and f is not None:
            if stl_triangles(f, size) == 0:
                rec.status, rec.reason, rec.check = "blank", "STL has 0 triangles", "0 triangles"
        elif ext in ("igs", "iges") and f is not None:
            if not iges_has_geometry(f):
                rec.status, rec.reason, rec.check = "blank", "IGES holds no geometry entity", "No geometry"
    else:
        low = rec.name_l
        if low in JUNK_NAMES or low.startswith("~$") or low.startswith("._") or ext in JUNK_EXTS:
            rec.status, rec.reason = "junk", "System, lock, temp, installer or shortcut file"
        rec.check = "Byte hash"


# =====================================================================================================  archives
def archive_kind(name):
    low = name.lower()
    for suf, k in ((".tar.gz", "tar"), (".tar.bz2", "tar"), (".tar.xz", "tar"), (".tgz", "tar"), (".tbz2", "tar"), (".tbz", "tar"), (".txz", "tar"),
                   (".tar", "tar"), (".zip", "zip"), (".rar", "ext"), (".7z", "ext"), (".gz", "gz")):
        if low.endswith(suf):
            return k
    return None


def _split(p):
    return [c for c in re.split(r"[\\/]", p) if c]


def expand_archive(ctx, name, opener, size, pdir, arch, inner, depth, out, mtime):
    """Members of one archive become Recs appended to `out`. A whole archive that cannot be opened becomes ONE Not Checked row."""
    def archive_nc(reason):
        r = Rec(pdir, (), (), name, size, mtime)
        r.cat, r.status, r.reason = "Other", "nc", reason
        out.append(r)

    if depth > MAX_DEPTH:
        return archive_nc(f"Archive nested deeper than {MAX_DEPTH} levels")
    kind = archive_kind(name)
    my_arch = (name,)
    ctx.archives += 1
    try:
        with opener() as f:
            if kind == "zip":
                with zipfile.ZipFile(f) as zf:
                    infos = [i for i in zf.infolist() if not i.is_dir()]
                    if any(i.flag_bits & 1 for i in infos):
                        return archive_nc("Archive is password-protected")
                    for i in infos:
                        parts = _split(i.filename)
                        if not parts:
                            continue
                        mt = datetime(*i.date_time, tzinfo=timezone.utc).timestamp() if i.date_time[0] >= 1980 else mtime
                        _member(ctx, parts, i.file_size, mt, lambda zf=zf, i=i: zf.open(i), pdir, my_arch, depth, out)
            elif kind == "tar":
                with tarfile.open(fileobj=f, mode="r:*") as tf:
                    for m in tf:
                        if not m.isfile():
                            continue
                        parts = _split(m.name)
                        _member(ctx, parts, m.size, m.mtime, lambda tf=tf, m=m: tf.extractfile(m), pdir, my_arch, depth, out)
            elif kind == "gz":
                data_name = name[:-3]
                with gzip.GzipFile(fileobj=f) as gz:
                    raw = gz.read(MAX_NESTED_BYTES + 1)
                if len(raw) > MAX_NESTED_BYTES:
                    return archive_nc("Compressed file larger than 1 GB: not opened")
                _member(ctx, [data_name], len(raw), mtime, lambda raw=raw: io.BytesIO(raw), pdir, my_arch, depth, out)
            else:
                return _expand_external(ctx, name, f, size, pdir, my_arch, depth, out, mtime, archive_nc)
    except RecursionError:
        return archive_nc("Archive could not be opened")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, gzip.BadGzipFile, NotImplementedError) as exc:
        return archive_nc(f"Archive is corrupt or unsupported ({type(exc).__name__})")
    except OSError as exc:
        return archive_nc(f"Archive could not be read ({type(exc).__name__})")
    except RuntimeError as exc:
        return archive_nc("Archive is password-protected" if "password" in str(exc).lower() or "encrypted" in str(exc).lower()
                          else f"Archive could not be opened ({exc})")


def _member(ctx, parts, size, mtime, opener, pdir, my_arch, depth, out):
    """One member: a plain file, or (recursively) another archive."""
    name, inner = parts[-1], tuple(parts[:-1])
    if archive_kind(name) and size <= MAX_NESTED_BYTES:
        data = fobj = None
        try:
            if archive_kind(name) in ("zip", "tar", "gz"):
                with opener() as mf:
                    data = mf.read()
            else:                                           # rar / 7z go to an external tool anyway: stream them to disk, not into memory
                fobj = tempfile.TemporaryFile(dir=ctx.tmp_root)
                with opener() as mf:
                    shutil.copyfileobj(mf, fobj, 8 * 1024 * 1024)
        except Exception as exc:
            if fobj is not None:
                fobj.close()
            r = Rec(pdir, my_arch, inner, name, size, mtime)
            r.cat, r.status, r.reason = "Other", "nc", f"Archive member could not be read ({type(exc).__name__})"
            out.append(r)
            return
        try:
            _nested(ctx, name, data, pdir, my_arch, inner, depth + 1, out, mtime, fobj=fobj, size=size)
        finally:
            if fobj is not None:
                fobj.close()
        return
    if archive_kind(name):                                  # an archive too big to hold in memory
        r = Rec(pdir, my_arch, inner, name, size, mtime)
        r.cat, r.status, r.reason = "Other", "nc", "Nested archive larger than 1 GB: not opened"
        out.append(r)
        return
    r = Rec(pdir, my_arch, inner, name, size, mtime)
    ctx.archive_members += 1
    analyse(ctx, r, opener, size)
    out.append(r)


def _nested(ctx, name, data, pdir, my_arch, inner, depth, out, mtime, fobj=None, size=None):
    """Archive inside an archive: its members' archive chain gets the inner archive's path inside its parent."""
    full = "/".join(list(inner) + [name])
    chain = my_arch
    kind = archive_kind(name)
    n = len(data) if data is not None else size
    ctx.archives += 1
    if depth > MAX_DEPTH:
        r = Rec(pdir, chain, inner, name, n, mtime)
        r.cat, r.status, r.reason = "Other", "nc", f"Archive nested deeper than {MAX_DEPTH} levels"
        out.append(r)
        return
    sub_chain = chain + (full,)

    def nc(reason):
        r = Rec(pdir, chain, inner, name, n, mtime)
        r.cat, r.status, r.reason = "Other", "nc", reason
        out.append(r)
    try:
        bio = io.BytesIO(data) if data is not None else fobj
        if kind == "zip":
            with zipfile.ZipFile(bio) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if any(i.flag_bits & 1 for i in infos):
                    return nc("Archive is password-protected")
                for i in infos:
                    parts = _split(i.filename)
                    if parts:
                        mt = datetime(*i.date_time, tzinfo=timezone.utc).timestamp() if i.date_time[0] >= 1980 else mtime
                        _member(ctx, parts, i.file_size, mt, lambda zf=zf, i=i: zf.open(i), pdir, sub_chain, depth, out)
        elif kind == "tar":
            with tarfile.open(fileobj=bio, mode="r:*") as tf:
                for m in tf:
                    if m.isfile():
                        _member(ctx, _split(m.name), m.size, m.mtime, lambda tf=tf, m=m: tf.extractfile(m), pdir, sub_chain, depth, out)
        elif kind == "gz":
            raw = gzip.GzipFile(fileobj=bio).read(MAX_NESTED_BYTES + 1)
            _member(ctx, [name[:-3]], len(raw), mtime, lambda raw=raw: io.BytesIO(raw), pdir, sub_chain, depth, out)
        else:
            return _expand_external(ctx, name, bio, n, pdir, sub_chain, depth, out, mtime, nc)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, gzip.BadGzipFile, NotImplementedError, OSError) as exc:
        nc(f"Archive is corrupt or unsupported ({type(exc).__name__})")
    except RuntimeError:
        nc("Archive is password-protected")


def _expand_external(ctx, name, f, size, pdir, my_arch, depth, out, mtime, archive_nc):
    """rar / 7z through 7-Zip, unrar or bsdtar (or py7zr): extracted to a temporary folder that is deleted at once; members then go the normal way."""
    t = ctx.tools
    tmp = tempfile.mkdtemp(prefix="scan_", dir=ctx.tmp_root)
    try:
        src = os.path.join(tmp, "src.bin" + os.path.splitext(name)[1])
        f.seek(0)
        with open(src, "wb") as g:
            shutil.copyfileobj(f, g, 8 * 1024 * 1024)
        dst = os.path.join(tmp, "out")
        os.makedirs(dst)
        ok, why = False, ""
        low = name.lower()
        runs = []
        if "7z" in t:
            runs.append([t["7z"], "x", "-y", "-pNOPASSWORD", f"-o{dst}", src])
        if low.endswith(".rar") and "unrar" in t:
            runs.append([t["unrar"], "x", "-y", "-p-", src, dst + os.sep])
        if "bsdtar" in t:
            runs.append([t["bsdtar"], "-xf", src, "-C", dst])
        for cmd in runs:
            try:
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=3600)
                if r.returncode == 0:
                    ok = True
                    break
                txt = (r.stdout + r.stderr).decode("latin-1", "ignore").lower()
                why = "password" if ("password" in txt or "encrypted" in txt or "wrong" in txt) else "error"
            except Exception:
                why = "error"
        if not ok and low.endswith(".7z") and t.get("py7zr"):
            try:
                import py7zr
                with py7zr.SevenZipFile(src, "r") as z:
                    z.extractall(dst)
                ok = True
            except Exception as exc:
                why = "password" if "password" in str(exc).lower() else "error"
        if not ok:
            if not runs and not t.get("py7zr"):
                return archive_nc("Cannot open .rar/.7z: install 7-Zip (or unrar) on this machine")
            return archive_nc("Archive is password-protected" if why == "password" else "Archive is corrupt or unsupported")
        for base, _d, files in os.walk(dst):
            for fn in files:
                p = os.path.join(base, fn)
                rel = _split(os.path.relpath(p, dst))
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                _member(ctx, rel, st.st_size, st.st_mtime, lambda p=p: open(p, "rb"), pdir, my_arch, depth, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================================================  walking
def list_files(ctx, root, folders=None):
    """All files below root (or below each of `folders`, which sit under root): (abs path, dir parts, name, size, mtime, cloud).
    Folder links are not followed."""
    stack = [(f, tuple(_split(os.path.relpath(f, root)))) for f in folders] if folders else [(root, ())]
    entries = []
    while stack:
        base, parts = stack.pop()
        ctx.checkpoint()
        try:
            it = os.scandir(lp(base))
        except OSError as exc:
            ctx.nc_dirs.append((parts, f"Folder could not be listed ({type(exc).__name__})"))
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append((e.path, parts + (e.name,)))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        entries.append((e.path, parts, e.name, st.st_size, st.st_mtime, is_cloud(st)))
                except OSError as exc:
                    ctx.nc_dirs.append((parts + (e.name,), f"Entry could not be read ({type(exc).__name__})"))
        if len(entries) % 5000 < 50:
            ctx.progress("scan", len(entries), 0, 0, 0)
    return entries


def process_entry(ctx, ent):
    path, parts, name, size, mtime, cloud = ent
    ctx.checkpoint()
    out = []
    kind = archive_kind(name)
    if cloud and not ctx.download_cloud:
        r = Rec(parts, (), (), name, size, mtime)
        r.status, r.reason = "nc", "Cloud-only file (not downloaded): run with --download-cloud-files"
        ctx.cloud_skipped += 1
        out.append(r)
    elif kind:
        expand_archive(ctx, name, lambda: open(lp(path), "rb"), size, parts, (), (), 1, out, mtime)
    else:
        r = Rec(parts, (), (), name, size, mtime)
        analyse(ctx, r, lambda: open(lp(path), "rb"), size)
        out.append(r)
    with ctx.lock:
        ctx.files_seen += 1
        ctx.bytes_seen += size
        n, b = ctx.files_seen, ctx.bytes_seen
    if n % 200 == 0:
        ctx.progress("process", n, ctx.total_files, b, ctx.total_bytes)
    return out


# =====================================================================================================  tree, projects
class Node:
    __slots__ = ("name", "path", "kids", "files", "cad", "asm")

    def __init__(self, name, path):
        self.name, self.path, self.kids, self.files, self.cad, self.asm = name, path, {}, [], 0, []


def build_tree(recs, root_name):
    root = Node(root_name, ())
    for r in recs:
        n = root
        for i, c in enumerate(r.dir):
            k = n.kids.get(c)
            if k is None:
                k = n.kids[c] = Node(c, n.path + (c,))
            n = k
        n.files.append(r)
    stack, order = [root], []
    while stack:
        n = stack.pop()
        order.append(n)
        stack += list(n.kids.values())
    for n in reversed(order):
        n.cad = sum(1 for f in n.files if f.cat in CAD_CATS) + sum(k.cad for k in n.kids.values())
        n.asm = [f for f in n.files if f.cat == "Assembly" and f.refs] + [a for k in n.kids.values() for a in k.asm]
    return root


def subtree_files(node):
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out += n.files
        stack += list(n.kids.values())
    return out


class Project:
    def __init__(self, name, ptype, nodes, loose_nodes, rule, folder):
        self.name, self.type, self.rule, self.folder = name, ptype, rule, folder
        self.nodes = nodes                  # nodes whose whole subtree belongs here
        self.loose_nodes = loose_nodes      # nodes of which only the direct files belong here
        self.pid = ""
        self.files = []
        self.main = None
        self.notes = []
        self.candidates = []


class NameIndex:
    """File name (lowercase) -> part / assembly records, with a nearest-folder lookup that stays fast for names occurring tens of thousands of
    times: among folders sorted as tuples, the one sharing the longest prefix with the query is next to it."""

    def __init__(self, recs):
        self.by = collections.defaultdict(list)
        for r in recs:
            if r.cat in ("Part", "Assembly"):
                self.by[r.name_l].append(r)
        self.sorted = {}

    def nearest(self, name_l, from_dir):
        c = self.by.get(name_l)
        if not c:
            return None
        if len(c) == 1:
            return c[0]
        s = self.sorted.get(name_l)
        if s is None:
            pairs = sorted(((r.dir, i) for i, r in enumerate(c)), key=lambda x: x[0])
            s = self.sorted[name_l] = (pairs, [d for d, _ in pairs])
        pairs, keys = s
        i = bisect.bisect_left(keys, from_dir)
        best, best_k = None, -1
        for j in (i - 1, i):
            if 0 <= j < len(pairs):
                k = 0
                for a, b in zip(pairs[j][0], from_dir):
                    if a != b:
                        break
                    k += 1
                if k > best_k:
                    best, best_k = c[pairs[j][1]], k
        return best


def resolve_assemblies(recs, index):
    for r in recs:
        if r.cat == "Assembly" and r.refs and r.res is None:
            r.res = [index.nearest(n, r.dir) for n in r.refs]


def child_of(node, tdir):
    d = len(node.path)
    if len(tdir) > d and tdir[:d] == node.path:
        return tdir[d]
    return None


def split_projects(root, root_label):
    projects = []
    assigned = set()

    def role_child(x):
        return any(k.lower() in ROLE_NAMES or ROLE_RE.match(k) for k in x.kids)

    def top_level(x):
        names = {n for a in x.asm for n in a.refs}
        return [a for a in x.asm if a.name_l not in names]

    def label(x):
        return x.name if x.path else root_label

    def folder(x):
        return "/".join(x.path) if x.path else root_label

    def rec_split(x):
        nl = x.name.lower() if x.path else ""
        if nl and any(w in nl for w in LIB_WORDS):
            projects.append(Project(label(x), "Library", [x], [], "Folder name marks a library / standard-parts folder", folder(x)))
            return
        if role_child(x):
            projects.append(Project(label(x), "Project", [x], [], "Child folders named by role -> this folder is one project", folder(x)))
            return
        kids = [k for k in x.kids.values()]
        if not kids:
            projects.append(Project(label(x), "Project", [x], [], "Folder without subfolders", folder(x)))
            return
        d = len(x.path)
        names = {k.name: i for i, k in enumerate(kids)}
        # references from the assemblies of child A to files that sit in child B
        refs = collections.defaultdict(collections.Counter)
        resolved = collections.Counter()
        for i, k in enumerate(kids):
            for a in k.asm:
                for t in (a.res or ()):
                    if t is None:
                        continue
                    resolved[i] += 1
                    c = child_of(x, t.dir)
                    if c is not None and names[c] != i:
                        refs[i][names[c]] += 1
        referenced_by = collections.defaultdict(set)
        for a_i, cnt in refs.items():
            for b_i in cnt:
                referenced_by[b_i].add(a_i)
        hubs = {b for b, s in referenced_by.items() if len(s) >= 3}
        parent = list(range(len(kids)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for a_i, cnt in refs.items():
            for b_i, n in cnt.items():
                if b_i in hubs:
                    continue
                if n >= 10 and n / max(1, resolved[a_i]) >= 0.2:
                    parent[find(a_i)] = find(b_i)
        groups = collections.defaultdict(list)
        for i in range(len(kids)):
            groups[find(i)].append(i)
        cands, small = [], []
        for g in groups.values():
            members = [kids[i] for i in g]
            ncad = sum(m.cad for m in members)
            has_asm = any(m.asm for m in members)
            lib = len(members) == 1 and ncad > 0 and any(w in members[0].name.lower() for w in LIB_WORDS)
            (cands if (has_asm or ncad >= 20 or lib) else small).append(members)
        if len(cands) < 2:
            projects.append(Project(label(x), "Project", [x], [], "Fewer than two candidate groups -> this folder is one project", folder(x)))
            return
        for members in cands:
            if len(members) == 1:
                rec_split(members[0])
            else:
                projects.append(Project(" + ".join(m.name for m in members), "Project", members, [],
                                        "Sibling folders joined: assemblies reference files across them (10+ references, 20%+)", folder(x)))
        loose_nodes = [m for g in small for m in g]
        if x.files or loose_nodes:
            projects.append(Project(f"{label(x)} (loose files)", "Loose files", loose_nodes, [x],
                                    "Collection folder: own loose files and small groups", folder(x)))

    if root.cad == 0:
        projects.append(Project(root_label, "Loose files", [root], [], "No CAD files anywhere below the selected folder", root_label))
    else:
        rec_split(root)
    return projects


def mark_hub_libraries(projects, recs):
    """2 (second condition): a folder without a top-level assembly that assemblies in 3+ OTHER projects reference is a Library.
    Applied to the finished projects (see LIMITS)."""
    src = collections.defaultdict(set)
    for a in recs:
        if a.cat == "Assembly" and a.res:
            for t in a.res:
                if t is not None and t.pid != a.pid:
                    src[t.pid].add(a.pid)
    for p in projects:
        if p.type != "Project" or len(src[p.pid]) < 3:
            continue
        asm = [f for f in p.files if f.cat == "Assembly" and f.refs]
        used = {n for f in asm for n in f.refs}
        if not any(f.name_l not in used for f in asm):
            p.type, p.rule = "Library", "No top-level assembly and assemblies in 3+ other projects reference it"


def assign_files(projects, recs, root_label):
    for i, p in enumerate(projects, 1):
        p.pid = f"P{i:03d}"
    for p in projects:
        for n in p.nodes:
            for f in subtree_files(n):
                f.pid = p.pid
        for n in p.loose_nodes:
            for f in n.files:
                f.pid = p.pid
    byp = {p.pid: p for p in projects}
    for r in recs:
        if r.pid in byp:
            byp[r.pid].files.append(r)
    return byp


# =====================================================================================================  classification inside projects
def stem(name):
    return name.rsplit(".", 1)[0] if "." in name else name


def base_group(name):
    s = stem(name).lower()
    while True:
        n = re.sub(r"_\d+$", "", s)
        if n == s:
            return s
        s = n


def apply_debris(p):
    parts = [f for f in p.files if f.cat == "Part" and f.status == "real"]
    cnt = collections.Counter(base_group(f.name) for f in parts)
    for f in parts:
        low = stem(f.name).lower()
        if "_oa" in low or "-oa" in low or DEBRIS_RE.match(low) or cnt[base_group(f.name)] >= 50:
            f.status, f.reason = "debris", "STEP-import split (name pattern _oa / -oa / Fillet n / PRODUCT_ID / Imported, or 50+ parts share one base name)"
            f.check = "Import debris"


def hollow_assemblies(recs):
    """2.2 (b): an assembly none of whose components is a real part or a non-empty assembly is blank. Fixed-point over the whole scan."""
    asm = [r for r in recs if r.cat == "Assembly" and r.status == "real" and r.res is not None]
    nonempty = set()

    def good(t):
        if t is None:
            return False
        if t.cat == "Part":
            return t.status in ("real", "nc")
        if t.cat == "Assembly":
            return t.status == "nc" or id(t) in nonempty
        return False
    changed = True
    while changed:
        changed = False
        for a in asm:
            if id(a) not in nonempty and any(good(t) for t in a.res):
                nonempty.add(id(a))
                changed = True
    for a in asm:
        if id(a) not in nonempty:
            a.status, a.reason, a.check = "blank", "Hollow: none of its components is a real part or a non-empty assembly", "Hollow"


def keep_newest(group):
    return max(group, key=lambda r: (r.mtime or 0, -len(r.dir), r.name))


def mark_dups(group, kept, why):
    for r in group:
        if r is not kept:
            r.status, r.reason, r.same_as = "dup", why, kept


def fuzzy_clusters(ctx, p):
    """1.4: Solid Edge bodies of the same length that differ in under 2% of bytes are one part (re-saves rewrite internal ids)."""
    by_len = collections.defaultdict(list)
    for f in p.files:
        if f.cat == "Part" and f.status == "real" and f.body_md5:
            by_len[f.body_len].append(f)
    merged = {}
    for ln, fs in by_len.items():
        distinct = {}
        for f in fs:
            distinct.setdefault(f.body_md5, f)
        if len(distinct) < 2:
            continue
        reps = []
        for md5, f in distinct.items():
            body = load_body(ctx, f)
            if body is None:
                if len(reps) < FUZZY_MAX_CLUSTERS:
                    reps.append((md5, None))
                continue
            hit = None
            for rm, rb in reps[:FUZZY_MAX_CLUSTERS]:
                if rb is None or len(rb) != len(body):
                    continue
                if diff_below(rb, body, FUZZY_PCT):
                    hit = rm
                    break
            if hit:
                merged[md5] = hit
            elif len(reps) < FUZZY_MAX_CLUSTERS:        # only the first FUZZY_MAX_CLUSTERS are ever compared against: do not hold the rest in memory
                reps.append((md5, body))
    return merged


def diff_pct(a, b):
    """Percentage of byte positions that differ (equal-length inputs), at C speed: xor as big integers and count the zero bytes."""
    if a == b:
        return 0.0
    n = len(a)
    x = (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(n, "little")
    return (n - x.count(0)) * 100.0 / max(1, n)


_CHUNK = 64 * 1024


def diff_below(a, b, pct):
    """Exactly `diff_pct(a, b) < pct`, but stops as soon as the differing bytes already reach the limit (so unrelated bodies cost one chunk,
    not the whole body). Equal-length inputs."""
    n = len(a)
    if a == b:
        return True
    limit = pct * n / 100.0                    # differing bytes must stay strictly below this
    diff = 0
    for i in range(0, n, _CHUNK):
        x, y = a[i:i + _CHUNK], b[i:i + _CHUNK]
        if x != y:
            m = len(x)
            z = (int.from_bytes(x, "little") ^ int.from_bytes(y, "little")).to_bytes(m, "little")
            diff += m - z.count(0)
            if diff >= limit:
                return False
    return diff < limit


_NOT_A_ZIP = object()


class _ZipCache:
    """Open zip files kept for the duplicate check, so every re-read of a member does not re-read (and re-index) the whole archive."""
    MAX = 6

    def __init__(self):
        self.d = collections.OrderedDict()      # key -> (ZipFile, {normalised member name: ZipInfo})

    def get(self, key, opener):
        hit = self.d.get(key)
        if hit is not None:
            self.d.move_to_end(key)
            if hit is _NOT_A_ZIP:                      # this archive is not a readable zip (rar / 7z / corrupt): do not re-extract it per member
                raise zipfile.BadZipFile("not a zip archive")
            return hit
        try:
            zf = opener()
            names = {}
            for i in zf.infolist():
                names.setdefault("/".join(_split(i.filename)), i)
        except Exception:
            self.d[key] = _NOT_A_ZIP                   # a marker, not the exception: its traceback would keep the archive's bytes alive
            raise
        self.d[key] = (zf, names)
        while sum(v is not _NOT_A_ZIP for v in self.d.values()) > self.MAX:
            for k, v in self.d.items():                 # drop the least recently used open archive (not-a-zip markers cost nothing and stay)
                if v is not _NOT_A_ZIP:
                    del self.d[k]
                    _close_zip(v[0])
                    break
        return self.d[key]

    def close(self):
        for v in self.d.values():
            if v is not _NOT_A_ZIP:
                _close_zip(v[0])
        self.d.clear()


def _member_bytes(cache, key, opener, member):
    zf, names = cache.get(key, opener)
    info = names.get(member)
    if info is None:
        raise KeyError(member)
    return zf.read(info)


def _nested_zip_opener(cache, parent_key, parent_opener, member, tmp_dir=None):
    """Opens a zip stored inside another zip. It is copied to a temporary file (not held in memory) and only when it really is a zip,
    which is told from its first bytes, so a rar / 7z member is never read just to find that out."""
    def opener():
        zf, names = cache.get(parent_key, parent_opener)
        info = names.get(member)
        if info is None:
            raise KeyError(member)
        with zf.open(info) as src:
            if src.read(2) != b"PK" and member.lower().endswith((".rar", ".7z")):   # only these are skipped unread; anything else gets the full zip check
                raise zipfile.BadZipFile("not a zip archive")
        tmp = tempfile.TemporaryFile(dir=tmp_dir)
        try:
            with zf.open(info) as src:
                shutil.copyfileobj(src, tmp, 8 * 1024 * 1024)
            z = zipfile.ZipFile(tmp)
        except Exception:
            tmp.close()
            raise
        z._datalabs_tmp = tmp
        return z
    return opener


def _close_zip(zf):
    for obj in (zf, getattr(zf, "_datalabs_tmp", None)):
        try:
            if obj is not None:
                obj.close()
        except Exception:
            pass


def load_body(ctx, f):
    """Re-open a Solid Edge part (on disk or inside zip files) and return its body bytes; None if it cannot be re-opened."""
    try:
        base = os.path.join(ctx.root, *f.pdir)
        if not f.arch:
            with open(lp(os.path.join(base, f.name)), "rb") as fh:
                return se_body_bytes(fh.read())
        cache = ctx.zip_cache
        path = lp(os.path.join(base, f.arch[0]))
        key, opener = (path,), (lambda: zipfile.ZipFile(path))
        for name in f.arch[1:]:
            # the nested archive's bytes are only extracted when it is not already open in the cache
            opener = _nested_zip_opener(cache, key, opener, name, ctx.tmp_root)
            key = key + (name,)
        return se_body_bytes(_member_bytes(cache, key, opener, "/".join(list(f.inner) + [f.name])))
    except Exception:
        return None


def classify_project(ctx, p):
    files = p.files
    # 5.2 (c): .cfg / .log / .err / .bak next to a same-named CAD file are CAD side-files
    cad_names = collections.defaultdict(set)
    for f in files:
        if f.cat in CAD_CATS:
            cad_names[(f.arch, f.dir)].add(f.name_l)
            cad_names[(f.arch, f.dir)].add(stem(f.name_l))
    for f in files:
        if f.cat == "Other" and f.status == "real" and f.ext in SIDE_EXTS:
            names = cad_names.get((f.arch, f.dir), ())
            if stem(f.name_l) in names or f.name_l.rsplit(".", 1)[0] in names:
                f.status, f.reason = "junk", "CAD side-file (.cfg / .log / .err / .bak with the same name as a CAD file in the same folder)"
    apply_debris(p)


def dedupe_project(ctx, p):
    files = p.files
    # parts
    merged = fuzzy_clusters(ctx, p)
    groups = collections.defaultdict(list)
    for f in files:
        if f.cat == "Part" and f.status == "real":
            key = ("b", merged.get(f.body_md5, f.body_md5)) if f.body_md5 else ("f", f.md5)
            groups[key].append(f)
    for key, g in groups.items():
        if len(g) > 1:
            k = keep_newest(g)
            mark_dups(g, k, "Same 3D body as the kept file" if key[0] == "b" else "Byte-identical to the kept file")
    # Creo older versions: name.prt.N that is not the highest N in its folder
    ver = collections.defaultdict(list)
    for f in files:
        if f.cat == "Part" and f.status == "real":
            m = VERSION_RE.search(f.name_l)
            if m:
                ver[(f.arch, f.dir, f.name_l[:m.start()], m.group(1))].append((int(m.group(2)), f))
    for g in ver.values():
        if len(g) > 1:
            g.sort(key=lambda x: x[0])
            for _n, f in g[:-1]:
                f.status, f.reason, f.same_as = "dup", "Older Creo version (not the highest version number)", g[-1][1]
    # assemblies: byte-identical OR identical component list
    asm = [f for f in files if f.cat == "Assembly" and f.status == "real"]
    parent = {id(f): id(f) for f in asm}
    byid = {id(f): f for f in asm}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for keyfn in (lambda f: f.md5, lambda f: f.comp):
        seen = {}
        for f in asm:
            k = keyfn(f)
            if k is None:
                continue
            if k in seen:
                parent[find(id(f))] = find(seen[k])
            else:
                seen[k] = id(f)
    comps = collections.defaultdict(list)
    for f in asm:
        comps[find(id(f))].append(f)
    for g in comps.values():
        if len(g) > 1:
            k = keep_newest(g)
            mark_dups(g, k, "Byte-identical or same component list as the kept file")
    for cat in ("Drawing", "Exchange", "Other"):
        by = collections.defaultdict(list)
        for f in files:
            if f.cat == cat and f.status == "real" and f.md5:
                by[f.md5].append(f)
        for g in by.values():
            if len(g) > 1:
                mark_dups(g, keep_newest(g), "Byte-identical to the kept file")


# =====================================================================================================  main assembly, linkage
def real_parts_in_tree(top, seen=None):
    """Distinct part files (by name) in the full component tree of an assembly; also missing sub-assemblies."""
    parts, missing_sub, seen = set(), 0, seen if seen is not None else set()
    stack = [top]
    while stack:
        a = stack.pop()
        if id(a) in seen:
            continue
        seen.add(id(a))
        if not a.refs:
            continue
        for n, t in zip(a.refs, a.res or [None] * len(a.refs)):
            ext = n.rsplit(".", 1)[-1]
            if ext in ASM_EXTS:
                if t is None:
                    missing_sub += 1
                elif t.status != "blank":
                    stack.append(t)
            elif ext in PART_EXTS:
                parts.add(n)
    return parts, missing_sub


def find_main(p, index):
    asm = [f for f in p.files if f.cat == "Assembly" and f.status == "real" and f.refs]
    used = {n for f in p.files if f.cat == "Assembly" and f.refs for n in f.refs}
    cands = []
    for a in asm:
        if a.name_l in used:
            continue
        low = a.name_l
        if any(w in low for w in (".stp", ".step", ".igs", "iges", "imported")):
            continue
        comp = [t for t in (a.res or []) if t is not None and t.cat == "Part"]
        if comp and sum(1 for t in comp if t.status == "debris") * 2 > len(comp):
            continue
        parts, _m = real_parts_in_tree(a)
        cands.append((len(parts), a))
    cands.sort(key=lambda x: (-x[0], len(x[1].dir), x[1].name_l))
    p.candidates = cands
    if not cands:
        return None
    top = cands[0][0]
    pref = [c for c in cands if c[0] >= 0.9 * top and (any(w in c[1].name_l for w in FINAL_WORDS) or re.search(r"\bga\b", c[1].name_l))]
    return (pref or cands)[0][1]


def linkage(p, index, main):
    if main is None:
        return None
    parts_set, missing_sub = real_parts_in_tree(main)
    refs = {n for n in parts_set}
    in_proj = other = empty = missing = 0
    for n in refs:
        c = index.by.get(n, [])
        bodied = [t for t in c if t.status != "blank"]
        if bodied:
            if any(t.pid == p.pid for t in bodied):
                in_proj += 1
            else:
                other += 1
        elif c:
            empty += 1
        else:
            missing += 1
    missing += missing_sub
    return len(refs) + missing_sub, in_proj, other, empty, missing


def final_drawing(p, main):
    if main is None:
        return "N"
    group = {main.name_l}
    group |= {f.name_l for f in p.files if f.cat == "Assembly" and f.same_as is main}
    for f in p.files:
        if f.cat == "Drawing" and f.refs and group & set(f.refs):
            return "Y"
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    mstem, pname = norm(stem(main.name)), norm(p.name)
    for f in p.files:
        if f.status in ("nc",) or not (f.cat == "Drawing" or (f.cat == "Other" and f.ext == "pdf")):
            continue
        if f.ext not in ("dft", "dwg", "dxf", "slddrw", "pdf", "idw", "drw", "catdrawing"):
            continue
        s = stem(f.name_l)
        if norm(s) == mstem:
            return "Y"
        words = any(w in s for w in GA_WORDS) or re.search(r"(^|[^a-z])ga([^a-z]|$)", s)
        if words and (mstem in norm(s) or (pname and pname in norm(s))):
            return "Y"
    return "N"


# =====================================================================================================  workbook
PROJECT_HEADERS = ["Project ID", "Project Name", "Project Type", "Project Folder (relative path)", "Total Files",
                   "Total", "Real Distinct", "Duplicates", "Blank / Empty",
                   "Total", "Real Distinct", "Duplicates", "Blank (Placeholders / Empty)", "Import Debris (STEP splits)",
                   "Total", "Real Distinct", "Duplicates", "Blank / Empty",
                   "Total", "Real Distinct", "Duplicates", "Blank / Empty",
                   "Total", "Real Distinct", "Duplicates", "Junk / Auto-generated", "Not Checked",
                   "Total Real 3D Files (Parts + Assemblies)", "Total Real 2D Files (Drawings)", "Total Real Exchange Files", "Total Real Other Files",
                   "Total Real Files", "Main Assembly (detected)", "Parts Referenced", "Found – Real, in Project Folder", "Found – Real, in Other Folder",
                   "Found – Empty Placeholder Only", "Missing", "Part Linkage %", "Final Assembly Drawing Present (Y/N)", "Content Check Level", "Notes",
                   "Check (must be 0)"]
PROJECT_WIDTHS = [9, 30, 12, 44, 11] + [12] * 27 + [30, 12, 12, 12, 12, 12, 12, 14, 22, 30, 12]
L = xlsx_min.col_letter


def counts(files):
    c = collections.Counter()
    for f in files:
        c[(f.cat, f.status)] += 1
    return c


def content_level(p):
    parts = [f for f in p.files if f.cat == "Part" and f.status != "nc"]
    if not parts:
        return "No parts"
    other = collections.Counter(f.ext for f in parts if f.ext not in ("par", "psm"))
    if not other:
        return "Full (Solid Edge)"
    return "Partial (byte-identical duplicates only, no blank check: " + ", ".join(f".{e}" for e, _n in other.most_common(5)) + ")"


def project_row(p, n, c, link, final, level):
    """One row of the Projects sheet. Counts are values; the 'Real Distinct', totals and check columns are formulas with cached values."""
    g = lambda cat, st: c[(cat, st)]
    tot = lambda cat: sum(c[(cat, s)] for s in ("real", "dup", "blank", "debris", "junk"))
    A_t, P_t, D_t, X_t, O_t = (tot(x) for x in ("Assembly", "Part", "Drawing", "Exchange", "Other"))
    A_b, A_d = g("Assembly", "blank"), g("Assembly", "dup")
    P_b, P_d, P_i = g("Part", "blank"), g("Part", "dup"), g("Part", "debris")
    D_b, D_d = g("Drawing", "blank"), g("Drawing", "dup")
    X_b, X_d = g("Exchange", "blank"), g("Exchange", "dup")
    O_j, O_d = g("Other", "junk"), g("Other", "dup")
    nc = sum(v for (cat, st), v in c.items() if st == "nc")
    A_r, P_r, D_r, X_r, O_r = A_t - A_d - A_b, P_t - P_d - P_b - P_i, D_t - D_d - D_b, X_t - X_d - X_b, O_t - O_d - O_j
    i = lambda v: (v, "int")
    r = [p.pid, p.name, p.type, p.folder]
    total = A_t + P_t + D_t + X_t + O_t + nc
    r.append(F(f"F{n}+J{n}+O{n}+S{n}+W{n}+AA{n}", total, "int"))
    r += [i(A_t), F(f"F{n}-H{n}-I{n}", A_r, "int"), i(A_d), i(A_b)]
    r += [i(P_t), F(f"J{n}-L{n}-M{n}-N{n}", P_r, "int"), i(P_d), i(P_b), i(P_i)]
    r += [i(D_t), F(f"O{n}-Q{n}-R{n}", D_r, "int"), i(D_d), i(D_b)]
    r += [i(X_t), F(f"S{n}-U{n}-V{n}", X_r, "int"), i(X_d), i(X_b)]
    r += [i(O_t), F(f"W{n}-Y{n}-Z{n}", O_r, "int"), i(O_d), i(O_j), i(nc)]
    r += [F(f"G{n}+K{n}", A_r + P_r, "int"), F(f"P{n}", D_r, "int"), F(f"T{n}", X_r, "int"), F(f"X{n}", O_r, "int"),
          F(f"AB{n}+AC{n}+AD{n}+AE{n}", A_r + P_r + D_r + X_r + O_r, "int")]
    if link is None:
        r += ["", "", "", "", "", "", "", "N" if final is None else final]
    else:
        refd, inp, oth, emp, mis = link
        den = refd - emp
        r += [p.main.name if p.main else "", i(refd), i(inp), i(oth), i(emp), i(mis),
              F(f'IF((AH{n}-AK{n})=0,"",AI{n}/(AH{n}-AK{n}))', (inp / den) if den else "", "pct"), final]
    r += [level, "; ".join(p.notes)]
    r.append(F(f"E{n}-(F{n}+J{n}+O{n}+S{n}+W{n})-AA{n}", 0, "int"))
    return r, dict(total=total, A_t=A_t, A_r=A_r, A_d=A_d, A_b=A_b, P_t=P_t, P_r=P_r, P_d=P_d, P_b=P_b, P_i=P_i, D_t=D_t, D_r=D_r, D_d=D_d,
                   D_b=D_b, X_t=X_t, X_r=X_r, X_d=X_d, X_b=X_b, O_t=O_t, O_r=O_r, O_d=O_d, O_j=O_j, nc=nc, link=link)


def rel_folder(r):
    return "/".join(r.pdir) or "."


def inside_archive(r):
    if not r.arch:
        return ""
    chain = list(r.arch)
    if r.inner:
        chain.append("/".join(r.inner))
    return " > ".join(chain)


def sheet_chunks(name, header_row, rows_iter, widths, freeze=(1, 0), first_row_count=1):
    """Yields (sheet name, rows) pairs, starting a continuation sheet before Excel's row limit."""
    cap = MAX_ROWS - 1
    buf, part = [], 1
    for row in rows_iter:
        buf.append(row)
        if len(buf) >= cap - 1:
            yield (name if part == 1 else f"{name} ({part})"), [header_row] + buf
            buf, part = [], part + 1
    if buf or part == 1:
        yield (name if part == 1 else f"{name} ({part})"), [header_row] + buf


def write_workbook(path, ctx, projects, rows_by_project, meta):
    docs = json.load(open(os.path.join(HERE, "scan_docs.json"), encoding="utf-8"))
    wb = xlsx_min.Workbook()
    # ---- Read Me
    rm = [[("LH2 CAD Project Scan: output format", "title14")]]
    for row in docs["Read Me"][1:]:
        rm.append([(row[0], "bold") if row else "", (row[1], "wrap") if len(row) > 1 else ""])
    rm += [[""], [("This scan run", "bold")]]
    for k, v in meta.items():
        rm.append([(k, "bold"), (str(v), "wrap")])
    rm += [[""], [("Known limits of this version", "bold")]] + [["", (x, "wrap")] for x in LIMITS]
    wb.add_sheet("Read Me", rm, widths=[18, 120])
    # ---- Field Logic
    fl = []
    for i, row in enumerate(docs["Field Logic"], 1):
        row = list(row) + [""] * (6 - len(row))
        if i == 1:
            fl.append([(row[0], "title")])
        elif i == 2:
            fl.append([(row[0], "note")])
        elif i == 4:
            fl.append([(x, "header") for x in row])
        elif len([x for x in row if x]) == 1 and row[0] and i > 4:
            fl.append([(row[0], "section")] + [("", "section")] * 5)
        else:
            fl.append([(row[0], "bold")] + [(x, "wrap") for x in row[1:]])
    wb.add_sheet("Field Logic", fl, widths=[5, 22, 22, 70, 62, 16], freeze=(4, 0), merges=["A2:F2"])
    # ---- Projects
    head1 = [("Projects", "title")]
    merges = []
    spans = [("Project", 1, 5), ("Assembly files", 6, 9), ("Part files", 10, 14), ("Drawing files (2D)", 15, 18),
             ("3D exchange files (STEP / IGES / Parasolid / STL)", 19, 22), ("Other files", 23, 27), ("Real file totals", 28, 32),
             ("Part linkage (main assembly)", 33, 39), ("Drawing", 40, 40), ("Quality", 41, 43)]
    cells = {}
    for label, a, b in spans:
        for c_ in range(a, b + 1):
            cells[c_] = (label if c_ == a else "", "group")
        if b > a:
            merges.append(f"{L(a)}2:{L(b)}2")
    group_row = [cells[i] for i in range(1, 44)]

    def rows():
        yield head1
        yield group_row
        yield [(h, "header") for h in PROJECT_HEADERS]
        for r in rows_by_project:
            yield r["row"]
        yield r_all(rows_by_project)
    wb.add_sheet("Projects", rows(), widths=PROJECT_WIDTHS, freeze=(3, 2), merges=merges, row_heights={3: 42})
    # ---- Real Files / Excluded Files
    rf_head = [(h, "header") for h in ["Project ID", "Project Name", "Category", "File Name", "Folder Path (relative)", "Inside Archive", "Extension",
                                         "Size (bytes)", "Modified (UTC)", "Copies Excluded", "Content Check"]]
    ex_head = [(h, "header") for h in ["Project ID", "Category", "File Name", "Folder Path (relative)", "Inside Archive", "Exclusion", "Reason Detail",
                                         "Same As (kept file path)"]]
    kept_by = collections.Counter()
    for p in projects:
        for f in p.files:
            if f.status == "dup" and f.same_as is not None:
                kept_by[id(f.same_as)] += 1

    def rf_rows():
        for p in projects:
            for cat in ("Part", "Assembly", "Drawing", "Exchange", "Other"):
                for f in sorted((x for x in p.files if x.cat == cat and x.status == "real"), key=lambda x: (x.dir, x.name_l)):
                    yield [f.pid, p.name, cat, f.name, rel_folder(f), inside_archive(f), f.ext, (f.size, "int"), iso(f.mtime), (kept_by[id(f)], "int"),
                           f.check or "Byte hash"]

    def ex_rows():
        label = {"blank": "Blank", "dup": "Duplicate", "debris": "Import Debris", "junk": "Junk"}
        for p in projects:
            for f in sorted((x for x in p.files if x.status in label), key=lambda x: (x.cat, x.dir, x.name_l)):
                same = ""
                if f.same_as is not None:
                    k = f.same_as
                    same = "/".join(k.pdir + (k.name,)) if not k.arch else "/".join(k.pdir) + "/" + inside_archive(k) + " > " + k.name
                yield [f.pid, f.cat, f.name, rel_folder(f), inside_archive(f), label[f.status], f.reason, same]
    rf_w = [9, 28, 11, 40, 60, 28, 9, 12, 21, 11, 24]
    ex_w = [9, 11, 40, 60, 28, 14, 60, 60]
    for nm, rows_ in sheet_chunks("Real Files", rf_head, rf_rows(), rf_w):
        wb.add_sheet(nm, rows_, widths=rf_w, freeze=(1, 0))
    for nm, rows_ in sheet_chunks("Excluded Files", ex_head, ex_rows(), ex_w):
        wb.add_sheet(nm, rows_, widths=ex_w, freeze=(1, 0))
    # ---- Project Map
    pm_head = [(h, "header") for h in ["Project ID", "Project Name", "Project Type", "Folders Included", "Rule Applied",
                                         "Top-Level Assemblies Found (main assembly first)", "CAD Files", "Notes"]]
    pm = [pm_head]
    for p in projects:
        incl = [("/".join(n.path) or meta["Scanned folder"]) + " (and all subfolders)" for n in p.nodes] + \
               [("/".join(n.path) or meta["Scanned folder"]) + " (loose files only)" for n in p.loose_nodes]
        cands = p.candidates
        if cands:
            txt = f"{len(cands)} top-level assemblies. Main (most real components): {'/'.join(cands[0][1].dir[len(p.nodes[0].path) if p.nodes else 0:] + (cands[0][1].name,))}."
            if len(cands) > 1:
                txt += " Next: " + "; ".join(c[1].name for c in cands[1:4])
        else:
            txt = "No top-level assembly found"
        ncad = sum(1 for f in p.files if f.cat in CAD_CATS)
        note = "Confirm the main assembly before using Part Linkage %" if cands else ""
        pm.append([p.pid, p.name, p.type, "; ".join(incl), p.rule, txt, (ncad, "int"), note])
    wb.add_sheet("Project Map", pm, widths=[9, 30, 12, 50, 50, 70, 10, 40], freeze=(1, 0))
    # ---- Not Checked
    nc_head = [(h, "header") for h in ["Project ID", "Path (relative)", "Inside Archive", "Extension", "Reason"]]

    def nc_rows():
        for p in projects:
            for f in p.files:
                if f.status == "nc":
                    yield ([f.pid, "/".join(f.pdir + (f.name,)), "", f.ext, f.reason] if not f.arch else
                           [f.pid, "/".join(f.pdir + (f.arch[0],)), inside_archive(f) + " > " + f.name, f.ext, f.reason])
        for d, reason in ctx.nc_dirs:
            yield [ctx.dir_pid(d), "/".join(d), "", "(folder)", reason]
    wb.add_sheet("Not Checked", [nc_head] + list(nc_rows()), widths=[9, 80, 30, 10, 70], freeze=(1, 0))
    wb.save(path)


def r_all(rows):
    keys = ["total", "A_t", "A_r", "A_d", "A_b", "P_t", "P_r", "P_d", "P_b", "P_i", "D_t", "D_r", "D_d", "D_b", "X_t", "X_r", "X_d", "X_b", "O_t", "O_r", "O_d",
            "O_j", "nc"]
    s = {k: sum(r["m"][k] for r in rows) for k in keys}
    n0, n1 = 4, 3 + len(rows)
    cols = ["E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "AA"]
    cached = [s["total"], s["A_t"], s["A_r"], s["A_d"], s["A_b"], s["P_t"], s["P_r"], s["P_d"], s["P_b"], s["P_i"], s["D_t"], s["D_r"], s["D_d"], s["D_b"],
              s["X_t"], s["X_r"], s["X_d"], s["X_b"], s["O_t"], s["O_r"], s["O_d"], s["O_j"], s["nc"]]
    out = [("", "total_text"), ("All projects", "total_text"), ("", "total_text"), ("", "total_text")]
    for c_, v in zip(cols, cached):
        out.append(F(f"SUM({c_}{n0}:{c_}{n1})", v, "total_int"))
    real3d = s["A_r"] + s["P_r"]
    for c_, v in (("AB", real3d), ("AC", s["D_r"]), ("AD", s["X_r"]), ("AE", s["O_r"]), ("AF", real3d + s["D_r"] + s["X_r"] + s["O_r"])):
        out.append(F(f"SUM({c_}{n0}:{c_}{n1})", v, "total_int"))
    out.append(("", "total_text"))
    links = [r["m"]["link"] for r in rows if r["m"]["link"]]
    for k, c_ in zip(range(5), ("AH", "AI", "AJ", "AK", "AL")):
        out.append(F(f"SUM({c_}{n0}:{c_}{n1})", sum(x[k] for x in links), "total_int"))
    out += [("", "total_text")] * 4
    out.append(F(f"SUM(AQ{n0}:AQ{n1})", 0, "total_int"))
    # keep exactly 43 columns
    return out[:43]


# =====================================================================================================  entry point
def scan(root, out_dir, log=print, label=None, workers=None, progress=None, download_cloud=False, host=None, checkpoint=None, folders=None):
    """Runs the scan; returns (xlsx path, project count, file count, problems). `folders`: scan only these sub-folders of root."""
    t0 = time.time()
    root = os.path.abspath(root)
    if folders:
        folders = [os.path.abspath(f) for f in folders]
        root = os.path.commonpath(folders) if len(folders) > 1 else os.path.dirname(folders[0])
    root_label = label or os.path.basename(root.rstrip("/\\")) or root
    if host is None:
        import file_profiler as host_mod
        host = host_mod
    nworkers = workers or host.default_workers()
    ctx = Ctx(root, nworkers, download_cloud, host)
    ctx.progress = progress or (lambda *a: None)
    ctx.checkpoint = checkpoint or (lambda: host.CONTROL.check())
    ctx.tmp_root = None
    log(f"Scan (step 2): {root_label}")
    log("Listing files ...")
    entries = list_files(ctx, root, folders)
    ctx.total_files = len(entries)
    ctx.total_bytes = sum(e[3] for e in entries)
    log(f"{len(entries):,} files, {ctx.total_bytes / 1e9:.1f} GB. Reading every file (hash + content check); archives are opened ...")
    if ctx.tools:
        log("Archive tools: " + ", ".join(sorted(ctx.tools)))
    else:
        log("No 7-Zip / unrar / bsdtar found: .rar and .7z archives will be listed in Not Checked.")
    ctx.progress("process", 0, ctx.total_files, 0, ctx.total_bytes)
    recs = []
    with ThreadPoolExecutor(max_workers=nworkers) as ex:
        for part in ex.map(lambda e: process_entry(ctx, e), entries, chunksize=16):
            recs.extend(part)
    ctx.progress("process", ctx.total_files, ctx.total_files, ctx.total_bytes, ctx.total_bytes)
    log(f"Read {len(recs):,} files (archive members included). Splitting into projects ...")
    ctx.progress("links", 0, 0, 0, 0)
    index = NameIndex(recs)
    resolve_assemblies(recs, index)
    tree = build_tree(recs, root_label)
    projects = split_projects(tree, root_label)
    byp = assign_files(projects, recs, root_label)
    mark_hub_libraries(projects, recs)
    ctx.dir_pid = lambda d: next((r.pid for r in recs if r.dir[:len(d)] == d and r.pid), "")
    for p in projects:
        classify_project(ctx, p)
    log("Checking assemblies, duplicates and part linkage ...")
    hollow_assemblies(recs)
    for p in projects:
        dedupe_project(ctx, p)
    ctx.zip_cache.close()
    for p in projects:
        p.main = find_main(p, index)
    rows = []
    for p in projects:
        n = 4 + len(rows)
        c = counts(p.files)
        link = linkage(p, index, p.main)
        final = final_drawing(p, p.main)
        if any(f.cat == "Part" and f.status == "nc" for f in p.files):
            p.notes.append("Some part files are Not Checked (see Not Checked sheet)")
        if p.main is not None and p.main.kind == "scan":
            p.notes.append("Main assembly components found by text scan (weaker evidence)")
        row, m = project_row(p, n, c, link, final, content_level(p))
        rows.append({"row": row, "m": m})
    today = datetime.now().strftime("%Y-%m-%d")
    meta = {"Scanned folder": root_label, "Scan date": today, "Script": f"Folder Profiler CAD scan {SCAN_VERSION}",
            "Files read": f"{len(recs):,} (archive members included)", "Archives opened": f"{ctx.archives:,}",
            "Cloud-only files": ("downloaded and checked" if download_cloud else f"{ctx.cloud_skipped:,} not downloaded (listed in Not Checked)"),
            "Seconds": f"{time.time() - t0:,.0f}"}
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", root_label)
    path = os.path.join(out_dir, f"scan_{safe}_{today}.xlsx")
    log("Writing the workbook ...")
    write_workbook(path, ctx, projects, rows, meta)
    problems = sum(1 for r in recs if r.status == "nc") + len(ctx.nc_dirs)
    log(f"Done: {len(projects):,} projects, {len(recs):,} files, {problems:,} not checked.")
    return path, len(projects), len(recs), problems
