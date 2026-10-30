"""Step 1 file lists keep memory flat (rows spill to temp files past PROFILER_PATHS_MEM_ROWS) without changing a single row; the temp files are
removed; container limits (cgroup v1 / v2) lower the memory budget and the default worker count."""
import csv, glob, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp, sysmetrics
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rows(z):
    out = []
    for n in z.namelist():
        if n.startswith("Paths/"):
            out += [(n, r["project"], r["path"], r["size_bytes"], r["modified_utc"]) for r in csv.DictReader(io.StringIO(z.read(n).decode("utf-8-sig")))]
    return sorted(out)
with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "R")
    for p in range(6):
        for i in range(400):
            d = os.path.join(root, f"proj{p}", f"s{i % 5}", f"l{i % 3}"); os.makedirs(d, exist_ok=True)
            f = os.path.join(d, f"f{i}{['.dwg', '.pdf', '.txt', ''][i % 4]}")
            open(f, "wb").write(b"x" * (i % 17)); os.utime(f, (1_700_000_000 + i * 31, 1_700_000_000 + i * 31))
    tmpd = os.path.join(t, "tmp"); os.makedirs(tmpd); os.environ["TMPDIR"] = tmpd; tempfile.tempdir = None
    os.environ.pop("PROFILER_PATHS_MEM_ROWS", None)
    a = rows(zipfile.ZipFile(fp.quick_scan(root, os.path.join(t, "a"), log=lambda *_: None, list_paths=True, workers=3)[0]))
    os.environ["PROFILER_PATHS_MEM_ROWS"] = "30"                                  # far below the 2,400 files: many spills
    zb = zipfile.ZipFile(fp.quick_scan(root, os.path.join(t, "b"), log=lambda *_: None, list_paths=True, workers=3)[0])
    b = rows(zb)
    info = {r[0]: r[1] for r in csv.reader(io.StringIO(zb.read("Run_info.csv").decode("utf-8-sig")))}
    check("2,400 files listed", len(a), 2400)
    check("spilled run lists exactly the same rows as the in-memory run", a == b, True)
    check("the spilled run really spilled", int(info.get("path_list_spills_to_disk", 0)) > 3, True)
    check("temp files are gone afterwards", glob.glob(os.path.join(tmpd, "profiler_paths_*")), [])
    # odd names survive a spill
    s = fp.PathStore(1, 1)
    s.add(0, ".txt", 0, "a/b", [('we"ird, name\nx.txt', 5, 1.5), ("ünï.txt", 0, 2.0)]); s.add(0, ".txt", 1, "", [("z.txt", 1, 3.0)])
    got = [(u, r, l) for u, r, l in s.chunks(".txt")]
    check("quotes, commas, newlines and unicode survive the spill", got, [(0, "a/b", [('we"ird, name\nx.txt', 5, 1.5), ("ünï.txt", 0, 2.0)]), (1, "", [("z.txt", 1, 3.0)])])
    s.close(); check("store removes its folder", s.dir, None)
# container limits
real = sysmetrics._read_first
def fake(files):
    sysmetrics._read_first = lambda p: files.get(p, "")
try:
    sysmetrics.IS_LINUX = True
    fake({"/sys/fs/cgroup/memory.max": "268435456", "/sys/fs/cgroup/cpu.max": "100000 100000"})
    check("cgroup v2 memory limit", sysmetrics.cgroup_mem_limit(), 268435456); check("cgroup v2 one cpu", sysmetrics.cgroup_cpus(), 1.0)
    check("one cpu -> one usable cpu", sysmetrics.effective_cpus(), 1)
    fake({"/sys/fs/cgroup/memory.max": "max", "/sys/fs/cgroup/cpu.max": "max 100000"})
    check("'max' means no limit", (sysmetrics.cgroup_mem_limit(), sysmetrics.cgroup_cpus()), (0, 0))
    fake({"/sys/fs/cgroup/memory/memory.limit_in_bytes": "536870912", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "150000", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"})
    check("cgroup v1 memory limit", sysmetrics.cgroup_mem_limit(), 536870912); check("cgroup v1 1.5 cpus", sysmetrics.cgroup_cpus(), 1.5)
    fake({"/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712"})
    check("v1 'unlimited' number means no limit", sysmetrics.cgroup_mem_limit(), 0)
    fake({"/sys/fs/cgroup/memory.max": "268435456", "/sys/fs/cgroup/cpu.max": "100000 100000"})
    os.environ.pop("PROFILER_WORKERS", None); os.environ.pop("PROFILER_MEM_BUDGET_MB", None)
    check("1 cpu -> 2 default workers", fp.default_workers(), 2)
finally:
    sysmetrics._read_first = real
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
