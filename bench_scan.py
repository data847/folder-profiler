"""Step 2 benchmark for a small / old machine. Standard library only; works on Windows, macOS and Linux.

    python bench_scan.py <folder> [--old-pc] [--workers N] [--mem-mb N] [--out DIR]

--old-pc  : pretend to be a weak machine (2 reader threads, 256 MB read budget) on any computer.
Writes bench_result.json next to the workbook. It holds machine facts, timings, peak memory and counts only; no file or folder
names. Send that one file back. The workbook itself is not needed.
"""
import argparse
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--old-pc", action="store_true")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--mem-mb", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(HERE, "bench_output"))
    a = ap.parse_args()
    if a.old_pc:
        a.workers = a.workers or 2
        a.mem_mb = a.mem_mb or 256
    if a.mem_mb:
        os.environ["PROFILER_MEM_BUDGET_MB"] = str(a.mem_mb)

    import cad_scan
    import file_profiler
    import sysmetrics

    phases, t0 = {}, time.time()

    def log(msg):
        now = round(time.time() - t0, 1)
        print(f"[{now:7.1f}s] {msg}")
        if msg.startswith("Read "):
            phases["read_done"] = now
        elif msg.startswith("Checking"):
            phases["check_start"] = now
        elif msg.startswith("Writing"):
            phases["write_start"] = now

    workers = a.workers or file_profiler.default_workers()
    path, projects, files, problems = cad_scan.scan(a.folder, a.out, log=log, host=file_profiler, workers=workers)
    total = round(time.time() - t0, 1)
    ram = sysmetrics.total_ram_bytes()
    res = {
        "os": platform.platform(), "python": sys.version.split()[0], "machine": platform.machine(),
        "cpus": os.cpu_count(), "ram_gb": round(ram / 2 ** 30, 1) if ram else None,
        "workers": workers, "read_budget_mb": file_profiler.memory_budget() // 2 ** 20, "old_pc_mode": a.old_pc,
        "projects": projects, "files": files, "not_checked": problems,
        "seconds_total": total, "seconds_read": phases.get("read_done"),
        "seconds_check": round(phases.get("write_start", total) - phases.get("check_start", total), 1),
        "peak_memory_mb": sysmetrics.peak_memory_mb(),
        "workbook_mb": round(os.path.getsize(path) / 2 ** 20, 1),
    }
    out = os.path.join(a.out, "bench_result.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print("\n" + json.dumps(res, indent=2))
    print("\nResult file:", out)


if __name__ == "__main__":
    main()
