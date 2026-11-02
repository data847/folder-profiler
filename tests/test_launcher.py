"""drive-package/launcher.py: fetches the program from GitHub into ./app, updates only when a newer version exists, keeps working from the copy it has when GitHub
cannot be reached, refuses unsafe downloads without touching the installed copy, passes --out and the build id on, and says plainly when nothing can be done.
A local server plays GitHub (the launcher's two URL overrides exist for this)."""
import http.server, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, threading, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "drive-package", "launcher.py")
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)

REPO = "data847/folder-profiler"
STUB_UI = "import os, sys\nprint('UI-STUB', sys.argv[1:], 'build=' + os.environ.get('FOLDER_PROFILER_BUILD', ''), 'cwd_is_pkg=' + str(os.path.samefile(os.getcwd(), os.environ['PKG'])))\n"
STUB_CLI = "import sys\nprint('CLI-STUB', sys.argv[1:])\n"
def make_zip(sha, files=None, extra=None, comment=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, t in (files or {"ui.py": STUB_UI, "file_profiler.py": STUB_CLI, "Dockerfile": "FROM x\n", "VERSION.txt": sha}).items():
            z.writestr(f"folder-profiler-{sha[:7]}/{n}", t)
        for zi, data in (extra or []):
            z.writestr(zi, data)
        if comment: z.comment = sha.encode()
    return buf.getvalue()

class Hub(http.server.BaseHTTPRequestHandler):
    sha = "a" * 40; api_ok = True; zip_ok = True; zip_bytes = None; hits = []
    def log_message(self, *a): pass
    def do_GET(self):
        Hub.hits.append(self.path)
        try:
            if self.path.startswith(f"/repos/{REPO}/commits/"):
                if not Hub.api_ok: self.send_response(403); self.end_headers(); return
                body = Hub.sha.encode()
            elif self.path.startswith(f"/{REPO}/zip/"):
                if not Hub.zip_ok: self.send_response(500); self.end_headers(); return
                body = Hub.zip_bytes or make_zip(self.path.rsplit("/", 1)[1] if len(self.path.rsplit("/", 1)[1]) == 40 else Hub.sha)
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except BrokenPipeError: pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hub); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

def new_pkg():
    d = tempfile.mkdtemp(); shutil.copy(LAUNCHER, d); return d
def launch(pkg, *args, env=None):
    e = dict(os.environ, FOLDER_PROFILER_API=BASE, FOLDER_PROFILER_ARCHIVE=BASE, PKG=pkg); e.pop("FOLDER_PROFILER_REF", None); e.update(env or {})
    p = subprocess.run([sys.executable, os.path.join(pkg, "launcher.py"), *args], capture_output=True, text=True, timeout=60, env=e, cwd=tempfile.gettempdir())
    return p.returncode, p.stdout, p.stderr
def zips(): return [h for h in Hub.hits if "/zip/" in h]
def built(pkg):
    try: return json.load(open(os.path.join(pkg, "app", "current", ".build.json")))["sha"]
    except OSError: return None
def listing(pkg): return sorted(os.listdir(os.path.join(pkg, "app"))) if os.path.isdir(os.path.join(pkg, "app")) else []

