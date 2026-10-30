"""Logging gaps: failures that used to vanish now leave a reason (Read_Problems.csv, Setup_Warnings.csv, Run_log.csv, error_log.txt),
for the folder profiler, the UI and the Fusion cloud profiler.   Run: python tests/test_logging.py"""
import csv, http.server, io, json, os, shutil, sys, tempfile, threading, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import file_profiler as fp
import fusion_cloud_profiler as fc
import diag

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)
rd = lambda z, n: list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
info = lambda z: {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(body)
def lock(path):
    os.chmod(path, 0)
def unlock_all(root):
    for d, ds, fs in os.walk(root):
        for n in ds + fs:
            try: os.chmod(os.path.join(d, n), 0o755)
            except OSError: pass
can_chmod = hasattr(os, "geteuid") and os.geteuid() != 0 and sys.platform != "win32"
quiet = dict(log=lambda *_: None)

# ---------------------------------------------------------------- diag: notes are per thread and are forgotten once taken
diag.note("header", "PermissionError")
check("take() returns the note once, then nothing", (diag.take(), diag.take()), ("header: PermissionError", ""))
seen = []
threading.Thread(target=lambda: seen.append(diag.take())).start()
diag.note("references", "ValueError"); import time; time.sleep(0.1)
check("a note made in one thread is not seen by another", (seen, diag.take()), ([""], "references: ValueError"))

# ---------------------------------------------------------------- folder profiler
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data")
    put(src, "P/good.sldprt", b"fine")
    put(src, "P/broken.sldasm", b"this is not a solidworks container at all, " * 4)     # reader cannot understand it -> reason, not silence
    put(src, "P/big.step", b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    if can_chmod:
        put(src, "P/secret.dwg", b"AC1032" + b"\0" * 50); lock(os.path.join(src, "P", "secret.dwg"))
    z, *_ = fp.profile(src, os.path.join(t, "o"), workers=2, system_info=False, tier=2, **quiet)
    names = zipfile.ZipFile(z).namelist()
    check("Read_Problems.csv exists when a file could not be fully read", "Read_Problems.csv" in names, True)
    rp = rd(z, "Read_Problems.csv")
    files = {r["file_no"]: r for r in rd(z, "Files.csv")}
    byname = {files[r["file_no"]]["rel_path"].split("/")[-1]: r for r in rp}
    check("damaged assembly: stage references with a reason", (byname["broken.sldasm"]["stage"], byname["broken.sldasm"]["reason"] != ""), ("references", True))
    if can_chmod:
        check("unreadable drawing: stage header, reason is the OS error type", (byname["secret.dwg"]["stage"], byname["secret.dwg"]["reason"]), ("header", "PermissionError"))
        ur = rd(z, "Unreadable.csv")
        check("Unreadable.csv now carries the real reason too", [u["reason"] for u in ur if u["path"].endswith("secret.dwg")], ["PermissionError"])
    check("Run_info counts the problems", int(info(z)["read_problems_listed"]) >= 1, True)
    log = rd(z, "Run_log.csv")
    check("Run_log has a WARN naming the problems", any(r["level"] == "WARN" and "not fully read" in r["message"] for r in log), True)
    check("good files are not listed", "good.sldprt" in byname, False)
    unlock_all(src)

    # no problems -> no file
    src2 = os.path.join(t, "clean"); put(src2, "P/a.sldprt", b"ok")
    z2, *_ = fp.profile(src2, os.path.join(t, "o2"), workers=1, system_info=False, tier=1, **quiet)
    check("a clean run has no Read_Problems.csv and counts 0", ("Read_Problems.csv" in zipfile.ZipFile(z2).namelist(), info(z2)["read_problems_listed"]), (False, "0"))

    # anonymised names: the problem list carries codes, not project names
    z3, *_ = fp.profile(src, os.path.join(t, "o3"), workers=2, system_info=False, tier=2, anonymize_names=True, name_map=False, **quiet)
    check("name anonymisation also covers Read_Problems.csv", all(r["project"].startswith("P-") for r in rd(z3, "Read_Problems.csv")), True)

# ---------------------------------------------------------------- unreadable folders: list cap is reported, setup problems are reported
if can_chmod:
    with tempfile.TemporaryDirectory() as t:
        src = os.path.join(t, "data")
        for i in range(3):
            put(src, f"P/locked{i}/f.txt", b"x"); lock(os.path.join(src, "P", f"locked{i}"))
        put(src, "P/ok.txt", b"x")
        old = fp.MAX_UNREADABLE; fp.MAX_UNREADABLE = 1
        try:
            qz, *_ = fp.quick_scan(src, os.path.join(t, "q"), **quiet)
            fz, *_ = fp.profile(src, os.path.join(t, "f"), workers=1, system_info=False, tier=1, **quiet)
        finally:
            fp.MAX_UNREADABLE = old
        check("step 1 says how many unreadable items the list cap left out", info(qz)["unreadable_not_listed_cap"], "2")
        check("step 2 says it too, and the log has a WARN", (info(fz)["unreadable_not_listed_cap"], any("more unreadable" in r["message"] for r in rd(fz, "Run_log.csv"))), ("2", True))
        unlock_all(src)
    with tempfile.TemporaryDirectory() as t:
        src = os.path.join(t, "data")
        put(src, "A/x/f.txt", b"x"); put(src, "A/y/f.txt", b"x"); lock(os.path.join(src, "A", "y"))
        qz, *_ = fp.quick_scan(src, os.path.join(t, "q"), project_depth=3, **quiet)
        check("a folder that cannot even be listed when choosing projects is reported (quick)", [r["where"] for r in rd(qz, "Setup_Warnings.csv")] if "Setup_Warnings.csv" in zipfile.ZipFile(qz).namelist() else [], ["project folders"])
        unlock_all(src)

# ---------------------------------------------------------------- a corrupt saved-progress file is no longer ignored silently
class Stop(Exception): pass
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data")
    for i in range(12): put(src, f"P/f{i:02d}.sldprt", b"x" * (i + 1))
    out = os.path.join(t, "o")
    def hook(ui, ci):
        if ci == 0: raise Stop()
    fp._TEST_HOOK = hook
    try:
        fp.profile(src, out, workers=1, chunk_files=4, system_info=False, tier=1, **quiet)
    except Stop:
        pass
    fp._TEST_HOOK = None
    mans = [os.path.join(d, "manifest.json") for d, _s, fs in os.walk(os.path.join(out, ".resume")) if "manifest.json" in fs]
    open(mans[0], "w").write("{ this is not json")
    z, *_ = fp.profile(src, out, workers=1, chunk_files=4, system_info=False, tier=1, **quiet)
    check("corrupt manifest: the run log says progress was dropped and why", any("could not be read" in r["message"] and "JSONDecodeError" in r["message"] for r in rd(z, "Run_log.csv")), True)

# ---------------------------------------------------------------- UI error log: appended, never overwritten
import ui
with tempfile.TemporaryDirectory() as t:
    ui.OUT_DIR = t
    ui._write_error_log("first failure"); ui._write_error_log("second failure")
    txt = open(os.path.join(t, "error_log.txt")).read()
    check("error_log.txt keeps both failures, each with a time line", ("first failure" in txt, "second failure" in txt, txt.count("=== ")), (True, True, 2))

# ---------------------------------------------------------------- Fusion: causes are kept, run log is in the zip
class H(http.server.BaseHTTPRequestHandler):
    hits = {"n": 0}
    def log_message(self, *a): pass
    def _j(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path
        if p.startswith("/project/v1/hubs/b.hub1/projects/p1/topFolders"):
            return self._j(200, {"data": [{"type": "folders", "id": "f1", "attributes": {"displayName": "Top"}}]})
        if p.startswith("/project/v1/hubs/b.hub1/projects"):
            return self._j(200, {"data": [{"type": "projects", "id": "p1", "attributes": {"name": "Proj"}}]})
        if p.startswith("/project/v1/hubs"):
            H.hits["n"] += 1
            if H.hits["n"] == 1:
                return self._j(503, {"developerMessage": "maintenance window"})
            return self._j(200, {"data": [{"type": "hubs", "id": "b.hub1", "attributes": {"name": "Hub"}}]})
        if "/folders/f1/contents" in p:
            return self._j(403, {"reason": "no access to this folder"})
        if p.startswith("/capacity-once"):
            H.hits["cap"] = H.hits.get("cap", 0) + 1
            return self._j(403, {"message": "request capacity exceeded"}) if H.hits["cap"] <= 2 else self._j(200, {"ok": True})
        if p.startswith("/capacity-always"):
            return self._j(403, {"message": "request capacity exceeded"})
        if p.startswith("/forbidden"):
            return self._j(403, {"developerMessage": "app not provisioned"})
        self._j(404, {})
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._j(401, {"error": "invalid_client", "error_description": "client authentication failed"})
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
fc.time.sleep = lambda s: None                                       # do not wait out the retry pauses in a test
api = fc.Api("tok", base)
hubs = fc.hubs(api)
check("a 503 is retried and the log keeps Autodesk's own words", (hubs, any("503" in e[4] and "maintenance window" in e[4] for e in api.events)), ([("b.hub1", "Hub")], True))
try:
    api.get("/forbidden")
except fc.ApiError as e:
    check("a 403 carries Autodesk's explanation", "app not provisioned" in str(e), True)
try:
    fc.auth_finish("cid", "http://localhost:8765/callback", "code", "verifier", base)
except fc.ApiError as e:
    check("a failed sign-in shows Autodesk's error (invalid_client), not only the status", ("invalid_client" in str(e), "client authentication failed" in str(e)), (True, True))
check("403 'request capacity exceeded' is waited out and retried, not treated as no permission", (api.get("/capacity-once"), api.retried >= 3), ({"ok": True}, True))
api_t = fc.Api("tok", base, throttle_retries=3)
try:
    api_t.get("/capacity-always")
except fc.ApiError as e:
    check("endless throttling ends with a clear 'rate / capacity limit, not a permission problem' message", ("not a permission problem" in str(e), "no read permission" in str(e)), (True, False))
    check("each wait is in the log as throttled", len([x for x in api_t.events if "throttled" in x[4]]) >= 3, True)
dead = fc.Api("tok", "http://127.0.0.1:1", retries=2)
try:
    dead.get("/project/v1/hubs")
except fc.ApiError as e:
    check("network failure: the final error names the last cause", "last problem" in str(e) and "ConnectionRefusedError" in str(e), True)
    check("network failure: every failed try is in the log with its cause", len([x for x in dead.events if "try" in x[4]]), 2)
with tempfile.TemporaryDirectory() as t:
    api2 = fc.Api("tok", base)
    path, n, np_, stats = fc.run(api2, "b.hub1", "Hub", t, quiet=True)
    z = zipfile.ZipFile(path)
    check("zip contains Run_log.csv with start, project and finish lines", {"start", "project", "finish"} <= {r["phase"] for r in rd(path, "Run_log.csv")}, True)
    check("the unreadable folder's reason is in the log and in Unreadable.csv", (any("no access to this folder" in r["message"] for r in rd(path, "Run_log.csv")),
          [u["reason"].count("403") > 0 for u in rd(path, "Unreadable.csv")]), (True, [True]))
    check("Run_info reports the log size", info(path)["log_events"] != "0", True)
    sz = stats["stats_zip"]
    sn = zipfile.ZipFile(sz).namelist()
    check("a separate run-statistics zip is written next to the profile zip", (os.path.dirname(sz) == os.path.dirname(path), sorted(sn)), (True, ["README.txt", "Run_log.csv", "Run_stats.csv"]))
    st = {r["key"]: r["value"] for r in rd(sz, "Run_stats.csv")}
    check("it carries the same log and counters", (len(rd(sz, "Run_log.csv")) == len(rd(path, "Run_log.csv")), st["unreadable_folders"], int(st["log_errors"]) >= 1), (True, "1", True))
srv.shutdown()

print("\nFAILED" if any(bad) else "\nall passed"); sys.exit(1 if any(bad) else 0)
