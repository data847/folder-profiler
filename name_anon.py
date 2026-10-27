"""Optional anonymisation of file and folder names in the profiler's OUTPUT (never in the files themselves, never anywhere else).

One NameMap per run. Every distinct name gets a short code the first time it is seen and the same code every time after, so the
structure of the result is kept: same folder -> same code, same file name in two places -> same code, a reference to a file in
Links.csv -> the code of that file. File extensions are kept (they are the point of the profile); only the part before the extension
is replaced. Codes carry a 4-hex run id (P-a1b2-0003) so the results of two runs can be compiled together without clashing.

The code <-> name table is written to a SEPARATE zip (name_map_*.zip) and is never put inside the profile zip: whoever receives
the profile cannot reverse it, and the owner keeps the map. Cost: one dictionary lookup per path part when the option is on, nothing
at all when it is off.
"""
import csv
import io
import re
import secrets
import zipfile

ROOT_NAMES = {"(root)", ""}                  # placeholders the profiler itself writes: not names from the data
_VERSIONED = re.compile(r"^(.*)\.(prt|asm|drw|frm|lay|mfg|sec|dgm)\.(\d+)$", re.I)      # Creo: part.prt.7


def split_name(name):
    """-> (stem, extension with its dot). 'a.b.dwg' -> ('a.b', '.dwg'); 'part.prt.7' -> ('part', '.prt.7'); 'notes' -> ('notes', '')."""
    m = _VERSIONED.match(name)
    if m:
        return m.group(1), "." + m.group(2) + "." + m.group(3)
    i = name.rfind(".")
    if i <= 0:
        return name, ""
    ext = name[i:]
    if len(ext) > 12 or " " in ext:          # a dot inside a name, not an extension
        return name, ""
    return name[:i], ext


class NameMap:
    def __init__(self, run_id=None):
        self.run = run_id or secrets.token_hex(2)
        self._t = {"project": {}, "folder": {}, "file": {}}
        self._prefix = {"project": "P", "folder": "D", "file": "F"}
        self._width = {"project": 4, "folder": 6, "file": 6}
        self._dirs = {}                      # folder part of a path -> its renamed form (thousands of files share one folder)

    def _code(self, kind, name):
        table = self._t[kind]
        key = name.casefold()                # Windows names are case-insensitive, and references inside CAD files differ in case from the file
        hit = table.get(key)
        if hit is None:
            hit = (f"{self._prefix[kind]}-{self.run}-{len(table) + 1:0{self._width[kind]}d}", name)
            table[key] = hit
        return hit[0]

    # ---- single names
    def project(self, name):
        return name if name in ROOT_NAMES else self._code("project", name)

    def root(self, label):
        return f"ROOT-{self.run}"

    def folder(self, name):
        return name if name in ROOT_NAMES else self._code("folder", name)

    def file(self, name):
        """A file name (or a referenced name from Links.csv): the stem becomes a code, the extension stays."""
        if not name:
            return name
        stem, ext = split_name(name)
        return self._code("file", stem) + ext

    # ---- paths ('/' separated, as the profiler writes them)
    def folder_path(self, rel):
        return "/".join(self.folder(p) for p in rel.split("/")) if rel else rel

    def file_path(self, rel):
        if not rel:
            return rel
        i = rel.rfind("/")
        if i < 0:
            return self.file(rel)
        head = rel[:i]
        mapped = self._dirs.get(head)
        if mapped is None:
            mapped = self._dirs[head] = "/".join(self.folder(p) for p in head.split("/"))
        return mapped + "/" + self.file(rel[i + 1:])

    # ---- the map itself
    def rows(self):
        for kind in ("project", "folder", "file"):
            for code, orig in self._t[kind].values():
                yield (kind if kind != "file" else "file (name without extension)", orig, code)

    def counts(self):
        return {k: len(v) for k, v in self._t.items()}

    def write_zip(self, path, label):
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["kind", "original", "anonymised"])
        w.writerow(["root folder", label, self.root(label)])
        w.writerows(self.rows())
        readme = ("NAME MAP - KEEP THIS PRIVATE. Do not send it with the profile.\r\n\r\n"
                  "Name_Map.csv turns the codes in the profile (Files.csv, Dirs.csv, Links.csv, Projects.csv ...) back into the real names.\r\n"
                  "  project   a project folder (P-xxxx-nnnn)\r\n"
                  "  folder    a folder inside a project (D-xxxx-nnnnnn)\r\n"
                  "  file      a file name WITHOUT its extension (F-xxxx-nnnnnn); the extension is unchanged in the profile,\r\n"
                  "            so 'F-a1b2-000007.dwg' is the file whose name before '.dwg' is the 'original' of that code.\r\n"
                  "Matching ignores upper / lower case: two names that differ only in case share one code, and the map shows the spelling\r\n"
                  "seen first. The same name in several folders has one code. Only the output is renamed - your files and folders\r\n"
                  "were never touched. Open Name_Map.csv with Notepad or import it into Excel (it can have more rows than Excel holds).\r\n")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Name_Map.csv", "﻿" + buf.getvalue())
            z.writestr("README.txt", readme)
        return path
