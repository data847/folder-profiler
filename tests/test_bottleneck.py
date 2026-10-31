"""The run says what limits it. classify_window is judged on known numbers (each limit, and the order they are checked in); the memory gate
measures its waiting; a real run writes 'speed' lines to Run_log.csv and main_limit / limit_breakdown to Run_info.csv."""
import csv, io, os, sys, tempfile, threading, time, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp, sysmetrics as sm
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
base = dict(seconds=30.0, workers=4, cpus=4, read_busy_s=0.0, wait_s=0.0, ckpt_s=0.0, cpu_s=0.0, memwait_s=0.0, files=3000, bytes=300e6, budget_mb=256)
def c(**kw): return sm.classify_window({**base, **kw})[0]
check("workers blocked on the memory budget -> memory", c(memwait_s=30 * 4 * 0.2, cpu_s=30 * 4 * 0.95), "memory")
check("processor saturated -> cpu", c(cpu_s=30 * 4 * 0.95, read_busy_s=100, wait_s=20), "cpu")
check("slow saving of progress -> saving", c(ckpt_s=12, cpu_s=10), "saving")
check("main thread waiting for busy readers, cpu low -> reads", c(wait_s=22, read_busy_s=30 * 4 * 0.8, cpu_s=10), "reads")
check("readers idle, main thread listing -> listing", c(wait_s=2, read_busy_s=30 * 4 * 0.1, cpu_s=15), "listing")
check("nothing dominant -> balanced", c(wait_s=10, read_busy_s=30 * 4 * 0.55, cpu_s=40), "balanced")
check("1 CPU fully busy is cpu even if reads are slow too", c(cpus=1, cpu_s=29, wait_s=25, read_busy_s=30 * 4 * 0.9), "cpu")
check("text carries the measured rate", "100 files/s" in sm.classify_window(base)[1], True)
# the gate measures how long readers wait
g = fp.MemGate(100)
def hold(): 
    with g.hold(100): time.sleep(0.3)
a = threading.Thread(target=hold); b = threading.Thread(target=hold); a.start(); time.sleep(0.05); b.start(); a.join(); b.join()
check("the memory gate counts one wait of about 0.25 s", (g.waits, 0.15 < g.wait_s < 0.6), (1, True))
# a real run
os.environ["PROFILER_BOTTLENECK_SECS"] = "1"
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "d")
    for p in range(3):
        for i in range(900):
            d = os.path.join(src, f"P{p}", f"s{i % 5}"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"f{i}.dwg"), "wb").write(b"AC1027" + b"\0" * 300)
    real = fp.signature_stream if hasattr(fp, "signature_stream") else None
    z, *_ = fp.profile(src, os.path.join(t, "o"), log=lambda *_: None, workers=2, chunk_files=100, system_info=False, tier=2)
    zf = zipfile.ZipFile(z)
    info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zf.read("Run_info.csv").decode("utf-8-sig")))}
    log = list(csv.DictReader(io.StringIO(zf.read("Run_log.csv").decode("utf-8-sig"))))
    speed = [r["message"] for r in log if r["phase"] == "speed"]
    check("the run log has speed lines", len(speed) >= 1, True)
    check("Run_info names a main limit from the known list", info["main_limit"] in ("memory", "cpu", "saving", "reads", "listing", "balanced") or info["main_limit"].startswith("not measured"), True)
    check("limit_breakdown is present", "limit_breakdown" in info, True)
    print("   ", info["main_limit"], "|", info["limit_breakdown"]); print("   ", speed[:2])

# real runs with a slow disk and with slow folder listing: the verdict has to follow what we made slow
def real_run(patch, files_per_folder=40, folders=6, projects=2, workers=4, chunk=60):
    with tempfile.TemporaryDirectory() as t:
        src = os.path.join(t, "d")
        for p in range(projects):
            for f in range(folders):
                d = os.path.join(src, f"P{p}", f"s{f}"); os.makedirs(d)
                for i in range(files_per_folder):
                    open(os.path.join(d, f"p{p}_s{f}_{i}.dwg"), "wb").write(b"AC1027" + b"\0" * 300)      # unique names: exact copies are read only once
        restore = patch()
        try:
            z, *_ = fp.profile(src, os.path.join(t, "o"), log=lambda *_: None, workers=workers, chunk_files=chunk, system_info=False, tier=2)
        finally:
            restore()
        zf = zipfile.ZipFile(z)
        info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zf.read("Run_info.csv").decode("utf-8-sig")))}
        speed = [r["message"] for r in csv.DictReader(io.StringIO(zf.read("Run_log.csv").decode("utf-8-sig"))) if r["phase"] == "speed"]
        return info, speed
def slow_reads():
    o = fp.signature_stream
    def slow(*a, **k): time.sleep(0.02); return o(*a, **k)
    fp.signature_stream = slow
    return lambda: setattr(fp, "signature_stream", o)
def slow_listing():
    o = fp.walk_dirs
    def slow(*a, **k):
        for item in o(*a, **k):
            time.sleep(0.4); yield item
    fp.walk_dirs = slow
    os.environ["PROFILER_REUSE_LISTING"] = "0"         # with the counting pass's listing reused there is no second listing to be slow
    def restore():
        fp.walk_dirs = o; os.environ.pop("PROFILER_REUSE_LISTING", None)
    return restore
fp.PIPELINE_CHUNKS = 4
info, speed = real_run(slow_reads, files_per_folder=80)
print("   slow reads  ->", info["main_limit"], "|", info["limit_breakdown"]); print("   ", speed[-1:] )
check("slow file reads are reported as the limit (reads)", info["main_limit"], "reads")
info, speed = real_run(slow_listing)
print("   slow listing ->", info["main_limit"], "|", info["limit_breakdown"]); print("   ", speed[-1:])
check("slow folder listing is reported as the limit (listing)", info["main_limit"], "listing")
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
