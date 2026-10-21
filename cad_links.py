"""Read ASSEMBLY REFERENCES out of CAD files (detail level 2 of the Folder Profiler). Standard library only.

What is extracted - and ONLY this:
    the file NAMES (no folders) of the other CAD files an assembly points to, e.g. {"bracket.par", "bolt.par"},
    or, for STEP files, the product-structure "parent product -> child product" name pairs.
Nothing else is returned: no geometry, no text, no properties, no paths. Files are opened read-only.

Formats
    SolidWorks .sldasm/.sldprt/.slddrw  proprietary container: deflate-compressed chunks after the marker
                                         14 00 06 00 08 00; the chunks hold UTF-16 paths of referenced files.
    Solid Edge / Inventor (.par .psm .asm .dft .iam .ipt .idw ...)  OLE compound documents; every stream is searched.
    STEP (.stp .step)                    PRODUCT / PRODUCT_DEFINITION / NEXT_ASSEMBLY_USAGE_OCCURRENCE entities.
Anything unreadable (other formats, damaged or too large files) returns None - never raises.
"""
import collections
import os
import re
import struct
import zlib

CAD_EXTS = ("par", "psm", "asm", "pwd", "dft", "iam", "ipt", "ipn", "idw", "sldprt", "sldasm", "slddrw", "prt",
            "catpart", "catproduct", "catdrawing", "x_t", "x_b", "step", "stp", "igs", "iges", "3dm", "f3d")
MAX_FILE_BYTES = 256 * 1024 * 1024       # larger files are skipped (None)
MAX_STEP_BYTES = 400 * 1024 * 1024
MAX_STREAM_BYTES = 128 * 1024 * 1024
MAX_REFS = 20000

_EXT_ALT = "|".join(re.escape(e) for e in CAD_EXTS)
_ASCII_REF = re.compile(rb"[^\\/:*?\"<>|\x00-\x1f]{1,200}\.(?:" + _EXT_ALT.encode() + rb")(?![A-Za-z0-9_])", re.I)
_UTF16_REF = re.compile(
    rb"(?:[\x20-\x7e\x80-\xff]\x00){1,200}\.\x00(?:" + b"|".join(b"".join(bytes([ord(c)]) + b"\x00" for c in e).replace(b"_", b"_")
                                                                for e in CAD_EXTS) + rb")(?:[^A-Za-z0-9_]|\x00\x00|$)", re.I)
_SW_MARK = re.compile(rb"\x14\x00\x06\x00\x08\x00")


def _base(name):
    return re.split(r"[\\/]", name)[-1].strip().lower()


def _refs_in(blob, out):
    for m in _UTF16_REF.finditer(blob):
        txt = m.group(0).decode("utf-16le", "ignore")
        txt = re.sub(r"[^\w\-. ()\[\]$#&+,\\/:]+$", "", txt)
        out[_base(txt)] += 1
    for m in _ASCII_REF.finditer(blob):
        out[_base(m.group(0).decode("latin-1"))] += 1


def _clean(refs, own_name=""):
    own = own_name.lower()
    good = {r for r in refs if r and r != own and "." in r and 2 < len(r) < 200 and r.rsplit(".", 1)[-1] in CAD_EXTS}
    return sorted(good)[:MAX_REFS]


