"""Windows file-server shares (Server 2019, Azure File Sync, deduplication, dropped connections): retries, the unreadable list, cloud-tiered
files that must never be opened, and drive-aware thread counts - for step 1 AND step 2.   Run: python tests/test_network_shares.py"""
import builtins, csv, errno, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)
rd = lambda z, n: list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
info = lambda z, f=None: {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(body)
fp.RETRY_DELAYS = (0.0, 0.0, 0.0)                              # no waiting in tests

# ---- retry rules
calls = []
def flaky(n_fail, exc):
    def f():
        calls.append(1)
        if len(calls) <= n_fail:
            raise exc
        return "ok"
    return f
calls.clear(); c = fp.collections.Counter()
check("a dropped connection (ETIMEDOUT) is retried and then succeeds", (fp.retry(flaky(2, OSError(errno.ETIMEDOUT, "t")), c), len(calls), c["retries"]), ("ok", 3, 2))
calls.clear()
try:
    fp.retry(flaky(5, PermissionError(errno.EACCES, "denied")))
except PermissionError:
    pass
check("permission denied is NOT retried", len(calls), 1)
calls.clear()
try:
    fp.retry(flaky(99, OSError(errno.ETIMEDOUT, "t")))
except OSError:
    pass
check("a link that stays down gives up after 4 tries", len(calls), 4)

# ---- drive-aware threads
check("local disks: few listing threads; step 2 readers by cpu", (fp.quick_threads(["/tmp"]), fp.default_workers(["/tmp"]) <= 16), (4, True))
real_qt = fp.quick_threads
fp.quick_threads = lambda paths: 32                            # as for a UNC path / mapped network drive
check("network share: many readers for step 2 (the file opens wait on the network)", fp.default_workers(["\\\\server\\share"]), 48)
os.environ["PROFILER_WORKERS"] = "3"
check("PROFILER_WORKERS still overrides", fp.default_workers(["\\\\server\\share"]), 3)
del os.environ["PROFILER_WORKERS"]
fp.quick_threads = real_qt

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "data")
    for i in range(12):
        put(src, f"Alpha/sub{i % 2}/f{i}.sldprt", b"p" * (10 + i))
    put(src, "Alpha/sub0/plan.dwg", b"AC1015" + b"\x00" * 200)
    put(src, "Beta/ok.step", b"s" * 30)
    # ---- a dropped connection during a listing is retried; counts are not doubled
    real_scandir, state = os.scandir, {"n": 0}
    def flaky_scandir(path):
        it = real_scandir(path)
        if str(path).endswith("sub1") and state["n"] < 2:
            state["n"] += 1
            class Dying:
                def __enter__(s): return s
                def __exit__(s, *a): it.close()
                def __iter__(s):
                    yield next(iter(it))                        # one entry, then the share goes away
                    raise OSError(errno.ETIMEDOUT, "network timeout")
            return Dying()
        return it
    os.scandir = flaky_scandir
    try:
        qz, _p, nfiles, _e = fp.quick_scan(src, os.path.join(t, "q1"), log=lambda *_: None)
    finally:
        os.scandir = real_scandir
    check("step 1: 2 dropped listings retried, every file counted exactly once, retries reported", (nfiles, info(qz)["network_retries"]), (14, "2"))

    # ---- unreadable folders are listed (step 1 and step 2); files are not lost from the counts around them
    locked = os.path.join(src, "Alpha", "locked")
    put(src, "Alpha/locked/secret.dwg", b"zz")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        os.chmod(locked, 0)
        try:
            qz2, *_ = fp.quick_scan(src, os.path.join(t, "q2"), log=lambda *_: None)
            u1 = rd(qz2, "Unreadable.csv")
            check("step 1: the unreadable folder is listed with its project, path and reason", [(r["project"], r["kind"], r["path"], r["reason"]) for r in u1], [("Alpha", "folder", "locked", "PermissionError")])
            check("step 1: Run_info counts it", info(qz2)["unreadable_folders"], "1")
            fz, *_ = fp.profile(src, os.path.join(t, "f2"), log=lambda *_: None, workers=2, system_info=False, tier=2)
            u2 = rd(fz, "Unreadable.csv")
            check("step 2: the same folder is listed", [(r["project"], r["kind"], r["path"], r["reason"]) for r in u2], [("Alpha", "folder", "locked", "PermissionError")])
            try:
                sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
                import compile_profiles as cp
            except ImportError:
                cp = None
            if cp is not None:
                import subprocess
                outd = os.path.join(t, "cmp")
                subprocess.run([sys.executable, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py"), fz, "--out", outd], capture_output=True, text=True)
                ur = list(csv.DictReader(open(os.path.join(outd, "Unreadable.csv"), encoding="utf-8-sig")))
                rv = open(os.path.join(outd, "Review.csv"), encoding="utf-8-sig").read()
                check("compiler: Unreadable.csv is written and Review explains it", ([(r["project"], r["path"]) for r in ur], "could not be read" in rv and "NOT in the counts" in rv), ([("Alpha", "locked")], True))
                qd = os.path.join(t, "cmpq")
                subprocess.run([sys.executable, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py"), qz2, "--out", qd], capture_output=True, text=True)
                check("compiler: a step-1 zip passes the unreadable list on too", os.path.exists(os.path.join(qd, "Quick_Unreadable.csv")), True)
            fz0, *_ = fp.profile(src, os.path.join(t, "f0"), log=lambda *_: None, workers=2, system_info=False, tier=0)
            check("level 0 records no path", [r["path"] for r in rd(fz0, "Unreadable.csv")], [""])
        finally:
            os.chmod(locked, 0o755)
    else:
        print("SKIP - unreadable folder test needs a non-root user")
    put(src, "Alpha/locked/secret.dwg", b"zz")

    # ---- cloud-tiered files: counted, never opened
    real_attrs = fp._file_attrs
    fp._file_attrs = lambda st: 0x400000 if st.st_size == 1234 else 0          # RECALL_ON_DATA_ACCESS (Azure File Sync tiered file)
    put(src, "Beta/tiered_big.dwg", b"T" * 1234)
    put(src, "Beta/tiered_part.sldprt", b"U" * 1234)
    real_open, opened = builtins.open, []
    def guard(path, *a, **k):
        if os.path.basename(str(path)).startswith("tiered_"):
            opened.append(str(path))
        return real_open(path, *a, **k)
    builtins.open = guard
    try:
        tz, *_ = fp.profile(src, os.path.join(t, "ft"), log=lambda *_: None, workers=2, system_info=False, tier=2)
        qz3, *_ = fp.quick_scan(src, os.path.join(t, "q3"), log=lambda *_: None)
    finally:
        builtins.open = real_open
        fp._file_attrs = real_attrs
    check("step 2: tiered files are never opened", opened, [])
    rows = {r["rel_path"].split("/")[-1]: r for r in rd(tz, "Files.csv")}
    check("they are still listed, with their size and a TIERED tag", (rows["tiered_big.dwg"]["size_bytes"], rows["tiered_big.dwg"]["header_signature"], rows["tiered_part.sldprt"]["header_signature"]), ("1234", "TIERED", "TIERED"))
    check("a tiered drawing gets a clear status instead of a sheet count", (rows["tiered_big.dwg"]["sheets"], rows["tiered_big.dwg"]["sheets_status"]), ("", "not-read-tiered"))
    check("Run_info reports them (step 2 and step 1)", (info(tz)["cloud_tiered_files_not_opened"], info(qz3)["cloud_tiered_files"]), ("2", "2"))

    # ---- step 2's counting pass is the step-1 engine: same files, same bytes
    fz3, *_ = fp.profile(src, os.path.join(t, "f3"), log=lambda *_: None, workers=2, system_info=False, tier=0)
    key = lambda z: {(r["project"], r["ext"]): (int(r["file_count"]), int(r["total_bytes"])) for r in rd(z, "Extensions.csv")}
    qz4, *_ = fp.quick_scan(src, os.path.join(t, "q4"), log=lambda *_: None)
    check("step 1 and step 2 count the same files and bytes per project and type", key(qz4) == key(fz3), True)
    check("step 2 scan phase is much like step 1: same totals in Run_info", info(fz3)["files"], str(info(qz4)["files"]))
sys.exit(1 if any(bad) else 0)