A, B = "a" * 40, "b" * 40
# ---- first run, same version again, a newer version
pkg = new_pkg(); Hub.hits.clear(); Hub.sha = A
rc, out, err = launch(pkg, "ui", "--port", "1234")
check("first run: installs from GitHub and runs the program", (rc, built(pkg), len(zips()), "UI-STUB" in out), (0, A, 1, True))
check("--out ./output next to the launcher is added, the build id and the package folder are passed on", ("'--port', '1234', '--out', " in out and os.path.join(pkg, "output") in out, "build=" + A[:12] in out, "cwd_is_pkg=True" in out), (True, True, True))
check("it says what it is doing", ("checking GitHub" in out, f"Installed version {A[:7]}" in out), (True, True))
Hub.hits.clear(); rc, out, err = launch(pkg, "ui")
check("same version again: nothing downloaded, runs from the copy", (rc, len(zips()), "up to date" in out, "UI-STUB" in out), (0, 0, True, True))
Hub.sha = B; Hub.hits.clear(); rc, out, err = launch(pkg, "cli", "D:\\data", "--quick")
check("newer version: downloaded, replaces the old one, no leftovers in app/", (rc, built(pkg), len(zips()), listing(pkg)), (0, B, 1, ["current"]))
check("cli mode runs file_profiler.py with the user's words, then --out", "CLI-STUB ['D:\\\\data', '--quick', '--out', " in out, True)
rc, out, err = launch(pkg, "ui", "--out", "X:\\mine")
check("an --out given by the user is kept (not added twice)", out.count("--out"), 1)
rc, out, err = launch(pkg, "ui", "--out=X:/mine")
check("--out=... form too", out.count("--out"), 1)

# ---- GitHub unreachable
Hub.api_ok = False; Hub.hits.clear(); rc, out, err = launch(pkg, "ui")
check("API down, copy present: uses it and says so (no download attempted)", (rc, len(zips()), "Could not reach GitHub" in out, "UI-STUB" in out, built(pkg)), (0, 0, True, True, B))
Hub.zip_ok = False; shutil.rmtree(os.path.join(pkg, "app")); rc, out, err = launch(pkg, "ui")
check("everything down, no copy: stops with a plain message and exit code 2", (rc, "could not be downloaded" in out, "internet access to github.com" in out, "UI-STUB" in out, listing(pkg)), (2, True, True, False, []))
Hub.zip_ok = True; Hub.hits.clear(); Hub.sha = A; rc, out, err = launch(pkg, "ui")
check("API down but the archive host works: still installs (the commit id comes from the zip's comment)", (rc, built(pkg)), (0, A))
Hub.api_ok = True

# ---- unsafe downloads leave the installed copy alone
def hostile(label, zbytes, why):
    p = new_pkg(); Hub.sha = A; Hub.zip_bytes = None; launch(p, "ui"); Hub.sha = B; Hub.zip_bytes = zbytes
    rc, out, err = launch(p, "ui"); Hub.zip_bytes = None
    check(f"{label}: refused, installed copy kept and used", (rc, built(p), "Could not update" in out, why in out, "UI-STUB" in out, listing(p), os.path.exists(os.path.join(p, "..", "evil.txt"))), (0, A, True, True, True, ["current"], False))
    return p
hostile("path going up (../)", make_zip(B, extra=[(f"folder-profiler-{B[:7]}/../evil.txt", "x")]), "unsafe name")
hostile("absolute path inside the one folder", make_zip(B, extra=[(f"folder-profiler-{B[:7]}//tmp/evil.txt", "x")]), "unsafe name")
hostile("backslash path", make_zip(B, extra=[(f"folder-profiler-{B[:7]}/sub\\..\\..\\evil.txt", "x")]), "unsafe name")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr(f"folder-profiler-{B[:7]}/ui.py", STUB_UI); z.writestr(f"folder-profiler-{B[:7]}/file_profiler.py", STUB_CLI)
    zi = zipfile.ZipInfo(f"folder-profiler-{B[:7]}/link"); zi.external_attr = (0o120777 << 16); z.writestr(zi, "/etc/passwd")
    z.comment = B.encode()
hostile("a symbolic link", buf.getvalue(), "a link in the download")
hostile("no ui.py", make_zip(B, files={"file_profiler.py": STUB_CLI}), "has no ui.py")
two = io.BytesIO()
with zipfile.ZipFile(two, "w") as z:
    z.writestr("one/ui.py", STUB_UI); z.writestr("two/file_profiler.py", STUB_CLI)
hostile("two top folders", two.getvalue(), "unexpected layout")
hostile("not a zip at all", b"<html>captive portal login</html>", "Could not update")

