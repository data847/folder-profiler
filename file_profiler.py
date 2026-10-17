"""Folder Profiler — collects file METRICS only. It never copies, uploads or stores file contents.

What it writes (one zip of CSVs, nothing else):
  Files.csv       one row per file: project, file_no, ext, size_bytes, modified, depth, sha256, header_signature
  Projects.csv    one row per project folder: counts, bytes, date range
  Extensions.csv  one row per project + extension: counts, bytes
  Run_info.csv    tool version, date, OS, folder name, totals, read errors

What it does NOT write: file names, folder paths below the project name, or any file text.

How the file contents are touched (and nothing else):
  * sha256   - one-way fingerprint of the whole file (cannot be turned back into the file)
  * header_signature - computed from the first few KB of certain file types and reduced to a tag
    from the fixed lists in this file (e.g. "STEP:AP214:SolidWorks", "DXF:AC1027:3D", "PDF").
    The raw header text is never kept.

Standard library only. No network code. Files are opened read-only.

Usage:  python file_profiler.py [folder] [--out DIR] [--name LABEL]
        (no folder given -> you are asked for one)
Docker: see Dockerfile / run-docker.* - read-only input mount, no network, non-root user.
"""
import csv
import hashlib
import os
import platform
import re
import sys
import time
import traceback
import zipfile
from datetime import datetime, timezone

VERSION = "1.1"
HEAD_BYTES = 4096            # header peek for every file
DXF_SCAN_BYTES = 1_048_576   # DXF only: scan up to 1 MB for 3D entity names (flag only, text discarded)
HASH_CHUNK = 1024 * 1024

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
    except OSError:
        return "UNREADABLE"


def sha256_of(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return "UNREADABLE"
    return h.hexdigest()


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


def profile(root, out_dir, log=print, label=None):
    root = os.path.abspath(root)
    label = label or os.path.basename(root) or "folder"
    t0 = time.time()
    projects = sorted((d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))), key=str.lower)
    units = [(d, os.path.join(root, d), True) for d in projects]
    if any(os.path.isfile(os.path.join(root, f)) for f in os.listdir(root)):
        units.append(("(root)", root, False))        # loose files, top level only
    if not units:
        raise SystemExit("That folder is empty - nothing to profile.")

    files_rows, errors, seen, junk = [], 0, 0, 0
    last_print = 0.0
    for proj, base, recurse in units:
        n = 0
        wbase = lp(base)
        for dirpath, _dirs, names in os.walk(wbase):
            depth = 0 if dirpath == wbase else os.path.relpath(dirpath, wbase).count(os.sep) + 1
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
                seen += 1
                ext = ext_of(name)
                digest = sha256_of(path)
                sig = signature(path, ext, st.st_size)
                if "UNREADABLE" in (digest, sig):
                    errors += 1
                files_rows.append([proj, n, ext, st.st_size, iso(st.st_mtime), depth, digest, sig])
                if time.time() - last_print > 1.0:
                    log(f"  scanned {seen:,} files ... (now in {proj})")
                    last_print = time.time()
            if not recurse:
                break
        log(f"  {proj}: {n:,} files")

    # ---- aggregates ----
    proj_stats, ext_stats = {}, {}
    for proj, _n, ext, size, mod, _d, _h, _s in files_rows:
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
                            "header_signature"], files_rows),
        "Projects.csv": table(["project", "file_count", "total_bytes", "distinct_extensions",
                               "first_modified_utc", "last_modified_utc"],
                              [[p, s[0], s[1], len(s[4]), s[2], s[3]] for p, s in sorted(proj_stats.items())]),
        "Extensions.csv": table(["project", "ext", "file_count", "total_bytes", "avg_bytes"],
                                [[p, e, s[0], s[1], s[1] // s[0]] for (p, e), s in sorted(ext_stats.items())]),
        "Run_info.csv": table(["key", "value"], [
            ["tool_version", VERSION], ["run_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")],
            ["os", platform.platform()], ["python", platform.python_version()],
            ["folder_name", label], ["mode", "docker" if os.environ.get("PROFILER_IN_DOCKER") else "native"],
            ["projects", len(units)],
            ["files", len(files_rows)], ["total_bytes", sum(r[3] for r in files_rows)],
            ["read_errors", errors], ["ignored_system_files", junk], ["seconds", round(time.time() - t0, 1)]]),
        "WHAT_IS_IN_THIS_ZIP.txt": (
            "This archive contains file METRICS only: extensions, sizes, dates, SHA-256 fingerprints and\r\n"
            "short format tags. It contains no file names, no folder paths below the project name, and no\r\n"
            "file contents. You can open every file here in Excel or Notepad to check.\r\n"),
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in tables.items():
            z.writestr(name, text.encode("utf-8-sig") if name.endswith(".csv") else text.encode("utf-8"))
    return zip_path, len(units), len(files_rows), errors


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
        zip_path, nproj, nfiles, errors = profile(folder, out_dir, label=label)
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
