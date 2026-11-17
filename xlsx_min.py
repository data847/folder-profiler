"""Minimal streaming .xlsx writer. Standard library only (zipfile + XML text).

Why not openpyxl: the profiler runs on the supplier's machine with nothing installed. This writes just what the scan workbook needs:
inline strings, numbers, formulas with a cached value (so the figures show even in a viewer that does not recalculate), a few cell styles,
column widths, frozen panes and merged cells. Rows are written as they are produced, so a sheet of a million rows never sits in memory.

    wb = Workbook()
    wb.add_sheet("Projects", rows_iterable, widths=[10, 30], freeze=(3, 2), merges=["A2:E2"])
    wb.save("out.xlsx")

A row is a list; each cell is a plain value (str / int / float / None), a (value, style) pair, or F(formula, cached_value, style).
Styles are the names in STYLES below.
"""
import re
import zipfile

MAX_ROWS = 1_048_576
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


class F:
    """A formula cell: F("SUM(A1:A3)", 6, "int")."""
    __slots__ = ("f", "v", "style")

    def __init__(self, f, v=None, style=None):
        self.f, self.v, self.style = f, v, style


# name -> (font, fill, alignment, numFmt) ; ids are assigned in the order of this table
STYLES = {
    "default": ("n", None, None, 0),
    "title": ("b13", None, "left", 0),
    "group": ("b10", "dce3f0", "center-wrap", 0),
    "header": ("b10w", "1b2a4a", "center-wrap", 0),
    "text": ("n10", None, "left", 0),
    "int": ("n10", None, "right", 3),
    "pct": ("n10", None, "right", 10),
    "wrap": ("n11", None, "top-wrap", 0),
    "bold": ("b11", None, "top-left", 0),
    "section": ("b11", "dce3f0", "top-left", 0),
    "total_text": ("b11", "e6f2e6", "left", 0),
    "total_int": ("b10", "e6f2e6", "right", 3),
    "note": ("n9", None, "top-wrap", 0),
    "small": ("n10", None, "center", 0),
}
_FONTS = {"n": (11, False, None), "n9": (9, False, None), "n10": (10, False, None), "n11": (11, False, None), "b10": (10, True, None),
          "b11": (11, True, None), "b13": (13, True, None), "b14": (14, True, None), "b10w": (10, True, "FFFFFFFF")}
_ALIGN = {"left": ('left', 'center', 0), "right": ('right', 'center', 0), "center": ('center', 'center', 0),
          "center-wrap": ('center', 'center', 1), "top-wrap": ('left', 'top', 1), "top-left": ('left', 'top', 0)}
STYLES["title14"] = ("b14", None, "left", 0)


def _col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_letter(n):
    return _col(n)


