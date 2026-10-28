"""Run and system metrics for the Folder Profiler. Standard library only, best effort: a failing probe is logged as
"unavailable", never raises into the run, and nothing identifying is recorded (no host name, user name or paths).

    system_info(...)  -> list of (key, value): OS, CPU, RAM, storage type/free space of the drive being read, antivirus state
    Monitor           -> background thread that samples CPU, memory and read throughput every few seconds into Run_metrics.csv
    RunLog            -> timestamped event log (Run_log.csv) that survives a crash (append + flush) and a restart (append)
    Activity          -> thread-safe "busy seconds" per activity (tag reading, link reading, sheet counting, writing ...)

Windows probes use ctypes (kernel32 / psapi / winreg) and PowerShell for disk type and Defender; Linux reads /proc; macOS uses
sysctl / ps / vm_stat. Probes that a platform cannot answer are left blank.
"""
import csv
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ------------------------------------------------------------------------------------ low-level probes
class _Win:
    """ctypes wrappers (only touched on Windows)."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)

        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        class IOC(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]
        self.FILETIME, self.MEMSTAT, self.PMC, self.IOC = FILETIME, MEMSTAT, PMC, IOC
        # 64-bit safe signatures: without argtypes a handle (a large Python int) is passed as a 32-bit int and raises, which made the
        # process memory / read-speed columns come out empty on a real Windows run
        self.k32.GetCurrentProcess.restype = wintypes.HANDLE
        self.k32.GetCurrentProcess.argtypes = []
        self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        self.k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.k32.GetProcessIoCounters.restype = wintypes.BOOL
        self.k32.GetSystemTimes.argtypes = [ctypes.c_void_p] * 3
        self.k32.GetSystemTimes.restype = wintypes.BOOL
        self.k32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        self.k32.GlobalMemoryStatusEx.restype = wintypes.BOOL

    @staticmethod
    def _ft(f):
        return (f.hi << 32) | f.lo

    def cpu_times(self):
        i, k, u = self.FILETIME(), self.FILETIME(), self.FILETIME()
        if not self.k32.GetSystemTimes(self.ct.byref(i), self.ct.byref(k), self.ct.byref(u)):
            return None
        idle, kern, user = self._ft(i), self._ft(k), self._ft(u)
        return idle, kern + user                      # kernel time already includes idle time

    def memory(self):
        m = self.MEMSTAT(); m.dwLength = self.ct.sizeof(self.MEMSTAT)
        if not self.k32.GlobalMemoryStatusEx(self.ct.byref(m)):
            return None
        return m.ullTotalPhys, m.ullAvailPhys, m.dwMemoryLoad

    def rss(self):
        p = self.PMC(); p.cb = self.ct.sizeof(self.PMC)
        h = self.k32.GetCurrentProcess()
        if not self.psapi.GetProcessMemoryInfo(h, self.ct.byref(p), p.cb):
            return None
        return p.WorkingSetSize

    def io_read(self):
        c = self.IOC()
        if not self.k32.GetProcessIoCounters(self.k32.GetCurrentProcess(), self.ct.byref(c)):
            return None
        return c.ReadTransferCount, c.ReadOperationCount

    def drive_type(self, root):
        names = {0: "unknown", 1: "no root", 2: "removable", 3: "fixed (internal)", 4: "network", 5: "optical", 6: "RAM disk"}
        return names.get(self.k32.GetDriveTypeW(root), "unknown")


_WIN = None


def _win():
    global _WIN
    if _WIN is None:
        try:
            _WIN = _Win()
        except Exception:
            _WIN = False
    return _WIN or None


def _linux_cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    return v[3] + (v[4] if len(v) > 4 else 0), sum(v)          # idle + iowait, total


def _linux_mem():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            d[k] = int(v.split()[0]) * 1024
    return d["MemTotal"], d.get("MemAvailable", d.get("MemFree", 0))


def _linux_io():
    d = {}
    with open("/proc/self/io") as f:
        for line in f:
            k, _, v = line.partition(":")
            d[k] = int(v)
    return d.get("rchar", 0), d.get("read_bytes", 0)


def _rss_bytes():
    try:
        if IS_WIN and _win():
            return _win().rss()
        if IS_LINUX:
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        out = _run(["ps", "-o", "rss=", "-p", str(os.getpid())], 5)
        return int(out) * 1024 if out else None
    except Exception:
        return None


def _mac_mem():
    total = int(_run(["sysctl", "-n", "hw.memsize"], 5) or 0)
    vm = _run(["vm_stat"], 5)
    if not total or not vm:
        return None
    page = int((re.search(r"page size of (\d+)", vm) or [0, 4096])[1])
    get = lambda name: int((re.search(name + r":\s+(\d+)", vm) or [0, 0])[1])
    avail = (get("Pages free") + get("Pages inactive") + get("Pages speculative")) * page
    return total, avail


# ------------------------------------------------------------------------------------ system information
def total_ram_bytes():
    """Installed RAM in bytes, or 0 if the system will not say."""
    try:
        if IS_WIN and _win():
            m = _win().memory()
            return int(m[0]) if m else 0
        if IS_LINUX:
            return int(_linux_mem()[0])
        if IS_MAC:
            m = _mac_mem()
            return int(m[0]) if m else 0
    except Exception:
        pass
    return 0


def system_info(data_paths=(), out_dir=".", extra=None, deep=True):
    """Static facts about the machine for the run report. `deep` also asks PowerShell about disks and antivirus on Windows."""
    rows = [("os", platform.platform()), ("machine", platform.machine()), ("python", platform.python_version()),
            ("python_build", platform.python_implementation()), ("logical_cpus", os.cpu_count() or ""),
            ("in_docker", "yes" if os.environ.get("PROFILER_IN_DOCKER") else "no")]
    cpu = ""
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        elif IS_MAC:
            cpu = _run(["sysctl", "-n", "machdep.cpu.brand_string"], 5)
        elif IS_LINUX:
            with open("/proc/cpuinfo") as f:
                cpu = next((l.split(":", 1)[1].strip() for l in f if l.lower().startswith("model name")), "")
    except Exception:
        pass
    rows.append(("cpu_model", cpu or platform.processor() or ""))
    try:
        if IS_WIN and _win():
            m = _win().memory(); total = m[0] if m else None
        elif IS_LINUX:
            total = _linux_mem()[0]
        elif IS_MAC:
            m = _mac_mem(); total = m[0] if m else None
        else:
            total = None
        rows.append(("ram_gb", f"{total / 1024 ** 3:.1f}" if total else ""))
    except Exception:
        rows.append(("ram_gb", ""))
    # storage: the drive(s) being read and the drive receiving the output
    seen = set()
    for label, p in [("data", d) for d in data_paths] + [("output", out_dir)]:
        try:
            p = os.path.abspath(p)
            drive = os.path.splitdrive(p)[0].upper() if IS_WIN else ""
            if (label, drive) in seen:
                continue
            seen.add((label, drive))
            tag = f"{label}_{drive}".rstrip("_").replace(":", "").replace("\\", "unc").replace("/", "_")
            unc = IS_WIN and (p.startswith("\\\\") or p.startswith("//"))
            if IS_WIN and _win():
                rows.append((f"{tag}_drive_type", "network (UNC)" if unc else _win().drive_type((drive + "\\") if drive else "")))
            try:
                st = os.statvfs(p) if hasattr(os, "statvfs") else None
                if st:
                    rows.append((f"{tag}_free_gb", f"{st.f_bavail * st.f_frsize / 1024 ** 3:.1f}"))
                else:
                    import shutil
                    rows.append((f"{tag}_free_gb", f"{shutil.disk_usage(p).free / 1024 ** 3:.1f}"))
            except Exception:
                pass
        except Exception:
            continue
    if IS_LINUX:
        try:
            rot = []
            for dev in sorted(os.listdir("/sys/block")):
                f = f"/sys/block/{dev}/queue/rotational"
                if os.path.exists(f) and not dev.startswith(("loop", "ram")):
                    rot.append(f"{dev}={'HDD' if open(f).read().strip() == '1' else 'SSD/flash'}")
            if rot:
                rows.append(("block_devices", "; ".join(rot[:8])))
        except Exception:
            pass
    if IS_WIN and deep:
        ps = lambda c: _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", c], 12)
        disks = ps("Get-PhysicalDisk | ForEach-Object { '{0} {1} {2} GB' -f $_.MediaType,$_.BusType,[math]::Round($_.Size/1GB) }")
        if disks:
            rows.append(("physical_disks", "; ".join(x.strip() for x in disks.splitlines() if x.strip())))
        rows.append(("defender_realtime_protection", ps("(Get-MpComputerStatus).RealTimeProtectionEnabled") or "unknown"))
        rows.append(("power_plan", (ps("(powercfg /getactivescheme)") or "unknown").split("(")[-1].rstrip(")")))
    for k, v in (extra or {}).items():
        rows.append((k, v))
    return rows


# ------------------------------------------------------------------------------------ logs
class RunLog:
    """Append-only event log (Run_log.csv). Flushed on every event, so a crash still leaves the story up to that point."""
    HEADER = ["timestamp_utc", "elapsed_s", "level", "phase", "message"]

    def __init__(self, path, t0=None, echo=None):
        self.path, self.t0, self.echo = path, t0 or time.time(), echo
        self.lock = threading.Lock()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.HEADER); self.f.flush()

    def event(self, phase, message, level="INFO"):
        with self.lock:
            self.w.writerow([now_utc(), f"{time.time() - self.t0:.1f}", level, phase, message]); self.f.flush()
        if self.echo:
            self.echo(f"  {message}")

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class Activity:
    """Thread-safe 'busy seconds' per named activity (summed over all worker threads) + a call counter."""
    def __init__(self):
        self.lock, self.sec, self.n = threading.Lock(), {}, {}

    def add(self, name, seconds, count=1):
        with self.lock:
            self.sec[name] = self.sec.get(name, 0.0) + seconds
            self.n[name] = self.n.get(name, 0) + count

    def timer(self, name):
        act = self

        class _T:
            def __enter__(s):
                s.t = time.perf_counter(); return s

            def __exit__(s, *a):
                act.add(name, time.perf_counter() - s.t)
        return _T()

    def snapshot(self):
        with self.lock:
            return dict(self.sec), dict(self.n)


class Monitor(threading.Thread):
    """Samples the machine every `interval` seconds into Run_metrics.csv (appended; survives restarts)."""
    HEADER = ["timestamp_utc", "elapsed_s", "phase", "files_done", "bytes_done_GB", "files_per_s", "MB_per_s_processed",
              "process_cpu_pct", "system_cpu_pct", "ram_used_pct", "process_memory_MB", "process_read_MB_per_s",
              "process_read_ops_per_s"]

    def __init__(self, path, state, interval=5.0, t0=None):
        super().__init__(daemon=True)
        self.path, self.state, self.interval, self.t0 = path, state, max(1.0, float(interval)), t0 or time.time()
        self.stop_ev = threading.Event()
        self.unavailable = set()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.HEADER); self.f.flush()

    def _sys_cpu(self):
        try:
            if IS_WIN and _win():
                return _win().cpu_times()
            if IS_LINUX:
                return _linux_cpu()
        except Exception:
            pass
        return None

    def _ram_pct(self):
        try:
            if IS_WIN and _win():
                m = _win().memory(); return float(m[2]) if m else None
            if IS_LINUX:
                t, a = _linux_mem(); return 100.0 * (1 - a / t)
            if IS_MAC:
                m = _mac_mem(); return 100.0 * (1 - m[1] / m[0]) if m else None
        except Exception:
            pass
        return None

    def _io(self):
        try:
            if IS_WIN and _win():
                r = _win().io_read(); return (r[0], r[1]) if r else None
            if IS_LINUX:
                rchar, rb = _linux_io(); return rb or rchar, None
        except Exception:
            pass
        return None

    def run(self):
        ncpu = os.cpu_count() or 1
        prev = dict(t=time.time(), cpu=time.process_time(), sys=self._sys_cpu(), io=self._io(),
                    files=self.state.get("files", 0), bytes=self.state.get("bytes", 0))
        while not self.stop_ev.wait(self.interval):
            try:
                self.sample(prev, ncpu)
            except Exception:
                pass

    def sample(self, prev, ncpu):
        t, cpu, sysc, io = time.time(), time.process_time(), self._sys_cpu(), self._io()
        files, nbytes = self.state.get("files", 0), self.state.get("bytes", 0)
        dt = max(1e-6, t - prev["t"])
        sys_pct = ""
        if sysc and prev["sys"]:
            didle, dtot = sysc[0] - prev["sys"][0], sysc[1] - prev["sys"][1]
            sys_pct = f"{100.0 * (1 - didle / dtot):.0f}" if dtot > 0 else ""
        io_mb = io_ops = ""
        if io and prev["io"]:
            io_mb = f"{(io[0] - prev['io'][0]) / dt / 1e6:.1f}"
            if io[1] is not None and prev["io"][1] is not None:
                io_ops = f"{(io[1] - prev['io'][1]) / dt:.0f}"
        ram = self._ram_pct(); rss = _rss_bytes()
        self.w.writerow([now_utc(), f"{t - self.t0:.0f}", self.state.get("phase", ""), files, f"{nbytes / 1e9:.3f}",
                         f"{(files - prev['files']) / dt:.0f}", f"{(nbytes - prev['bytes']) / dt / 1e6:.1f}",
                         f"{100.0 * (cpu - prev['cpu']) / dt / ncpu:.0f}", sys_pct,
                         f"{ram:.0f}" if ram is not None else "", f"{rss / 1e6:.0f}" if rss else "", io_mb, io_ops])
        self.f.flush()
        prev.update(t=t, cpu=cpu, sys=sysc, io=io, files=files, bytes=nbytes)

    def stop(self):
        self.stop_ev.set()
        try:
            self.join(timeout=self.interval + 2)
            self.f.close()
        except Exception:
            pass
