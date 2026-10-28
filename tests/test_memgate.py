"""Memory guard: the bytes of whole-file reads in flight never exceed the budget (an oversized file runs alone, nothing deadlocks), and a
real run with many huge files stays far below the unguarded peak."""
import os, subprocess, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)

# ---- unit: bound and no deadlock
g = fp.MemGate(100); peak = [0]; lock = threading.Lock(); inflight = [0]
def task(cost):
    with g.hold(cost):
        with lock:
            inflight[0] += min(cost, 100); peak[0] = max(peak[0], inflight[0])
        time.sleep(0.01)
        with lock: inflight[0] -= min(cost, 100)
ts = [threading.Thread(target=task, args=(c,)) for c in [30, 40, 50, 60, 20, 500, 10, 90, 25, 35] * 4]
t0 = time.time(); [t.start() for t in ts]; [t.join(timeout=30) for t in ts]
check("all 40 tasks finished (no deadlock, oversized 500 > budget 100 ran alone)", (not any(t.is_alive() for t in ts), g.used), (True, 0))
check("in-flight cost never exceeded the budget", peak[0] <= 100, True)
check("some tasks had to wait", g.waits > 0, True)
check("budget from the environment", (os.environ.update(PROFILER_MEM_BUDGET_MB="300"), fp.memory_budget())[1], 300 * 1024 * 1024)
del os.environ["PROFILER_MEM_BUDGET_MB"]
check("budget from RAM is between 256 MB and 2 GB", 256 << 20 <= fp.memory_budget() <= 2 << 30, True)

# ---- real peak memory: ten 100 MB 'drawings', ten workers
CHILD = r'''
import os, sys, resource
sys.path.insert(0, %r)
import file_profiler as fp
z = fp.profile(sys.argv[1], sys.argv[2], log=lambda *a: None, resume=False, system_info=False, workers=10)[0]
div = 1 if sys.platform == "darwin" else 1024
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / div / 1048576 if sys.platform == "darwin" else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
''' % ROOT
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "r", "P"); os.makedirs(src)
    for i in range(10):
        with open(os.path.join(src, f"big{i}.dwg"), "wb") as f:
            f.write(b"AC1032" + b"\0" * 20); f.truncate(100 * 1024 * 1024)        # sparse: no real disk used
    def peak(budget_mb):
        env = dict(os.environ, PROFILER_MEM_BUDGET_MB=str(budget_mb))
        r = subprocess.run([sys.executable, "-c", CHILD, os.path.join(t, "r"), os.path.join(t, f"o{budget_mb}")], capture_output=True, text=True, env=env, timeout=600)
        return float(r.stdout.strip().splitlines()[-1])
    free, small = peak(100000), peak(320)
    print(f"     peak memory MB: unguarded {free:.0f}, budget 320 MB {small:.0f}")
    check("guarded peak is well below unguarded (ten 100 MB drawings, ten workers)", small < free * 0.6, True)
sys.exit(1 if any(bad) else 0)