def _esc(s):
    return (_ILLEGAL.sub("", s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _styles_xml():
    fonts, fills, xfs = [], ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>'], []
    font_id, fill_id = {}, {}
    for key, (sz, bold, color) in _FONTS.items():
        font_id[key] = len(fonts)
        fonts.append(f'<font>{"<b/>" if bold else ""}<sz val="{sz}"/>'
                     + (f'<color rgb="{color}"/>' if color else "") + '<name val="Calibri"/></font>')
    for name, (font, fill, align, numfmt) in STYLES.items():
        fid = 0
        if fill:
            if fill not in fill_id:
                fill_id[fill] = len(fills)
                fills.append(f'<fill><patternFill patternType="solid"><fgColor rgb="FF{fill.upper()}"/></patternFill></fill>')
            fid = fill_id[fill]
        h, v, wrap = _ALIGN[align] if align else ("general", "bottom", 0)
        wrap_attr = ' wrapText="1"' if wrap else ""
        xfs.append(f'<xf numFmtId="{numfmt}" fontId="{font_id[font]}" fillId="{fid}" borderId="0" xfId="0" applyNumberFormat="1" '
                   f'applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="{h}" vertical="{v}"{wrap_attr}/></xf>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts><fills count="{len(fills)}">{"".join(fills)}</fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


_STYLE_ID = {name: i for i, name in enumerate(STYLES)}


def _cell(ref, c):
    style = None
    if isinstance(c, F):
        v, style = c.v, c.style
        s = f' s="{_STYLE_ID[style]}"' if style else ""
        if isinstance(v, str):
            return f'<c r="{ref}"{s} t="str"><f>{_esc(c.f)}</f><v>{_esc(v)}</v></c>'
        return f'<c r="{ref}"{s}><f>{_esc(c.f)}</f>' + (f"<v>{v}</v>" if v is not None else "") + "</c>"
    if isinstance(c, tuple):
        c, style = c
    s = f' s="{_STYLE_ID[style]}"' if style else ""
    if c is None or c == "":
        return f'<c r="{ref}"{s}/>' if style else ""
    if isinstance(c, bool):
        return f'<c r="{ref}"{s} t="b"><v>{int(c)}</v></c>'
    if isinstance(c, (int, float)):
        return f'<c r="{ref}"{s}><v>{c}</v></c>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{_esc(str(c))}</t></is></c>'


class Workbook:
    def __init__(self):
        self.sheets = []           # (name, rows, widths, freeze, merges, heights)

    def add_sheet(self, name, rows, widths=None, freeze=None, merges=None, row_heights=None):
        name = re.sub(r"[\[\]:*?/\\]", "-", name)[:31]
        self.sheets.append((name, rows, widths or [], freeze, merges or [], row_heights or {}))

    def save(self, path):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                       '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                       + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                 for i in range(1, len(self.sheets) + 1)) + "</Types>")
            z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                       + "".join(f'<sheet name="{_esc(s[0])}" sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(self.sheets, 1))
                       + '</sheets><calcPr fullCalcOnLoad="1"/></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                                 for i in range(1, len(self.sheets) + 1))
                       + f'<Relationship Id="rId{len(self.sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
            z.writestr("xl/styles.xml", _styles_xml())
            for i, (name, rows, widths, freeze, merges, heights) in enumerate(self.sheets, 1):
                with z.open(f"xl/worksheets/sheet{i}.xml", "w", force_zip64=True) as f:
                    self._write_sheet(f, rows, widths, freeze, merges, heights)

    @staticmethod
    def _write_sheet(f, rows, widths, freeze, merges, heights):
        w = lambda s: f.write(s.encode("utf-8"))
        w('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
        w("<sheetViews><sheetView workbookViewId=\"0\">")
        if freeze:
            fr, fc = freeze
            tl = f"{_col(fc + 1)}{fr + 1}"
            attrs = (f' xSplit="{fc}"' if fc else "") + (f' ySplit="{fr}"' if fr else "")
            pane = "bottomRight" if fr and fc else "bottomLeft" if fr else "topRight"
            w(f'<pane{attrs} topLeftCell="{tl}" activePane="{pane}" state="frozen"/>')
        w("</sheetView></sheetViews><sheetFormatPr defaultRowHeight=\"15\"/>")
        if widths:
            w("<cols>" + "".join(f'<col min="{i}" max="{i}" width="{x}" customWidth="1"/>' for i, x in enumerate(widths, 1)) + "</cols>")
        w("<sheetData>")
        n = 0
        for row in rows:
            n += 1
            if n > MAX_ROWS:
                raise ValueError("sheet has more rows than Excel allows; the caller must split it")
            h = heights.get(n)
            cells = "".join(_cell(f"{_col(j)}{n}", c) for j, c in enumerate(row, 1))
            w(f'<row r="{n}"' + (f' ht="{h}" customHeight="1"' if h else "") + f">{cells}</row>")
        w("</sheetData>")
        if merges:
            w(f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in merges) + "</mergeCells>")
        w("</worksheet>")