# ---- pinning, build id, prepare modes
pkg = new_pkg(); Hub.hits.clear(); Hub.sha = A
open(os.path.join(pkg, "ref.txt"), "w").write("v1.9\n"); launch(pkg, "prepare")
check("ref.txt pins the version asked of GitHub", [h for h in Hub.hits if "/commits/" in h], [f"/repos/{REPO}/commits/v1.9"])
Hub.hits.clear(); open(os.path.join(pkg, "ref.txt"), "w").write("../../etc/passwd\n"); launch(pkg, "prepare")
check("a nonsense ref.txt falls back to main", [h for h in Hub.hits if "/commits/" in h], [f"/repos/{REPO}/commits/main"])
Hub.hits.clear(); launch(pkg, "prepare", env={"FOLDER_PROFILER_REF": "release-2"})
check("FOLDER_PROFILER_REF overrides ref.txt", [h for h in Hub.hits if "/commits/" in h], [f"/repos/{REPO}/commits/release-2"])
os.remove(os.path.join(pkg, "ref.txt"))
rc, out, err = launch(pkg, "prepare")
check("prepare prints only the program's folder on stdout", (rc, out.strip() == os.path.join(pkg, "app", "current"), "checking GitHub" in err), (0, True, True))
rc, out, err = launch(pkg, "build-id", env={"FOLDER_PROFILER_API": "http://127.0.0.1:1"})
check("build-id is the installed commit (12 chars), offline", (rc, out.strip()), (0, built(pkg)[:12]))
rc, out, err = launch(new_pkg(), "build-id")
check("build-id with nothing installed", (rc, out.strip()), (0, "none"))
envf = os.path.join(tempfile.mkdtemp(), "e.bat"); rc, out, err = launch(pkg, "prepare-env", envf)
raw = open(envf, "rb").read().decode()
check("prepare-env writes CRLF set-lines for a Windows script", (rc, raw == f'set "FP_CODE={os.path.join(pkg, "app", "current")}"\r\nset "FP_BUILD={built(pkg)[:12]}"\r\n'), (0, True))
rc, out, err = launch(pkg, "frobnicate")
check("unknown mode is refused", (rc != 0, "unknown mode" in err), (True, True))

# ---- in-process: a swap that cannot happen (a copy of the program is open on Windows) and size limits
spec = importlib.util.spec_from_file_location("launcher_under_test", LAUNCHER); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
pkg = new_pkg(); L.HERE = pkg; L.APP = os.path.join(pkg, "app"); L.CODE = os.path.join(L.APP, "current"); L.API = L.ARCHIVE = BASE
Hub.sha = A; Hub.zip_bytes = None; L.prepare()
real_rename = os.rename
def locked(src, dst):
    if src == L.CODE: raise PermissionError("in use by another process")
    return real_rename(src, dst)
os.rename = locked; Hub.sha = B
try:
    got = L.prepare()
finally:
    os.rename = real_rename
check("swap refused by the OS: keeps and uses the running copy, leaves no debris", (got == L.CODE, built(pkg), listing(pkg)), (True, A, ["current"]))
real_rename2 = os.rename; n = [0]
def half(src, dst):
    if dst == L.CODE and ".new" in os.path.basename(src) or (dst == L.CODE and os.path.basename(src).startswith("new.")): raise OSError("disk error")
    return real_rename2(src, dst)
os.rename = half
try: got = L.prepare()
finally: os.rename = real_rename2
check("second rename failing puts the old copy back", (got == L.CODE, built(pkg), listing(pkg)), (True, A, ["current"]))
L.MAX_FILES = 2
d = tempfile.mkdtemp()
try: L.unpack(make_zip(B), d); res = "accepted"
except ValueError as e: res = "refused"
check("file-count limit", res, "refused")
srv.shutdown()
sys.exit(1 if any(bad) else 0)
