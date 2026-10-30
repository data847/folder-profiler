"""Read problems (Read_Problems.csv, per-file reasons) together with pipelined chunks: unreadable files spread over many small chunks are all
listed once with their file number and a reason, the saved per-chunk problem files survive a resume, and the run counts agree."""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(z.read(n).decode("utf-8-sig"))))
can_chmod = hasattr(os, "geteuid") and os.geteuid() != 0 and sys.platform != "win32"
if not can_chmod:
    print("skipped (needs a non-root POSIX user)"); sys.exit(0)
quiet = dict(log=lambda *_: None)
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "src"); locked = []
    for p in range(3):
        for i in range(40):
            d = os.path.join(src, f"proj{p}", f"s{i % 4}"); os.makedirs(d, exist_ok=True)
            f = os.path.join(d, f"f{i}.dwg"); open(f, "wb").write(b"AC1027" + b"\0" * 200)
            if i % 7 == 3:
                os.chmod(f, 0); locked.append(f)
    try:
        for chunk in (5, 1000):
            for pipe in (0, 4):
                fp.PIPELINE_CHUNKS = pipe
                out = os.path.join(t, f"o_{chunk}_{pipe}")
                z, *_ = fp.profile(src, out, workers=3, chunk_files=chunk, system_info=False, tier=2, **quiet)
                zf = zipfile.ZipFile(z); rp = rd(zf, "Read_Problems.csv")
                info = {r["key"]: r["value"] for r in rd(zf, "Run_info.csv")}
                files = rd(zf, "Files.csv"); unread = [r for r in files if "UNREADABLE" in r["header_signature"]]
                tag = f"chunk={chunk} pipeline={pipe}"
                check(f"{tag}: every unreadable file is listed once", (len(rp), len(unread), int(info["read_problems_listed"])), (len(locked), len(locked), len(locked)))
                check(f"{tag}: each row names its file and a reason", all(r["file_no"] and r["reason"] for r in rp), True)
                check(f"{tag}: problem file numbers are exactly the unreadable rows of Files.csv", sorted((r["project"], r["file_no"]) for r in rp), sorted((r["project"], r["file_no"]) for r in unread))
        fp.PIPELINE_CHUNKS = 4
        out = os.path.join(t, "o_resume")
        fp.profile(src, out, workers=2, chunk_files=5, system_info=False, tier=2, **quiet)
        z, *_ = fp.profile(src, out, workers=2, chunk_files=5, system_info=False, tier=2, **quiet)          # second run: resumes, everything saved
        zf = zipfile.ZipFile(z)
        check("after a resume the problems saved by the first run are still all listed", len(rd(zf, "Read_Problems.csv")), len(locked))
    finally:
        for f in locked: os.chmod(f, 0o644)
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
