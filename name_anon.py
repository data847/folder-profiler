"""Optional anonymisation of file and folder names in the profiler's OUTPUT (never in the files themselves, never anywhere else).

One NameMap per run. Every distinct name gets a short code the first time it is seen and the same code every time after, so the
structure of the result is kept: same folder -> same code, same file name in two places -> same code, a reference to a file in
Links.csv -> the code of that file. File extensions are kept (they are the point of the profile); only the part before the extension
is replaced.

Codes are the SAME on every run on one computer: a code is a keyed hash of the name (HMAC-SHA256 with a random secret key made once and kept in
the owner's own key file, never in a profile), so Step 1 and Step 2, or a run today and one next month, give the same name the same code and the
two results line up by code. Without the key nobody can recompute or confirm a code from a guessed name. Format: P-<id>-<hex> / D-<id>-<hex> /
F-<id>-<hex>, <id> being 4 hex characters derived from the key. If no key can be kept (no writable place), the codes fall back to a random run id and a
counter (P-a1b2-0003): unique within the run only, and Run_info says so (name_codes).

The code <-> name table is written to a SEPARATE zip (name_map_*.zip) and is never put inside the profile zip: whoever receives
the profile cannot reverse it, and the owner keeps the map. Cost: one dictionary lookup per path part when the option is on, nothing
at all when it is off.
"""
import csv
import hashlib
import hmac
import io
import os
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


KEY_FILE_ENV = "PROFILER_NAME_KEY_FILE"
HEX_WIDTH = {"project": 6, "folder": 8, "file": 10}        # hex characters of a keyed code; a clash inside one run lengthens that code by 2


def key_file_candidates(out_dir=None):
    """Where the secret key may live, best first: the file named by PROFILER_NAME_KEY_FILE; else the user's own folder (native) or the output folder (Docker, where the
    home folder disappears with the container); the output folder is also the last resort."""
    env = os.environ.get(KEY_FILE_ENV)
    if env:
        return [env]
    home = os.path.join(os.path.expanduser("~"), ".folder-profiler", "name_key")
    out = os.path.join(out_dir, ".name_key") if out_dir else None
    order = [out, home] if os.environ.get("PROFILER_IN_DOCKER") else [home, out]
    return [x for x in order if x]


def load_key(out_dir=None):
    """-> (key bytes, where it is kept), making and saving a new random key the first time; (None, "") when no key can be kept or read."""
    for path in key_file_candidates(out_dir):
        try:
            with open(path, "r", encoding="ascii") as f:
                hexkey = f.read().strip()
            if len(hexkey) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", hexkey):
                return bytes.fromhex(hexkey if len(hexkey) % 2 == 0 else hexkey[:-1]), path
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            continue
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(secrets.token_hex(32) + "\n")
        except FileExistsError:                           # another run made it a moment ago: read theirs
            pass
        except OSError:
            continue
        try:
            with open(path, "r", encoding="ascii") as f:
                return bytes.fromhex(f.read().strip()), path
        except (OSError, ValueError):
            continue
    return None, ""


class NameMap:
    def __init__(self, run_id=None, key=None, key_path=""):
        self._key = key
        self.key_path = key_path
        self.stable = key is not None                       # True: the same name has the same code on every run with this key
        self.run = run_id or (hmac.new(key, b"run-id", hashlib.sha256).hexdigest()[:4] if key else secrets.token_hex(2))
        self._used = {"project": set(), "folder": set(), "file": set()}
        self._t = {"project": {}, "folder": {}, "file": {}}
        self._prefix = {"project": "P", "folder": "D", "file": "F"}
        self._width = {"project": 4, "folder": 6, "file": 6}
        self._dirs = {}                      # folder part of a path -> its renamed form (thousands of files share one folder)

    def _code(self, kind, name):
        table = self._t[kind]
        key = name.casefold()                # Windows names are case-insensitive, and references inside CAD files differ in case from the file
        hit = table.get(key)
        if hit is None:
            if self._key is None:
                hit = (f"{self._prefix[kind]}-{self.run}-{len(table) + 1:0{self._width[kind]}d}", name)
            else:
                digest = hmac.new(self._key, f"{kind}\0{key}".encode("utf-8"), hashlib.sha256).hexdigest()
                n, used = HEX_WIDTH[kind], self._used[kind]
                while digest[:n] in used and n < len(digest):
                    n += 2                                  # two different names, one code: the later one gets a longer code (about 1 in 10^12 files)
                used.add(digest[:n])
                hit = (f"{self._prefix[kind]}-{self.run}-{digest[:n]}", name)
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
                  "  project   a project folder (P-xxxx-...)\r\n"
                  "  folder    a folder inside a project (D-xxxx-...)\r\n"
                  "  file      a file name WITHOUT its extension (F-xxxx-...); the extension is unchanged in the profile,\r\n"
                  "            so 'F-a1b2-000007.dwg' is the file whose name before '.dwg' is the 'original' of that code.\r\n"
                  "Matching ignores upper / lower case: two names that differ only in case share one code, and the map shows the spelling\r\n"
                  "seen first. The same name in several folders has one code. Only the output is renamed - your files and folders\r\n"
                  "were never touched. Open Name_Map.csv with Notepad or import it into Excel (it can have more rows than Excel holds).\r\n")
        if self.stable:
            readme += ("\r\nCODES ARE THE SAME ON EVERY RUN: Step 1 and Step 2 (and any later run on this computer) give the same name the same code, so their\r\n"
                       "results line up by code. That comes from a secret key kept in the file " + self.key_path + "\r\n"
                       "Keep that file and this zip private; whoever has the key can check a guessed name against a code. A copy of the key is in Name_Key.txt here:\r\n"
                       "put it back as that file on another computer to get the same codes there. Delete the key file to start new, unrelated codes.\r\n")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Name_Map.csv", "﻿" + buf.getvalue())
            z.writestr("README.txt", readme)
            if self.stable:
                z.writestr("Name_Key.txt", self._key.hex() + "\r\n")
        return path
