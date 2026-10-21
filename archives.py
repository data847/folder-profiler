"""Look INSIDE archives for the Folder Profiler. Standard library only (plus the system `bsdtar` for 7z/rar, if present).

    zip                              zipfile            list + read entries
    tar, tar.gz/.tgz, .bz2, .xz      tarfile            list + read entries
    7z, rar                          `bsdtar` (libarchive; Windows 10+ ships it as tar.exe)   LIST ONLY - names, sizes, dates
Archives inside archives are opened up to MAX_DEPTH levels (the inner archive is read into memory, up to MAX_NESTED_BYTES).

Nothing is ever extracted to disk. Every function returns None / skips quietly on damaged, encrypted, oversized or
unsupported archives. An entry is described by (chain, inner): `chain` = names of the nested archives to open first (empty for an
entry of the archive itself), `inner` = the entry's path inside the innermost one.
"""
import io
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
EXTERNAL_SUFFIXES = (".7z", ".rar")
MAX_ENTRIES = 300_000          # per top-level archive
MAX_NESTED_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 2                  # archives inside archives
LIST_TIMEOUT = 600


def kind_of(name):
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(TAR_SUFFIXES):
        return "tar"
    if n.endswith(".7z"):
        return "7z"
    if n.endswith(".rar"):
        return "rar"
    return None


_BSDTAR = []


def bsdtar():
    """Path of a libarchive-based tar, or None. Cached."""
    if not _BSDTAR:
        found = shutil.which("bsdtar")
        if not found:
            t = shutil.which("tar")
            try:
                if t and "libarchive" in subprocess.run([t, "--version"], capture_output=True, text=True, timeout=10).stdout:
                    found = t
            except Exception:
                found = None
        _BSDTAR.append(found)
    return _BSDTAR[0]


def _iso(ts):
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _zip_iso(dt):
    try:
        return datetime(*dt).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _clean(name):
    """Inner path with forward slashes, no leading slash / drive / '..' parts (we never extract, but names are recorded)."""
    parts = [p for p in re.split(r"[\\/]+", name) if p not in ("", ".", "..")]
    return "/".join(p.replace("\x00", "") for p in parts)


# ------------------------------------------------------------------------- opening (with nesting)
class _Handle:
    def __init__(self, obj, kind):
        self.obj, self.kind = obj, kind

    def close(self):
        try:
            self.obj.close()
        except Exception:
            pass


def _open_top(path):
    k = kind_of(path)
    if k == "zip":
        return _Handle(zipfile.ZipFile(path), "zip")
    if k == "tar":
        return _Handle(tarfile.open(path, "r:*"), "tar")
    return None


def _entry_bytes(h, inner, limit):
    """Bytes of one entry of an open handle, or None (too big / unreadable)."""
    try:
        if h.kind == "zip":
            info = h.obj.getinfo(inner)
            if info.file_size > limit:
                return None
            with h.obj.open(info) as f:
                return f.read()
        m = h.obj.getmember(inner)
        if m.size > limit:
            return None
        f = h.obj.extractfile(m)
        return f.read() if f else None
    except Exception:
        return None


def _open_nested(h, name):
    data = _entry_bytes(h, name, MAX_NESTED_BYTES)
    if data is None:
        return None
    k = kind_of(name)
    try:
        if k == "zip":
            return _Handle(zipfile.ZipFile(io.BytesIO(data)), "zip")
        if k == "tar":
            return _Handle(tarfile.open(fileobj=io.BytesIO(data), mode="r:*"), "tar")
    except Exception:
        return None
    return None


def _open_chain(path, chain):
    handles = []
    try:
        h = _open_top(path)
        if h is None:
            return None, handles
        handles.append(h)
        for name in chain:
            h = _open_nested(handles[-1], name)
            if h is None:
                return None, handles
            handles.append(h)
        return handles[-1], handles
    except Exception:
        return None, handles