# ------------------------------------------------------------------------- SolidWorks
def solidworks_refs(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            b = f.read()
        return _sw_from_bytes(b, os.path.basename(path))
    except Exception:
        return None


def _sw_from_bytes(b, own_name=""):
    try:
        refs = collections.Counter()
        budget = MAX_STREAM_BYTES
        for m in _SW_MARK.finditer(b):
            for off in range(6, 60):                 # chunk header length varies; the first offset that inflates wins
                try:
                    out = zlib.decompressobj(-15).decompress(b[m.start() + off:m.start() + off + 8_000_000], 16_000_000)
                except Exception:
                    continue
                if len(out) < 16:
                    continue
                budget -= len(out)
                _refs_in(out, refs)
                break
            if budget <= 0:
                break
        if not refs and not _SW_MARK.search(b):
            return None                              # not a container we understand
        return _clean(refs, own_name)
    except Exception:
        return None


# ------------------------------------------------------------------------- OLE / compound document
class _CFB:
    """Minimal read-only Compound File Binary reader (v3 and v4)."""
    SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    FREE, END = 0xFFFFFFFF, 0xFFFFFFFE

    def __init__(self, data):
        if data[:8] != self.SIG or len(data) < 512:
            raise ValueError("not OLE")
        self.d = data
        shift = struct.unpack_from("<H", data, 0x1E)[0]
        mshift = struct.unpack_from("<H", data, 0x20)[0]
        if shift not in (9, 12) or mshift != 6:
            raise ValueError("bad sector size")
        self.ss, self.ms = 1 << shift, 1 << mshift
        n_fat, self.dir_start, _t, self.cutoff, self.mf_start, n_mf, self.dif_start, n_dif = struct.unpack_from(
            "<I I I I I I I I", data, 0x2C)
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        s, hops = self.dif_start, 0
        while s not in (self.FREE, self.END) and hops < n_dif + 1:
            sec = self._sector(s)
            vals = struct.unpack_from(f"<{self.ss // 4}I", sec)
            difat += list(vals[:-1]); s = vals[-1]; hops += 1
        fat = []
        for s in difat[:n_fat]:
            if s in (self.FREE, self.END):
                continue
            fat += list(struct.unpack_from(f"<{self.ss // 4}I", self._sector(s)))
        self.fat = fat
        self.minifat = []
        for s in self._chain(self.mf_start, limit=n_mf + 1):
            self.minifat += list(struct.unpack_from(f"<{self.ss // 4}I", self._sector(s)))
        self.entries = self._directory()
        root = self.entries[0]
        self.ministream = self._read_chain(root["start"], root["size"]) if root["size"] else b""

    def _sector(self, i):
        off = 512 + i * self.ss if self.ss == 512 else self.ss + i * self.ss
        sec = self.d[off:off + self.ss]
        if len(sec) < self.ss:
            raise ValueError("truncated")
        return sec

    def _chain(self, start, limit=1 << 20):
        out, s, seen = [], start, set()
        while s not in (self.FREE, self.END) and len(out) < limit:
            if s in seen or s >= len(self.fat):
                break
            seen.add(s); out.append(s); s = self.fat[s]
        return out

    def _read_chain(self, start, size):
        buf = b"".join(self._sector(s) for s in self._chain(start))
        return buf[:size]

    def _directory(self):
        raw = b"".join(self._sector(s) for s in self._chain(self.dir_start))
        ents = []
        for i in range(0, len(raw) - 127, 128):
            e = raw[i:i + 128]
            nlen = struct.unpack_from("<H", e, 0x40)[0]
            name = e[:max(0, nlen - 2)].decode("utf-16le", "ignore") if 2 <= nlen <= 64 else ""
            typ = e[0x42]
            left, right, child = struct.unpack_from("<III", e, 0x44)
            start, size = struct.unpack_from("<I Q", e, 0x74)
            ents.append({"name": name, "type": typ, "l": left, "r": right, "c": child, "start": start,
                         "size": size & 0xFFFFFFFF if self.ss == 512 else size})
        if not ents:
            raise ValueError("no directory")
        return ents

    def streams(self):
        """Yield (name, bytes) for every stream under the root (depth-first; each entry visited once)."""
        seen, stack = set(), [self.entries[0]["c"]]
        while stack:
            i = stack.pop()
            if i in (self.FREE,) or i >= len(self.entries) or i in seen:
                continue
            seen.add(i)
            e = self.entries[i]
            stack += [e["l"], e["r"]]
            if e["type"] == 1:
                stack.append(e["c"])
            elif e["type"] == 2 and e["size"] <= MAX_STREAM_BYTES:
                if e["size"] < self.cutoff:
                    buf = b"".join(self.ministream[s * self.ms:(s + 1) * self.ms] for s in self._mini_chain(e["start"]))
                    yield e["name"], buf[:e["size"]]
                else:
                    yield e["name"], self._read_chain(e["start"], e["size"])

    def _mini_chain(self, start):
        out, s, seen = [], start, set()
        while s not in (self.FREE, self.END) and len(out) < 1 << 20:
            if s in seen or s >= len(self.minifat):
                break
            seen.add(s); out.append(s); s = self.minifat[s]
        return out


def ole_refs(path):
    """File names referenced from inside an OLE compound document (Solid Edge, Inventor, ...). None if not OLE."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
        return _ole_from_bytes(data, os.path.basename(path))
    except Exception:
        return None


def _ole_from_bytes(data, own_name=""):
    try:
        cfb = _CFB(data)
        refs = collections.Counter()
        for _name, blob in cfb.streams():
            _refs_in(blob, refs)
        return _clean(refs, own_name)
    except Exception:
        return None


# ------------------------------------------------------------------------- STEP
_Q = re.compile(r"'((?:[^']|'')*)'")
_R = re.compile(r"#(\d+)")


def step_structure_bytes(data):
    """Same as step_structure() for content already in memory (files inside archives)."""
    import io
    if len(data) > MAX_STEP_BYTES:
        return None
    return _step_from_lines(io.StringIO(data.decode("latin-1", "ignore")))


def step_structure(path):
    """Product structure of a STEP file -> {"parents": {parent_name: [child_name, ...]}} (names lower-cased, both the
    PRODUCT id and name are returned as candidates), or None when unreadable. {} parents = a single part, no assembly."""
    try:
        if os.path.getsize(path) > MAX_STEP_BYTES:
            return None
        with open(path, "r", errors="ignore") as f:
            return _step_from_lines(f)
    except Exception:
        return None


def _step_from_lines(lines):
    try:
        prod, form, pdef, nauo = {}, {}, {}, []
        buf = ""
        for line in lines:
            if not buf:
                if not line.startswith("#"):
                    continue
                buf = line
            else:
                buf += line
            if ";" not in line:
                continue
            st, buf = buf, ""
            m = re.match(r"#(\d+)\s*=\s*([A-Z_0-9]+)\s*\((.*)\)\s*;", st.replace("\n", " ").replace("\r", " "), re.S)
            if not m:
                continue
            n, typ, args = int(m.group(1)), m.group(2), m.group(3)
            if typ == "PRODUCT":
                q = [x.replace("''", "'") for x in _Q.findall(args)]
                prod[n] = tuple(x.lower() for x in q[:2] if x.strip())
            elif typ.startswith("PRODUCT_DEFINITION_FORMATION"):
                r = _R.findall(args)
                if r:
                    form[n] = int(r[0])
            elif typ == "PRODUCT_DEFINITION":
                r = _R.findall(args)
                if r:
                    pdef[n] = int(r[0])
            elif typ == "NEXT_ASSEMBLY_USAGE_OCCURRENCE":
                r = _R.findall(args)
                if len(r) >= 2:
                    nauo.append((int(r[0]), int(r[1])))
        names = lambda pd: prod.get(form.get(pdef.get(pd, -1), -1), ())
        parents = collections.defaultdict(set)
        for a, b in nauo:
            pa, ch = names(a), names(b)
            for p in pa:                                # a parent is reachable by its PRODUCT id and by its name
                parents[p] |= set(ch)
        return {"parents": {k: sorted(v) for k, v in parents.items()}}
    except Exception:
        return None


# ------------------------------------------------------------------------- dispatch
SW_EXTS = {"sldasm", "sldprt", "slddrw"}
OLE_EXTS = {"par", "psm", "asm", "pwd", "dft", "iam", "ipt", "ipn", "idw"}
STEP_EXTS = {"step", "stp"}
LINK_EXTS = SW_EXTS | OLE_EXTS | STEP_EXTS
ASSEMBLY_LIKE = {"sldasm", "asm", "iam"}                 # only these are searched for references by default


def read_links(path, ext):
    """-> ("native", [child file names]) | ("step", {parent: [children]}) | None."""
    if ext in SW_EXTS:
        r = solidworks_refs(path)
        return ("native", r) if r is not None else None
    if ext in OLE_EXTS:
        r = ole_refs(path)
        return ("native", r) if r is not None else None
    if ext in STEP_EXTS:
        r = step_structure(path)
        return ("step", r["parents"]) if r is not None else None
    return None


def read_links_bytes(data, ext, name=""):
    """Like read_links() for a file whose content is already in memory (an entry of an archive)."""
    if ext in SW_EXTS:
        r = _sw_from_bytes(data, name)
        return ("native", r) if r is not None else None
    if ext in OLE_EXTS:
        r = _ole_from_bytes(data, name)
        return ("native", r) if r is not None else None
    if ext in STEP_EXTS:
        r = step_structure_bytes(data)
        return ("step", r["parents"]) if r is not None else None
    return None
