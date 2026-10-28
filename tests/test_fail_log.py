"""Fail log: when a run fails the UI offers ONE zip for remote debugging - with the error, the run's own log, settings and machine - and
without the owner's paths, user name or access tokens (and, with name anonymisation on, without anything below the scanned folder)."""
import io, json, os, sys, tempfile, threading, time, urllib.request, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp, ui
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
USER = os.environ.get("USER") or os.environ.get("USERNAME") or "nouser"
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "ZQXScanRoot"); out = os.path.join(t, "out"); os.makedirs(os.path.join(src, "ZQXSecretProject"));  os.makedirs(out)
    for i in range(12): open(os.path.join(src, "ZQXSecretProject", f"f{i}.txt"), "w").write("x")
    ui.OUT_DIR = out
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"; T = ui.TOKEN
    def post(doc): return json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/start", data=json.dumps(doc).encode(), method="POST")).read())
    def status(): return json.loads(urllib.request.urlopen(f"{base}/api/status?t={T}").read())
    def wait():
        for _ in range(300):
            s = status()
            if s["state"] != "running": return s
            time.sleep(0.1)
    def fail_zip():
        return zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(f"{base}/api/download_fail?t={T}").read()))
    def text_of(z): return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())

    # 1) a crash in the MIDDLE of a real run (after the first saved chunk)
    class Boom(Exception): pass
    BAD = os.path.join(src, "ZQX" + "SecretProject", "f3" + ".txt")                      # built here so the traceback's source line holds no sentinel
    def hook(ui_, chunk): raise Boom("disk vanished at " + BAD + " token=Bearer " + "abcdefghijklmnop" + "1234567890")
    fp._TEST_HOOK = hook
    real_chunk = fp.CHUNK_FILES; fp.CHUNK_FILES = 5
    check("run starts", post({"t": T, "path": src, "kind": "full"}), {"ok": True})
    s = wait(); fp._TEST_HOOK = None; fp.CHUNK_FILES = real_chunk
    check("the run ends in error and offers a fail log", (s["state"], bool(s["fail"]), (s["fail"] or {}).get("name", "").startswith("fail_log_")), ("error", True, True))
    check("the page message points at the button, not at a file on disk", "Download fail log" in s["error"], True)
    z = fail_zip(); txt = text_of(z)
    check("zip has the error, settings, screen log, the run's own log + metrics, progress and machine description",
          {"problem.txt", "settings.txt", "screen_log.txt", "Run_log.csv", "Run_metrics.csv", "progress.txt", "System_info.txt", "README.txt"} <= set(z.namelist()), True)
    check("the error text is there", "disk vanished" in z.read("problem.txt").decode() and "Boom" in z.read("problem.txt").decode(), True)
    check("the scanned folder's path became <scanned-folder>", ("<scanned-folder>" in txt, src in txt), (True, False))
    check("the user name and home folder are not there", (os.path.expanduser("~") in txt, ("/" + USER + "/") in txt), (False, False))
    check("the access token was removed", ("abcdefghijklmnop1234567890" in txt, "<removed>" in txt), (False, True))
    check("settings say version, memory budget and the mode", all(k in z.read("settings.txt").decode() for k in ("tool_version", "memory_budget_MB", "mode: full profile")), True)
    check("progress shows a chunk was saved before the crash", "chunks saved: 1" in z.read("progress.txt").decode() or "chunks saved: 2" in z.read("progress.txt").decode(), True)

    # 2) with anonymisation on, nothing below the scanned folder survives
    fp._TEST_HOOK = hook; fp.CHUNK_FILES = 5
    post({"t": T, "path": src, "kind": "full", "anon_names": True, "fresh": True}); s = wait(); fp._TEST_HOOK = None; fp.CHUNK_FILES = real_chunk
    z = fail_zip(); txt = text_of(z)
    for n in z.namelist():
        b = z.read(n).decode("utf-8", "replace")
        for w in ("ZQXSecretProject", "ZQXScanRoot", "f3.txt"):
            if w in b: i = b.index(w); print("     LEAK", n, w, repr(b[max(0, i - 70):i + 50]))
    check("anonymised run: no real name anywhere in the fail log", [w for w in ("ZQXSecretProject", "ZQXScanRoot", "f3.txt") if w in txt], [])
    check("and the README says why", "anonymisation" in z.read("README.txt").decode(), True)

    # 3) a plain exception before anything ran, and an expected error message
    orig = fp.profile
    fp.profile = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("explode in " + src))
    post({"t": T, "path": src, "kind": "full"}); s = wait(); fp.profile = orig
    check("any exception gets a fail log", (s["state"], "explode" in fail_zip().read("problem.txt").decode()), ("error", True))
    fp.profile = lambda *a, **k: (_ for _ in ()).throw(SystemExit("detail level must be 0, 1 or 2"))
    post({"t": T, "path": src, "kind": "full"}); s = wait(); fp.profile = orig
    check("a SystemExit message too", (s["state"], s["error"], "detail level" in fail_zip().read("problem.txt").decode()), ("error", "detail level must be 0, 1 or 2", True))
    # 4) a good run has no fail log
    post({"t": T, "path": src, "kind": "quick"}); s = wait()
    check("a successful run offers none", (s["state"], s["fail"]), ("done", None))
    # 4b) the OUTPUT folder is the problem (a plain file where the folder should be): the run must still end in an error with a downloadable fail log
    blocker = os.path.join(t, "iamafile"); open(blocker, "w").write("x"); good_out = ui.OUT_DIR; ui.OUT_DIR = os.path.join(blocker, "output")
    post({"t": T, "path": src, "kind": "full"}); s = wait()
    check("unwritable output folder: the page does not hang on 'running'; it shows an error and a fail log", (s["state"], bool(s["fail"])), ("error", True))
    check("and that fail log downloads (it was written to the temp folder)", "problem.txt" in fail_zip().namelist(), True)
    # 4c) the safety net: the run function itself blows up with something unexpected
    real_run = ui._run
    def broken(*a, **k): raise MemoryError("simulated out of memory")
    ui._run = broken
    post({"t": T, "path": src, "kind": "quick"}); s = wait(); ui._run = real_run; ui.OUT_DIR = good_out
    check("an unexpected error inside the run still ends in 'error' with a fail log", (s["state"], bool(s["fail"]), "out of memory" in fail_zip().read("problem.txt").decode()), ("error", True, True))
    # 5) the page wires the button
    page = urllib.request.urlopen(base + "/").read().decode()
    check("page has the Download fail log button in both tabs", page.count("download_fail"), 2)
    srv.shutdown()
sys.exit(1 if any(bad) else 0)