# ------------------------------------------------------------------------- listing
def _list_handle(h, chain, out, depth):
    """Append {"chain","inner","size","mtime","readable"} for every file in an open archive (recursing into nested ones)."""
    try:
        if h.kind == "zip":
            members = [(i.filename, i.file_size, _zip_iso(i.date_time), bool(i.flag_bits & 1)) for i in h.obj.infolist() if not i.is_dir()]
        else:
            members = [(m.name, m.size, _iso(m.mtime), False) for m in h.obj.getmembers() if m.isfile()]
    except Exception:
        return
    for name, size, mtime, encrypted in members:
        if len(out) >= MAX_ENTRIES:
            return
        inner = _clean(name)
        if not inner:
            continue
        out.append({"chain": chain, "inner": inner, "size": int(size), "mtime": mtime, "readable": not encrypted, "raw": name})
        if depth < MAX_DEPTH and not encrypted and kind_of(inner) in ("zip", "tar") and size <= MAX_NESTED_BYTES:
            nested = _open_nested(h, name)
            if nested is not None:
                try:
                    _list_handle(nested, chain + (inner,), out, depth + 1)
                finally:
                    nested.close()


_BSD_LINE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(?:(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)|([A-Z][a-z]{2})\s+(\d+)\s+(\d{2}:\d{2}|\d{4}))\s+(.*)$")


def _list_external(path):
    exe = bsdtar()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-tvf", path], capture_output=True, text=True, errors="replace", timeout=LIST_TIMEOUT)
    except Exception:
        return None
    if r.returncode != 0 and not r.stdout:
        return None
    out = []
    for line in r.stdout.splitlines():
        if not line or line[0] == "d":                      # directories
            continue
        m = _BSD_LINE.match(line)
        if not m:
            continue
        size, d, hm, mon, day, tm, name = m.groups()
        mtime = f"{d}T{hm}:00Z" if d else ""
        inner = _clean(name.split(" -> ")[0])
        if inner and len(out) < MAX_ENTRIES:
            out.append({"chain": (), "inner": inner, "size": int(size), "mtime": mtime, "readable": False, "raw": name})
    return out


def list_archive(path):
    """-> list of entry dicts (files only), or None if the archive cannot be listed. Never raises."""
    try:
        k = kind_of(path)
        if k in ("zip", "tar"):
            h = _open_top(path)
            if h is None:
                return None
            try:
                out = []
                _list_handle(h, (), out, 0)
                return out
            finally:
                h.close()
        if k in ("7z", "rar"):
            return _list_external(path)
    except Exception:
        return None
    return None


# ------------------------------------------------------------------------- reading selected entries
def read_entries(path, wanted):
    """Yield (chain, inner, file_object_or_None) for each wanted (chain, inner), opening each (nested) archive once.
    The file object is only valid until the next iteration."""
    by_chain = {}
    for chain, inner in wanted:
        by_chain.setdefault(chain, []).append(inner)
    for chain, inners in by_chain.items():
        h, handles = _open_chain(path, chain)
        try:
            if h is None:
                for inner in inners:
                    yield chain, inner, None
                continue
            if h.kind == "zip":
                names = {_clean(i.filename): i for i in h.obj.infolist() if not i.is_dir()}
                for inner in inners:
                    info = names.get(inner)
                    try:
                        f = h.obj.open(info) if info is not None else None
                    except Exception:
                        f = None
                    try:
                        yield chain, inner, f
                    finally:
                        if f is not None:
                            f.close()
            else:
                want = set(inners)
                for m in h.obj.getmembers():
                    inner = _clean(m.name)
                    if inner in want and m.isfile():
                        try:
                            f = h.obj.extractfile(m)
                        except Exception:
                            f = None
                        try:
                            yield chain, inner, f
                        finally:
                            if f is not None:
                                f.close()
                        want.discard(inner)
                for inner in want:
                    yield chain, inner, None
        finally:
            for x in reversed(handles):
                x.close()
