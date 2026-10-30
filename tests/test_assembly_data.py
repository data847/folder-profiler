"""Assembly Breakdown data (profiler 1.2): folder ids, folder tree, assembly names - and nothing else named.
Run: python tests/test_assembly_data.py"""
import csv, io, os, re, sys, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import file_profiler as fp

TOP, SUB, LOOSE, PART = "ZZTOPZZ", "ZZSUBZZ", "ZZLOOSEZZ", "ZZPARTNAMEZZ"
bad = []
def check(label, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {extra}" if extra and not ok else "")); bad.append(not ok)

def build(t):
    def put(rel, body="x"):
        path = os.path.join(t, "P", rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w").write(body)
    put(f"{TOP}/Top.sldasm"); put(f"{TOP}/{PART}1.sldprt"); put(f"{TOP}/{PART}2.sldprt"); put(f"{TOP}/d.slddrw")
    put(f"{TOP}/{SUB}/Sub.sldasm"); put(f"{TOP}/{SUB}/{PART}3.sldprt"); put(f"{LOOSE}/{PART}4.sldprt")

def run(t, anon):
    z, *_ = fp.profile(t, os.path.join(t, "out"), log=lambda *_: None, anonymize_assembly_names=anon, workers=1, tier=0)
    zf = zipfile.ZipFile(z)
    rd = lambda n: list(csv.DictReader(io.StringIO(zf.read(n).decode("utf-8-sig"))))
    return rd("Files.csv"), rd("Dirs.csv"), {r["key"]: r["value"] for r in rd("Run_info.csv")}, b"\n".join(zf.read(n) for n in zf.namelist())

with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
    build(t1); build(t2)
    files, dirs, info, blob = run(t1, anon=False)
    asm_rows = [r for r in files if r["asm_name"]]
    check("only assembly files carry a name", sorted(r["asm_name"] for r in asm_rows) == ["Sub.sldasm", "Top.sldasm"])
    check("non-assembly rows have an empty asm_name", all(r["asm_name"] == "" for r in files if r["ext"] != "sldasm"))
    check("no folder name or part file name anywhere", not any(m.encode() in blob for m in (TOP, SUB, LOOSE, PART)))
    check("Run_info says names are real", info["assembly_names"] == "real" and info["tool_version"] == "1.8")
    ids = {r["dir_id"] for r in files}
    check("every file's dir_id exists in Dirs.csv", ids <= {d["dir_id"] for d in dirs})
    by_id = {d["dir_id"]: d for d in dirs}
    top = next(r["dir_id"] for r in files if r["asm_name"] == "Top.sldasm"); sub = next(r["dir_id"] for r in files if r["asm_name"] == "Sub.sldasm")
    check("Sub's folder is a child of Top's folder", by_id[sub]["parent_dir_id"] == top)
    check("files in the same folder share a dir_id", len({r["dir_id"] for r in files if r["ext"] == "slddrw"} | {top}) == 1)
    check("project root folder has an empty parent", sum(1 for d in dirs if d["parent_dir_id"] == "") == 1)
    check("dir_id is 16 hex chars (64 bits: no collisions at millions of folders)", all(re.fullmatch(r"[0-9a-f]{16}", i) for i in ids))

    files_a, dirs_a, info_a, blob_a = run(t2, anon=True)
    names = sorted(r["asm_name"] for r in files_a if r["asm_name"])
    check("anonymised names look like ASM-XXXXXXXX.SLDASM", len(names) == 2 and all(re.fullmatch(r"ASM-[0-9A-F]{8}\.SLDASM", n) for n in names), str(names))
    check("anonymised run has no file or folder names at all", not any(m.encode() in blob_a for m in (TOP, SUB, LOOSE, PART, "Top.sldasm", "Sub.sldasm")))
    check("Run_info says names are anonymized", info_a["assembly_names"] == "anonymized")
    check("folder ids differ between runs (random salt, not guessable)", {r["dir_id"] for r in files_a}.isdisjoint(ids))
sys.exit(1 if any(bad) else 0)
