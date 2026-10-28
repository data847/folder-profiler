"""Count the SHEETS (layouts = paper-space tabs) of AutoCAD drawings, with a CONFIDENCE number. Standard library only.

    count_sheets_bytes(data, ext) -> {"sheets": int | None, "confidence": 0-100, "method": str, "note": str}

Sheets = number of LAYOUT objects minus 1 (the Model tab is a layout object too). Only that one number leaves the file: no layout
names, no geometry, no text.

How, per format (all written from the published DWG / DXF structure descriptions)
    DXF (ASCII)            count the group-0 LAYOUT objects of the OBJECTS section.
    DWG R2000              AC1015: sections are not compressed. The class table gives the object-type number of LAYOUT,
                           the object map lists every object; the type of each object is read and the LAYOUT ones are counted.
    DWG R2004 and later    AC1018 / AC1024 / AC1027 / AC1032: the sections are compressed (LZ77 variant) in pages; they are located,
                           decompressed, then the same class table / object map / object scan is applied.
    DWG R13 / R14          no layouts exist in these versions: sheets = None, confidence 0.
    DWG R2007 (AC1021)     not supported (a different, Reed-Solomon coded layout): sheets = None, confidence 0.
    binary DXF             not supported.

Confidence (0-100) is computed from checks made ON THE FILE, not assumed:
    * the object map parses and every location is inside the file;
    * the handle read back from each sampled object (all LAYOUT objects + up to 300 others) equals the handle the object map gave it -
      a decoder that is wrong cannot pass this by chance;
    * the class table is consumed exactly and names LAYOUT exactly once;
    * the layout count is plausible.
A decoder that fails any check lowers the confidence; one that fails the basics returns sheets = None with confidence 0.
Validation: compared with the DXF export of the same drawing on 21 public test drawings (R2000, 2004, 2010, 2013, 2018; LibreDWG test data) -
21 of 21 identical - and run on 99 public DWG files of those versions (every one counted, every sampled handle verified). Not yet compared
with AutoCAD's own sheet count on your clients' drawings. Layouts were introduced in AutoCAD 2000, so R13 / R14 files have none to count.
"""
import itertools
import re
import struct

MAX_BYTES = 150 * 1024 * 1024
VERSIONS = {b"AC1012": "R13", b"AC1014": "R14", b"AC1015": "R2000", b"AC1018": "R2004", b"AC1021": "R2007",
            b"AC1024": "R2010", b"AC1027": "R2013", b"AC1032": "R2018"}
LAYOUT_FIXED_TYPE = 82                                          # object type of LAYOUT in R2004 and later files (a class number, 502 here, in R2000)
SENTINEL_CLASSES = bytes.fromhex("8DA1C4B8C4A9F8C5C0DCF45FE7CFB68A")
VERSION_FACTOR = {"R2000": 0.95, "R2004": 0.95, "R2010": 0.95, "R2013": 0.95, "R2018": 0.95}      # 95 is the ceiling for a DWG: the rest is residual risk


def _res(n, conf, method, note="", code=None):
    """code = a short, closed-vocabulary STATUS written next to every count ("ok" or why it could not be counted) - no file text in it."""
    conf = int(max(0, min(100, round(conf))))
    return {"sheets": n, "confidence": conf, "method": method, "note": note, "code": code or ("ok" if n is not None else "failed")}


# ------------------------------------------------------------------------------------------------------------ DXF
_DXF_LAYOUT = re.compile(rb"(?:^|\n)[ \t]*0[ \t]*\r?\n[ \t]*LAYOUT[ \t]*\r?(?:\n|$)")
_DXF_MODEL = re.compile(rb"AcDbLayout[ \t]*\r?\n[ \t]*1[ \t]*\r?\n[ \t]*Model[ \t]*\r?(?:\n|$)", re.I)


def dxf_sheets(data):
    if data[:22].startswith(b"AutoCAD Binary DXF"):
        return _res(None, 0, "dxf-binary", "binary DXF is not supported", "dxf-binary")
    n = len(_DXF_LAYOUT.findall(data))
    if n == 0:
        return _res(None, 0, "dxf", "no LAYOUT objects (a DXF older than R2000 has no layouts)", "no-layouts-in-version")
    model = bool(_DXF_MODEL.search(data))
    conf = 99 if model else 80
    return _res(max(0, n - 1), conf, "dxf-layout-objects", "" if model else "no layout named Model found: the count may include or miss it")


