"""Folder Profiler starter. Fetches the program from its public GitHub repository, then runs it. Open this file in Notepad to read everything it does.

    launcher.py ui        the web page (what run-ui.bat / run-ui.command start)
    launcher.py cli ...   the command-line profiler (what run.bat / run.command start); extra words are passed on
    launcher.py prepare   only fetch / update the program and print the folder it is in (for the Docker scripts)
    launcher.py build-id  print which version is installed, without touching the network
    launcher.py prepare-env FILE   like prepare, but writes "set FP_CODE=..." / "set FP_BUILD=..." lines into FILE for a Windows script to call

What it does, in order:
  1. Asks GitHub which version is the latest (one small request: github.com/data847/folder-profiler). Nothing about you or your files is sent.
  2. If that is not the version already in the "app" folder next to this file, downloads it (about 1 MB) and replaces "app" with it.
  3. If GitHub cannot be reached, it uses the version already in "app" and says so. The first run needs internet; later runs do not.
  4. Runs the program from "app". Results are written to the "output" folder next to this file, as before.
To stay on one exact version, put its tag or commit on the first line of a file named ref.txt next to this one (default: main).
Delete the "app" folder at any time to start clean. Only the Python standard library is used; no git, no pip.
"""
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

REPO = "data847/folder-profiler"
API = os.environ.get("FOLDER_PROFILER_API") or "https://api.github.com"            # the two overrides exist for the tests only
ARCHIVE = os.environ.get("FOLDER_PROFILER_ARCHIVE") or "https://codeload.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app")
CODE = os.path.join(APP, "current")
STAMP = ".build.json"                       # inside the installed program: which commit it is
MAX_FILES, MAX_BYTES = 5000, 300 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_quiet = False


def say(msg):
    print(msg, file=sys.stderr if _quiet else sys.stdout, flush=True)


def wanted_ref():
    r = os.environ.get("FOLDER_PROFILER_REF", "").strip()
    if not r:
        try:
            with open(os.path.join(HERE, "ref.txt"), encoding="utf-8") as f:
                r = (f.readline() or "").strip()
        except OSError:
            r = ""
    return r if re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", r or "") and ".." not in r else "main"


