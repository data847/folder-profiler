"""Run statistics: time per step, system samples, machine description - shareable and free of names.   Run: python tests/test_metrics.py"""
import csv, getpass, io, os, socket, sys, tempfile, time, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import file_profiler as fp

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)

_orig = fp.signature
def slow_sig(*a, **k):
    time.sleep(0.012); return _orig(*a, **k)

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data")
    for p in range(2):
        for i in range(90):
            d = os.path.join(src, f"Proj{p}", f"d{i % 4}"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"f{i}.sldprt"), "wb").write(b"x" * (i + 1))
    fp.signature = slow_sig
    out = os.path.join(t, "out")
    z, *_ = fp.profile(src, out, log=lambda *_: None, workers=1, tier=0, metrics_interval=1.0, chunk_files=60)
    fp.signature = _orig
    stats = [f for f in os.listdir(out) if f.startswith("run_stats_")]
    check("a separate, shareable run_stats zip is written next to the profile", len(stats), 1)
    zs, zp = zipfile.ZipFile(os.path.join(out, stats[0])), zipfile.ZipFile(z)
    want = {"Run_log.csv", "Run_metrics.csv", "Run_phases.csv", "System_info.csv"}
    check("it holds the four logs", want <= set(zs.namelist()), True)
    check("the same four logs are inside the profile zip", want <= set(zp.namelist()), True)
    rd = lambda zf, n: list(csv.reader(io.StringIO(zf.read(n).decode("utf-8-sig"))))
    ph = rd(zs, "Run_phases.csv")
    names = [r[0] for r in ph]
    check("time per step: scan / process / finalize", [n.split(" ")[0] for n in names[1:4]], ["scan", "process", "finalize"])
    act = {r[0]: r for r in ph if r and r[0] in ("format_tag_read", "write_checkpoint")}
    check("busy seconds and call counts per activity (180 files read for their tag)", (act["format_tag_read"][2], float(act["format_tag_read"][1]) > 1.0), ("180", True))
    met = rd(zs, "Run_metrics.csv")
    check("system samples are written during the run (header + at least one row)", (met[0][:5], len(met) >= 2),
          (["timestamp_utc", "elapsed_s", "phase", "files_done", "bytes_done_GB"], True))
    check("samples carry the CPU / memory / throughput columns", {"process_cpu_pct", "ram_used_pct", "process_memory_MB", "MB_per_s_processed", "process_read_MB_per_s"} <= set(met[0]), True)
    check("the phase is recorded in each sample", {r[2] for r in met[1:]} <= {"scan", "process", "finalize", "start"}, True)
    if sys.platform.startswith("linux"):          # Linux has /proc: the CPU and read-throughput probes must really answer
        hdr = met[0]; row = met[1]
        check("Linux: system CPU %, RAM % and process read MB/s are filled in", all(row[hdr.index(c)] != "" for c in ("system_cpu_pct", "ram_used_pct", "process_read_MB_per_s", "process_memory_MB")), True)
    si = dict(rd(zs, "System_info.csv")[1:])
    check("machine description: OS, CPU, cores, RAM, Python", all(k in si for k in ("os", "cpu_model", "logical_cpus", "ram_gb", "python")), True)
    log = rd(zs, "Run_log.csv")
    check("event log shows start, per-project progress and the end", (log[1][3], any("done" in r[4] for r in log), log[-1][3]), ("start", True, "finalize"))
    blob = b"".join(zs.read(n) for n in zs.namelist()).decode("utf-8", "ignore")
    leaks = [x for x in (socket.gethostname(), getpass.getuser(), t, src) if x and x in blob]
    check("nothing identifying: no host name, user name, or path in any log", leaks, [])
    # a crash still leaves the log behind
    out2 = os.path.join(t, "out2")
    class Boom(Exception): pass
    fp._TEST_HOOK = lambda ui, ch: (_ for _ in ()).throw(Boom())
    try: fp.profile(src, out2, log=lambda *_: None, workers=1, tier=0, chunk_files=60)
    except Boom: pass
    fp._TEST_HOOK = None
    left = os.path.join(out2, ".resume", os.listdir(os.path.join(out2, ".resume"))[0], "Run_log.csv")
    check("after a crash the log is still on disk and says the run stopped", ("run stopped" in open(left).read(), "Progress is saved" in open(left).read()), (True, True))
sys.exit(1 if any(bad) else 0)