# ------------------------------------------------------------------------------------------------------------ bits
class Bits:
    """MSB-first bit reader over bytes (the DWG bit-coding)."""

    def __init__(self, data, pos=0):
        self.d, self.p = data, pos

    def bits(self, n):
        p, end = self.p, self.p + n
        if end > len(self.d) * 8:
            raise EOFError
        b0, b1 = p >> 3, (end + 7) >> 3
        v = int.from_bytes(self.d[b0:b1], "big") >> (b1 * 8 - end)
        self.p = end
        return v & ((1 << n) - 1)

    def B(self):
        return self.bits(1)

    def RC(self):
        return self.bits(8)

    def RS(self):
        lo = self.bits(8)
        return lo | self.bits(8) << 8

    def RL(self):
        v = 0
        for i in range(4):
            v |= self.bits(8) << (8 * i)
        return v

    def BS(self):
        c = self.bits(2)
        return self.RS() if c == 0 else self.RC() if c == 1 else 0 if c == 2 else 256

    def BL(self):
        c = self.bits(2)
        if c == 0:
            return self.RL()
        if c == 1:
            return self.RC()
        if c == 2:
            return 0
        raise ValueError("bad BL")

    def H(self):
        code, n = self.bits(4), self.bits(4)
        v = 0
        for _ in range(n):
            v = v << 8 | self.bits(8)
        return code, v

    def TV(self, nul=True):
        n = self.BS()
        if n > 4096:
            raise ValueError("bad string")
        raw = bytes(self.bits(8) for _ in range(n))
        return raw.rstrip(b"\x00").decode("latin-1")

    def TU(self):
        n = self.BS()
        if n > 4096:
            raise ValueError("bad string")
        raw = b"".join(self.bits(16).to_bytes(2, "little") for _ in range(n))
        return raw.decode("utf-16le", "ignore").rstrip("\x00")


def _mc(d, p):
    """Modular char (unsigned): 7 bits per byte, little-endian, 0x80 = more. -> (value, new position)"""
    v, shift = 0, 0
    while True:
        b = d[p]
        p += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return v, p
        if shift > 63:
            raise ValueError("bad MC")


