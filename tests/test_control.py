"""Stop and pause: Stop (during counting, during profiling) ends the run cleanly without losing what was finished, and the same command carries
on to exactly an uninterrupted run's output; Pause waits at a chunk boundary and its time is not counted in the run's seconds; keep-awake is
off in Docker and when switched off; the page's control endpoint and time-left estimate work."""
import csv, io, json, os, sys, tempfile, threading, time, urllib.request, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp, sysmetrics
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
def rows_of(z): return [{k: v for k, v in r.items() if k != "dir_id"} for r in rd(z, "Files.csv")]
CALLS = [0]; _sig = fp.signature
def counting(*a, **k): CALLS[0] += 1; return _sig(*a, **k)
fp.signature = counting
fp.PIPELINE_CHUNKS = 2
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "d")
    for p in range(4):
        for i in range(40):
            d = os.path.join(src, f"P{p}", f"s{i % 4}"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"f{i:02d}.sldprt"), "wb").write(b"x" * (i + 1 + p))
    msgs = []
    def run(out, **kw):
        z, *_ = fp.profile(src, out, log=lambda m: msgs.append(m), workers=2, chunk_files=8, system_info=False, tier=1, **kw); return z
    CALLS[0] = 0; base = rows_of(run(os.path.join(t, "base"))); total = CALLS[0]
    # ---- Stop while profiling
    out = os.path.join(t, "stop"); CALLS[0] = 0
    def stop_hook(ui, ch):
        if (ui, ch) == (1, 1): fp.CONTROL.stop = True
    fp._TEST_HOOK = stop_hook
    try: run(out); stopped = False
    except fp.RunStopped: stopped = True
    fp._TEST_HOOK = None; c1 = CALLS[0]
    check("Stop ends the run with RunStopped (not a failure)", stopped, True)
    check("no result zip was written, the saved progress is there", ([f for f in os.listdir(out) if f.startswith("profile_")], os.path.isdir(os.path.join(out, ".resume"))), ([], True))
    del msgs[:]; CALLS[0] = 0
    z = run(out); c2 = CALLS[0]
    check("the same command carries on: nothing read twice", c1 + c2, total)
    check("final rows equal an uninterrupted run's", rows_of(z) == base, True)
    check("the run log says it was stopped on request", any("stopped on request" in r["message"] for r in rd(z, "Run_log.csv") if "message" in r), True)
    # ---- Pause, then resume
    out = os.path.join(t, "pause"); del msgs[:]
    def pause_hook(ui, ch):
        if (ui, ch) == (1, 1):
            fp.CONTROL.pause = True
            threading.Timer(1.5, lambda: setattr(fp.CONTROL, "pause", False)).start()
    fp._TEST_HOOK = pause_hook
    t0 = time.time(); z = run(out); wall = time.time() - t0
    fp._TEST_HOOK = None
    info = {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
    check("a paused run still ends with exactly the same rows", rows_of(z) == base, True)
    check("the log shows pause and resume", (any("paused - what is finished is saved" in m for m in msgs), any("resumed after" in m for m in msgs)), (True, True))
    check("paused time is not part of the run's seconds", float(info["seconds"]) < wall - 1.0, True)
    # ---- Stop while counting (slow listing)
    real_scandir = os.scandir
    def slow_scandir(*a, **k): time.sleep(0.05); return real_scandir(*a, **k)
    out = os.path.join(t, "scanstop"); del msgs[:]
    big = os.path.join(t, "big")
    for p in range(3):
        for f in range(60):
            d = os.path.join(big, f"P{p}", f"s{f}"); os.makedirs(d); open(os.path.join(d, "a.dwg"), "wb").write(b"x")
    os.scandir = slow_scandir
    threading.Timer(0.6, lambda: setattr(fp.CONTROL, "stop", True)).start()
    t0 = time.time()
    try:
        fp.profile(big, out, log=lambda m: msgs.append(m), workers=2, system_info=False, tier=1); sstopped = False
    except fp.RunStopped: sstopped = True
    dt = time.time() - t0
    check("Stop works while counting, and quickly (well before the ~9 s the count needs)", (sstopped, dt < 4), (True, True))
    # Step 1 too
    threading.Timer(0.6, lambda: setattr(fp.CONTROL, "stop", True)).start()
    try:
        fp.quick_scan(big, os.path.join(t, "q"), log=lambda *_: None); qstopped = False
    except fp.RunStopped: qstopped = True
    os.scandir = real_scandir
    check("Stop works in Step 1; no zip is left", (qstopped, [f for f in os.listdir(os.path.join(t, "q"))] if os.path.isdir(os.path.join(t, "q")) else []), (True, []))
    fp.CONTROL.reset()
    z = fp.profile(big, os.path.join(t, "scanstop"), log=lambda *_: None, workers=2, system_info=False, tier=1)[0]
    check("after a stop in the counting pass the next run starts clean and completes", len(rd(z, "Files.csv")), 180)
# ---- keep awake
ka = sysmetrics.KeepAwake(); os.environ["PROFILER_KEEP_AWAKE"] = "0"
check("keep-awake off when switched off", ka.start(), "")
os.environ.pop("PROFILER_KEEP_AWAKE"); os.environ["PROFILER_IN_DOCKER"] = "1"
check("keep-awake off in Docker", sysmetrics.KeepAwake().start(), "")
os.environ.pop("PROFILER_IN_DOCKER")
ka = sysmetrics.KeepAwake(); how = ka.start()
if sys.platform == "darwin":
    check("macOS: caffeinate is running, then stopped", (how, ka.proc is not None and ka.proc.poll() is None), ("caffeinate", True))
    p = ka.proc; ka.stop(); time.sleep(0.3); check("   ...and gone after stop()", p.poll() is not None, True)
else:
    ka.stop(); print("   (keep-awake on this OS:", how or "unavailable", ")")
# ---- the page's endpoint and time-left estimate
import ui
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
def call(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
    try: return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e: return {"_http": e.code}
check("control without a run in progress is refused", "error" in call("/api/control", {"t": ui.TOKEN, "action": "stop"}), True)
check("control with a wrong token is refused", call("/api/control", {"t": "x", "action": "stop"}), {"_http": 403})
ui.STATE.update(state="running", progress={"phase": "process", "files": 1000, "total_files": 11000, "bytes": 0, "total_bytes": 0})
now = time.time(); ui.HIST.clear(); ui.HIST.extend([(now - 60, 400), (now - 30, 700), (now, 1000)])        # 10 files/s -> 10,000 left = 1,000 s
st = call("/api/status?t=" + ui.TOKEN); check("time left from the speed of the last minute", round(st["eta_s"]), 1000)
call("/api/control", {"t": ui.TOKEN, "action": "pause"}); st = call("/api/status?t=" + ui.TOKEN)
check("pause is reported, no time left is promised while paused", (st["paused"], st["eta_s"]), (True, None))
call("/api/control", {"t": ui.TOKEN, "action": "resume"}); call("/api/control", {"t": ui.TOKEN, "action": "stop"})
st = call("/api/status?t=" + ui.TOKEN); check("stop is reported", (st["stopping"], st["paused"]), (True, False))
fp.CONTROL.reset(); ui.STATE.update(state="idle"); srv.shutdown()
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