def get(url, headers=None, timeout=30):
    """-> bytes. urllib first; if the failure is about certificates (a python.org Python on a Mac without its certificates installed), curl, which uses the system's."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": "folder-profiler-launcher"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        why = getattr(e, "reason", e)
        if isinstance(why, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
            curl = shutil.which("curl")
            if curl:
                cmd = [curl, "-fsSL", "--max-time", str(timeout), "-A", "folder-profiler-launcher"]
                for k, v in (headers or {}).items():
                    cmd += ["-H", f"{k}: {v}"]
                p = subprocess.run(cmd + [url], capture_output=True)
                if p.returncode == 0:
                    return p.stdout
        raise


def latest_sha(ref):
    body = get(f"{API}/repos/{REPO}/commits/{ref}", {"Accept": "application/vnd.github.sha"}, 20).decode("ascii", "replace").strip()
    if not SHA_RE.match(body):
        raise ValueError("GitHub did not answer with a version id")
    return body


def installed():
    try:
        with open(os.path.join(CODE, STAMP), encoding="utf-8") as f:
            d = json.load(f)
        if os.path.isfile(os.path.join(CODE, "ui.py")) and os.path.isfile(os.path.join(CODE, "file_profiler.py")):
            return d
    except (OSError, ValueError):
        pass
    return None


def unpack(data, dest):
    """Extract a GitHub zip (one top folder) into dest. Refuses anything that could write outside dest, links, or an absurd size. -> commit id from the zip's comment."""
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = [i for i in z.infolist() if not i.filename.endswith("/")]
    if not 0 < len(infos) <= MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
        raise ValueError("the download is not the size of this program")
    tops = {i.filename.split("/", 1)[0] for i in z.infolist()}
    if len(tops) != 1:
        raise ValueError("unexpected layout in the download")
    base = os.path.realpath(dest)
    for i in infos:
        name = i.filename
        rel = name.split("/", 1)[1] if "/" in name else ""
        if not rel or name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
            raise ValueError(f"unsafe name in the download: {name!r}")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"a link in the download: {name!r}")
        target = os.path.realpath(os.path.join(base, rel))
        if os.path.commonpath([base, target]) != base:
            raise ValueError(f"unsafe name in the download: {name!r}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(i) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    for must in ("ui.py", "file_profiler.py"):
        if not os.path.isfile(os.path.join(base, must)):
            raise ValueError(f"the download has no {must}")
    return z.comment.decode("ascii", "replace").strip()


def install(data, sha_hint, ref):
    os.makedirs(APP, exist_ok=True)
    new = os.path.join(APP, f"new.{os.getpid()}")
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    try:
        sha = unpack(data, new)
        sha = sha if SHA_RE.match(sha) else (sha_hint or "")
        with open(os.path.join(new, STAMP), "w", encoding="utf-8") as f:
            json.dump({"sha": sha, "ref": ref, "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        old = None
        if os.path.isdir(CODE):
            old = os.path.join(APP, f"old.{os.getpid()}")
            os.rename(CODE, old)                          # fails while a copy of the program is open on Windows: the caller keeps the current one
        try:
            os.rename(new, CODE)
        except OSError:
            if old:
                os.rename(old, CODE)
            raise
        if old:
            shutil.rmtree(old, ignore_errors=True)
        return sha
    finally:
        shutil.rmtree(new, ignore_errors=True)


def tidy():
    try:
        for n in os.listdir(APP):
            p = os.path.join(APP, n)
            if (n.startswith("old.") or n.startswith("new.")) and time.time() - os.path.getmtime(p) > 3600:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def prepare():
    """-> folder of the program to run; downloads or updates it when a newer version exists, keeps what is there when GitHub cannot be reached."""
    ref = wanted_ref()
    have = installed()
    say(f"Folder Profiler - checking GitHub for the latest version ({REPO}, {ref}) ...")
    sha, problem = "", ""
    try:
        sha = latest_sha(ref)
    except Exception as e:
        problem = str(getattr(e, "reason", e)) or e.__class__.__name__
    if have and sha and have.get("sha") == sha and have.get("ref") == ref:
        say(f"Version {sha[:7]} is up to date.")
        return CODE
    if have and not sha:
        say(f"Could not reach GitHub ({problem}). Using the version already here: {have.get('sha', '')[:7] or 'unknown'} from {have.get('fetched_utc', 'an earlier day')}.")
        return CODE
    try:
        say(f"Downloading version {sha[:7] or ref} ...")
        data = get(f"{ARCHIVE}/{REPO}/zip/{sha or ref}", None, 120)
        got = install(data, sha, ref)
        say(f"Installed version {(got or sha)[:7]} ({len(data) // 1024} KB).")
        tidy()
        return CODE
    except Exception as e:
        why = str(getattr(e, "reason", e)) or e.__class__.__name__
        if have and installed():
            say(f"Could not update ({why}). Using the version already here: {have.get('sha', '')[:7] or 'unknown'}.")
            return CODE
        say("")
        say("The program could not be downloaded, and there is no copy here yet.")
        say(f"  Reason: {why}")
        say("  This first start needs internet access to github.com (and api.github.com). Check the connection, a VPN or proxy, and try again.")
        say("  If your network blocks GitHub, ask the person who sent you this package for a full copy.")
        sys.exit(2)


def run(script, args):
    """Run one of the program's scripts in this process, with the program's folder importable. Results go to ./output next to this file unless --out is given."""
    code = prepare()
    path = os.path.join(code, script)
    if "--out" not in args and not any(a.startswith("--out=") for a in args):
        args = list(args) + ["--out", os.path.join(HERE, "output")]
    os.environ["FOLDER_PROFILER_BUILD"] = (installed() or {}).get("sha", "")[:12]
    os.chdir(HERE)
    sys.path.insert(0, code)
    sys.argv = [path] + list(args)
    import runpy
    runpy.run_path(path, run_name="__main__")


def main(argv):
    global _quiet
    mode = argv[0] if argv else "ui"
    rest = argv[1:]
    if mode == "build-id":
        _quiet = True
        have = installed()
        print((have or {}).get("sha", "")[:12] or "none")
    elif mode == "prepare":
        _quiet = True                                    # stdout carries only the folder, for the scripts that read it
        print(prepare())
    elif mode == "prepare-env" and rest:
        _quiet = True
        code = prepare()
        with open(rest[0], "w", encoding="utf-8", newline="") as f:
            f.write(f'set "FP_CODE={code}"\r\nset "FP_BUILD={(installed() or {}).get("sha", "")[:12] or "unknown"}"\r\n')
    elif mode == "ui":
        run("ui.py", rest)
    elif mode in ("cli", "profile"):
        run("file_profiler.py", rest)
    else:
        sys.exit(f"unknown mode {mode!r}: use ui, cli, prepare or build-id")


if __name__ == "__main__":
    main(sys.argv[1:])