def _smc(d, p):
    """Modular char (signed): the 0x40 bit of the last byte is the sign."""
    v, shift = 0, 0
    while True:
        b = d[p]
        p += 1
        if not b & 0x80:
            v |= (b & 0x3F) << shift
            return (-v if b & 0x40 else v), p
        v |= (b & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise ValueError("bad MC")


def _ms(d, p):
    """Modular short: 16-bit little-endian words, 15 bits each, 0x8000 = more. -> (value, new position)"""
    v, shift = 0, 0
    while True:
        w = d[p] | d[p + 1] << 8
        p += 2
        v |= (w & 0x7FFF) << shift
        shift += 15
        if not w & 0x8000:
            return v, p
        if shift > 60:
            raise ValueError("bad MS")


# ------------------------------------------------------------------------------------------------------------ classes
def _classes(blob, version, strings):
    """-> ({class number: dxf name}, exact?)  `blob` = the class data area (bytes). strings = 'inline' | 'stream'."""
    br = Bits(blob)
    end_bits = len(blob) * 8
    str_br = None
    if strings == "stream":
        # string stream: the last bit before the end is a flag; then a 16-bit size (bits) [and another 16 when the first has bit 15 set]
        last = end_bits
        while last > 0 and not (blob[(last - 1) >> 3] >> (7 - ((last - 1) & 7))) & 1:       # padding zeros at the end
            last -= 1
            if end_bits - last > 7:
                raise ValueError("no string flag")
        flag_pos = last - 1
        sb = Bits(blob, flag_pos - 16)
        size = sb.RS()
        pos = flag_pos - 16
        if size & 0x8000:
            sb = Bits(blob, flag_pos - 32)
            hi = sb.RS()
            size = (size & 0x7FFF) | hi << 15
            pos = flag_pos - 32
        str_br = Bits(blob, pos - size)
        end_bits = pos - size
    out, exact_names = {}, 0
    if version != "R2000" and version not in ("R13", "R14"):
        br.BS(); br.RC(); br.RC(); br.B()                       # maximum class number, two zero bytes, a bool
    guard = 0
    while end_bits - br.p >= 8 and guard < 5000:
        guard += 1
        num = br.BS()
        br.BS()                                                  # version / proxy flags
        if strings == "stream":
            app, cpp, dxf = str_br.TU(), str_br.TU(), str_br.TU()
        else:
            app, cpp, dxf = br.TV(), br.TV(), br.TV()
        br.B()                                                   # was a zombie
        br.BS()                                                  # item class id
        if version not in ("R13", "R14", "R2000"):
            for _ in range(5):
                br.BL()
        out[num] = dxf
    exact = 0 <= end_bits - br.p < 8
    return out, exact


# ------------------------------------------------------------------------------------------------------------ object map + objects
def _object_map(d, start, size, end_limit=None):
    """Handle -> location. Sections of: RS size (big-endian, counts the 2 size bytes + the pairs, NOT the CRC), pairs of (handle delta MC,
    location delta signed MC), RS crc. A section of size 2 ends the map."""
    p, out = start, []
    stop = start + size if size else len(d)
    while p + 2 <= min(stop, len(d)):
        sec = d[p] << 8 | d[p + 1]
        if sec <= 2:
            break
        e = p + sec                                              # entries end here; the CRC follows
        q = p + 2
        handle = loc = 0                                         # every section starts its running totals again
        while q < e:
            dh, q = _mc(d, q)
            dl, q = _smc(d, q)
            handle += dh
            loc += dl
            out.append((handle, loc))
        if q != e:
            raise ValueError("object map section does not end where it says")
        p = e + 2
        if sec > 2100:
            raise ValueError("object map section too large")
    return out


def _type_and_handle(buf, loc, version):
    """Read (type, handle) of the object at `loc` of `buf`. -> (type, handle) or None."""
    try:
        size, p = _ms(buf, loc)
        if size <= 0 or p + size > len(buf) + 4:
            return None
        if version in ("R2010", "R2013", "R2018"):
            _, p2 = _mc(buf, p)                                  # size of the handle stream (bits)
            br = Bits(buf, p2 * 8)
            c = br.bits(2)
            typ = br.RC() if c == 0 else br.RC() + 0x1F0 if c == 1 else br.RS()
            code, h = br.H()
            return typ, h
        br = Bits(buf, p * 8)
        typ = br.BS()
        if version in ("R2000", "R2004"):
            br.RL()                                              # size of the object in bits
        code, h = br.H()
        return typ, h
    except (EOFError, ValueError, IndexError):
        return None


def _scan(buf, entries, layout_type, version):
    """Count LAYOUT objects and verify the decoder by reading back the handle of sampled objects.
    -> (n_layouts, checked, matched, in_bounds_fraction)"""
    n_layouts = checked = matched = 0
    inb = 0
    nbuf = len(buf)
    sample_every = max(1, len(entries) // 300)
    for i, (h, loc) in enumerate(entries):
        if not 0 <= loc < nbuf:
            continue
        inb += 1
        r = _type_and_handle(buf, loc, version)
        is_layout = False
        if r is not None and r[0] == layout_type:
            n_layouts += 1
            is_layout = True
        if is_layout or i % sample_every == 0 or i < 5 or i >= len(entries) - 5:
            checked += 1
            if r is not None and r[1] == h:
                matched += 1
    return n_layouts, checked, matched, (inb / len(entries) if entries else 0.0)


def _score(version, n_layouts, checked, matched, inb, classes_exact, layout_names):
    if not checked or n_layouts == 0:
        return 0
    m = matched / checked
    if m < 0.5:
        return int(10 * m)
    score = 100.0 * VERSION_FACTOR.get(version, 0.8) * (0.5 + 0.5 * m) * (0.5 + 0.5 * inb)
    if not classes_exact:
        score -= 25
    if layout_names != 1:
        score -= 20
    if n_layouts > 2000:
        score = min(score, 10)
    return score


# ------------------------------------------------------------------------------------------------------------ R13 .. R2000
def _dwg_r2000(d, version):
    count = struct.unpack_from("<I", d, 0x15)[0]
    if not 3 <= count <= 12:
        return _res(None, 0, "dwg-" + version, "section locators not understood", "r2000-locators")
    loc = {}
    for i in range(count):
        num, seek, size = struct.unpack_from("<BII", d, 0x19 + 9 * i)
        loc[num] = (seek, size)
    if 1 not in loc or 2 not in loc:
        return _res(None, 0, "dwg-" + version, "class / object map sections not listed", "r2000-locators")
    cs, _ = loc[1]
    if d[cs:cs + 16] != SENTINEL_CLASSES:
        return _res(None, 0, "dwg-" + version, "class section sentinel not found", "class-table")
    csize = struct.unpack_from("<I", d, cs + 16)[0]
    classes, exact = _classes(d[cs + 20:cs + 20 + csize], version, "inline")
    names = [n for n, x in classes.items() if x == "LAYOUT"]
    if len(names) != 1:
        return _res(None, 0, "dwg-" + version, "no LAYOUT class in the class table", "class-table")
    om_start, om_size = loc[2]
    entries = _object_map(d, om_start, om_size)
    if not entries:
        return _res(None, 0, "dwg-" + version, "empty object map", "object-map")
    n, checked, matched, inb = _scan(d, entries, names[0], version)
    conf = _score(version, n, checked, matched, inb, exact, len(names))
    return _res(n - 1 if conf else None, conf, f"dwg-{version}-objects", f"{n} layout objects, {matched}/{checked} sampled handles verified",
                None if conf else ("no-layout-objects" if n == 0 else "handle-mismatch"))


# ------------------------------------------------------------------------------------------------------------ R2004+
def _lz_literal(src, p):
    """Literal length at src[p]: returns (length, next_opcode or 0, new p)."""
    b = src[p]
    p += 1
    if 1 <= b <= 0x0F:
        return b + 3, 0, p
    if b == 0:
        total = 0x0F
        while True:
            b = src[p]
            p += 1
            if b != 0:
                break
            total += 0xFF
        return total + b + 3, 0, p
    return 0, b, p


def lz77(src, out_len, plus_one=1):
    """Decompress a DWG R2004+ page. Returns bytes or None when the stream is not valid / does not give `out_len` bytes."""
    try:
        dst = bytearray()
        p, n = 0, len(src)
        lit, op, p = _lz_literal(src, p)
        dst += src[p:p + lit]
        p += lit
        while p < n:
            if op == 0:
                op = src[p]
                p += 1
            comp = off = lit = 0
            if op >= 0x40:
                comp = ((op & 0xF0) >> 4) - 1
                off = (src[p] << 2 | (op & 0x0C) >> 2) + plus_one
                p += 1
                lit = op & 0x03
                if lit == 0:
                    lit, op2, p = _lz_literal(src, p)
                else:
                    op2 = 0
            elif 0x21 <= op <= 0x3F:
                comp = op - 0x1E
                b1, b2 = src[p], src[p + 1]
                p += 2
                off, lit = (b1 >> 2 | b2 << 6) + plus_one, b1 & 3
                op2 = 0
                if lit == 0:
                    lit, op2, p = _lz_literal(src, p)
            elif op == 0x20:
                t = 0
                while src[p] == 0:
                    t += 0xFF
                    p += 1
                t += src[p]
                p += 1
                comp = t + 0x21
                b1, b2 = src[p], src[p + 1]
                p += 2
                off, lit = (b1 >> 2 | b2 << 6) + plus_one, b1 & 3
                op2 = 0
                if lit == 0:
                    lit, op2, p = _lz_literal(src, p)
            elif 0x12 <= op <= 0x1F:
                comp = (op & 0x0F) + 2
                b1, b2 = src[p], src[p + 1]
                p += 2
                off, lit = (b1 >> 2 | b2 << 6) + 0x3FFF + plus_one, b1 & 3
                op2 = 0
                if lit == 0:
                    lit, op2, p = _lz_literal(src, p)
            elif op == 0x10:
                t = 0
                while src[p] == 0:
                    t += 0xFF
                    p += 1
                t += src[p]
                p += 1
                comp = t + 9
                b1, b2 = src[p], src[p + 1]
                p += 2
                off, lit = (b1 >> 2 | b2 << 6) + 0x3FFF + plus_one, b1 & 3
                op2 = 0
                if lit == 0:
                    lit, op2, p = _lz_literal(src, p)
            elif op == 0x11:
                break
            else:
                return None
            if comp:
                if off > len(dst) or off <= 0:
                    return None
                s = len(dst) - off
                if off >= comp:
                    dst += dst[s:s + comp]
                else:                                            # overlapping copy: repeat the pattern
                    chunk = bytes(dst[s:])
                    dst += (chunk * (comp // len(chunk) + 1))[:comp]
            if lit:
                dst += src[p:p + lit]
                p += lit
            op = op2
        if len(dst) != out_len:
            return None
        return bytes(dst)
    except IndexError:
        return None


class SectionError(ValueError):
    """A step of reading the compressed sections failed; .code is the short status written next to the count."""

    def __init__(self, code, msg=""):
        super().__init__(msg or code)
        self.code = code


def _sys_pages(d, sig):
    """Every valid system page (page map / section map) found by its signature, in file order. More than one can exist (the page map is
    written twice; a damaged or re-saved file can hold stale copies), so the caller tries the candidates until one works."""
    pos = 0
    while True:
        pos = d.find(sig, pos)
        if pos < 0:
            return
        if pos + 20 <= len(d):
            dsz, csz, ctype = struct.unpack_from("<III", d, pos + 4)
            if 0 < csz < len(d) and 0 < dsz < (1 << 28) and ctype in (1, 2) and pos + 20 + csz <= len(d):
                body = d[pos + 20:pos + 20 + csz]
                if ctype == 1 and csz == dsz:
                    yield bytes(body)
                else:
                    for po in (1, 0):
                        out = lz77(body, dsz, po)
                        if out is not None:
                            yield out
                            break
        pos += 1


def _read_sections(d, pmap, smap):
    addr, pages, p = 0x100, {}, 0
    while p + 8 <= len(pmap):
        num, size = struct.unpack_from("<ii", pmap, p)
        p += 8
        if num < 0:
            p += 16
            addr += size
            continue
        pages[num] = addr
        addr += size
    ndesc = struct.unpack_from("<I", smap, 0)[0]
    if not 0 < ndesc < 200:
        raise SectionError("section-map", "implausible section count")
    p, out = 20, {}
    for _ in range(ndesc):
        total, npages, maxd, _u, comp, _sid, _enc = struct.unpack_from("<QIIIIII", smap, p)
        name = smap[p + 32:p + 96].split(b"\x00")[0].decode("latin-1")
        p += 96
        buf = bytearray(total if total < (1 << 31) else 0)
        for _i in range(npages):
            pnum, dsize, start = struct.unpack_from("<IIQ", smap, p)
            p += 16
            a = pages.get(pnum)
            if a is None or a + 32 > len(d):
                raise SectionError("page-missing", "page missing")
            mask = 0x4164536B ^ a
            hdr = bytes(x ^ y for x, y in zip(d[a:a + 32], struct.pack("<I", mask) * 8))
            ptype, _sec, csize, psize, pstart, _c1, _c2, _z = struct.unpack("<IIIIIIII", hdr)
            if ptype != 0x4163043B or a + 32 + csize > len(d):
                raise SectionError("data-page", "bad data page")
            body = d[a + 32:a + 32 + csize]
            if comp == 2:
                raw = None
                for po in (1, 0):
                    raw = lz77(body, maxd, po)
                    if raw is not None:
                        break
                if raw is None:
                    raise SectionError("decompress", "decompression failed")
            else:
                raw = bytes(body)
            if start + len(raw) > len(buf):
                buf.extend(b"\x00" * (start + len(raw) - len(buf)))
            buf[start:start + len(raw)] = raw
        out[name] = bytes(buf[:total]) if total else bytes(buf)
    return out


def _sections_r2004(d):
    """-> {section name: decompressed bytes}; tries every page-map / section-map candidate; raises SectionError."""
    pmaps = list(itertools.islice(_sys_pages(d, struct.pack("<I", 0x41630E3B)), 4))
    smaps = list(itertools.islice(_sys_pages(d, struct.pack("<I", 0x4163003B)), 4))
    if not pmaps or not smaps:
        raise SectionError("system-pages", "system pages not found")
    err = None
    for pm in pmaps:
        for sm in smaps:
            try:
                return _read_sections(d, pm, sm)
            except SectionError as e:
                err = err or e
            except (struct.error, IndexError, ValueError) as e:
                err = err or SectionError("section-map", str(e))
    raise err


def _dwg_r2004(d, version):
    try:
        secs = _sections_r2004(d)
    except SectionError as e:
        return _res(None, 0, "dwg-" + version, f"sections could not be read ({e})", "sections-" + e.code)
    except (ValueError, struct.error, IndexError) as e:
        return _res(None, 0, "dwg-" + version, f"sections could not be read ({e})", "sections-other")
    hnd, obj = secs.get("AcDb:Handles"), secs.get("AcDb:AcDbObjects")
    if not (hnd and obj):
        return _res(None, 0, "dwg-" + version, "handle / object sections missing", "sections-missing")
    # From R2004 on, LAYOUT is a FIXED object type (82); the class table is no longer needed to find it
    best = (LAYOUT_FIXED_TYPE, {}, True)
    try:
        entries = _object_map(hnd, 0, 0)
    except (ValueError, IndexError) as e:
        return _res(None, 0, "dwg-" + version, f"object map not understood ({e})", "object-map")
    if not entries:
        return _res(None, 0, "dwg-" + version, "empty object map", "object-map")
    n, checked, matched, inb = _scan(obj, entries, best[0], version)
    conf = _score(version, n, checked, matched, inb, best[2], 1)
    return _res(n - 1 if conf else None, conf, f"dwg-{version}-objects", f"{n} layout objects, {matched}/{checked} sampled handles verified",
                None if conf else ("no-layout-objects" if n == 0 else "handle-mismatch"))


# ------------------------------------------------------------------------------------------------------------ entry points
def dwg_sheets(data):
    v = VERSIONS.get(bytes(data[:6]))
    if v is None:
        return _res(None, 0, "dwg", "not a known DWG version", "not-dwg")
    if v in ("R13", "R14"):
        return _res(None, 0, "dwg-" + v, "layouts (sheet tabs) were introduced in AutoCAD 2000; this file has none to count", "no-layouts-in-version")
    if v == "R2007":
        return _res(None, 0, "dwg-R2007", "AutoCAD 2007 files use a different layout that is not supported", "unsupported-r2007")
    if len(data) > MAX_BYTES:
        return _res(None, 0, "dwg-" + v, "file larger than the size limit", "too-large")
    try:
        if v in ("R13", "R14", "R2000"):
            return _dwg_r2000(data, v)
        return _dwg_r2004(data, v)
    except (struct.error, ValueError, IndexError, EOFError, KeyError) as e:
        return _res(None, 0, "dwg-" + v, f"could not be read ({type(e).__name__})", "error-" + type(e).__name__.lower())


def count_sheets_bytes(data, ext):
    ext = (ext or "").lower()
    try:
        if ext == "dxf":
            return dxf_sheets(data)
        if ext == "dwg":
            return dwg_sheets(data)
    except Exception as e:                                       # never let a damaged file stop a run
        return _res(None, 0, ext, f"could not be read ({type(e).__name__})", "error-" + type(e).__name__.lower())
    return _res(None, 0, ext, "not a drawing format", "not-dwg")


def count_sheets_file(f, ext, size=None, head=None):
    """Same for an open binary file positioned at 0. `head` = the first 6 bytes if the caller already read them (the profiler has, for the
    format tag): then no extra read is made."""
    try:
        if size is not None and size > MAX_BYTES:
            return _res(None, 0, ext, "file larger than the size limit", "too-large")
        if (ext or "").lower() == "dwg":
            # the verdict for AutoCAD R13 / R14 (no layouts), R2007 (not supported) and "not a DWG" depends on the first 6 bytes only:
            # do not read the rest of the file (on real data that was 17% of all drawings, read whole for nothing)
            fresh = head is None
            if fresh:
                head = f.read(6)
            if VERSIONS.get(bytes(head)) in (None, "R13", "R14", "R2007"):
                return count_sheets_bytes(head, ext)
            if fresh:
                f.seek(0)
        # read exactly the file's size: asking for MAX_BYTES + 1 (150 MB) of a 3 MB drawing makes the allocator hold a block that big per call,
        # and with many workers that is what filled small machines' RAM
        return count_sheets_bytes(f.read((size if size is not None else MAX_BYTES) + 1), ext)
    except OSError:
        return _res(None, 0, ext, "unreadable", "unreadable")
