"""The three gaps: (1) references into another folder, (2) archive rows of older profiles in the compiler, (3) copies of whole projects.
Run: python tests/test_gaps.py"""
import csv, io, os, struct, subprocess, sys, tempfile, zipfile, zlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp
try:
    import compile_profiles as cp
except ImportError:
    print("SKIP - compiler not found next to this package"); sys.exit(0)

bad = []
def check(label, got, want):
    ok = got == want; print(("ok   " if ok else "FAIL ") + f"{label}: {got}" + ("" if ok else f"   (wanted {want})")); bad.append(not ok)
def sw_file(refs):
    payload = b"".join(("D:\\old\\" + r).encode("utf-16le") + b"\x00\x00" for r in refs)
    c = zlib.compressobj(wbits=-15); data = c.compress(payload) + c.flush()
    return b"w0\xbc\"" + b"\x00" * 40 + b"\x14\x00\x06\x00\x08\x00" + struct.pack("<IIII", 0, len(data), len(payload), 0) + data
def put(base, rel, body=b"x"):
    path = os.path.join(base, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(body if isinstance(body, bytes) else body.encode())
MAPDIR = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping")
SCRIPT = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py")
def profile(src, t, name, **kw):
    z, *_ = fp.profile(src, os.path.join(t, name), log=lambda *_: None, workers=1, **kw); return z
def comp(z, *a, **kw):
    em, sf = cp.load_mapping(MAPDIR); return cp.compile_runs(cp.load_profiles([z]), em, sf, *a, **kw)
def cli(z, *flags):
    out = tempfile.mkdtemp(); subprocess.run([sys.executable, SCRIPT, z, "--out", out, *flags], capture_output=True, text=True)
    rd = lambda n: list(csv.reader(open(os.path.join(out, n), encoding="utf-8-sig")))[1:]
    return rd("Project_Compiled.csv"), open(os.path.join(out, "Review.csv"), encoding="utf-8-sig").read()

# ---------------------------------------------------------------- (1) cross-folder references
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "Proj/Top.sldasm", sw_file(["own.sldprt", "std_bolt.sldprt", "nowhere.sldprt"])); put(src, "Proj/own.sldprt", "own part")
    put(src, "Library/std_bolt.sldprt", "standard bolt")
    z = profile(src, t, "o", tier=2)
    *_x, ev = comp(z, False, "auto", True)
    e = {x["project"]: x for x in ev["projects"]}["Proj"]
    check("reference to a part in another top-level folder now resolves (external), the missing one stays unresolved",
          (e["refs_total"], e["refs_resolved"], e["refs_external"], e["refs_unresolved"]), (3, 2, 1, 1))
    check("only the truly missing file is listed as unresolved", e["unresolved_examples"], "nowhere.sldprt")
    check("the external part is counted in ITS project, not double counted in Proj", (e["parts"], e["parts_link"]), (1, 1))

# ---------------------------------------------------------------- (2) profiles from the EARLIER profilers that looked inside archives
# The profiler no longer opens archives, but the compiler still understands profiles that carry archive rows (hand-built here).
with tempfile.TemporaryDirectory() as t:
    zp = os.path.join(t, "old_profile.zip")
    hdr = "project,file_no,ext,size_bytes,modified_utc,depth,header_signature,dir_id,asm_name,rel_path,archive\r\n"
    rows = ["P,1,sldasm,50,2024-01-01T00:00:00Z,1,OLE2,d1,Main.sldasm,Main.sldasm,",
            "P,2,sldprt,54,2024-01-01T00:00:00Z,1,BINARY,d1,,a.sldprt,",
            "P,3,sldprt,54,,2,ARCHIVED,d2,,bk.zip!/old/a.sldprt,zip",          # identical to the loose a.sldprt -> must not count twice
            "P,4,sldprt,29,,2,ARCHIVED,d2,,bk.zip!/old/b.sldprt,zip",          # exists nowhere else -> counts
            "P,5,sldasm,40,,2,ARCHIVED,d2,Sub.sldasm,bk.zip!/old/Sub.sldasm,zip"]
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("Files.csv", "\ufeff" + hdr + "\r\n".join(rows) + "\r\n")
        zf.writestr("Run_info.csv", "key,value\r\nfolder_name,old\r\n")
        zf.writestr("Projects.csv", "project,file_count,total_bytes,distinct_extensions,first_modified_utc,last_modified_utc\r\nP,5,227,2,,\r\n")
    c, f, s, rev, asm, ev = comp(zp, False, "auto", True)
    pc = {r[0]: r for r in c}["P"]
    check("parts: loose a + archived b counted; archived duplicate of a NOT counted again", pc[4], 2)
    check("assemblies: loose Main + archived Sub", pc[5], 2)
    check("File_Level marks archive rows and the duplicate that was not counted",
          (sum(1 for r in f if r["archive"]), [r["name"] for r in f if r["adup"]]), (3, ["a.sldprt"]))
    check("Review explains the archive handling", any("found inside archives" in r[2] and "1 identical" in r[2] for r in rev), True)

# ---------------------------------------------------------------- (3) whole-project copies and cross-project duplicates
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    for i in range(30):                                             # distinct sizes: unique inside each project -> only a RUN-WIDE size check can hash them
        put(src, f"Orig/f{i}.sldprt", ("part %d " % i) * (i + 3))
    for i in range(25):
        put(src, f"Orig backup/f{i}.sldprt", ("part %d " % i) * (i + 3))   # identical copy of 25 files
        st = os.stat(os.path.join(src, f"Orig/f{i}.sldprt"))
        os.utime(os.path.join(src, f"Orig backup/f{i}.sldprt"), (st.st_atime, st.st_mtime))   # a real copy keeps the modified date
    for i in range(5):
        put(src, f"Orig backup/new{i}.sldprt", "new only here " * (40 + i))
    for i in range(25):                                              # a second backup made by a tool that did NOT keep the dates
        put(src, f"Orig restored/f{i}.sldprt", ("part %d " % i) * (i + 3))
        st = os.stat(os.path.join(src, f"Orig/f{i}.sldprt"))
        os.utime(os.path.join(src, f"Orig restored/f{i}.sldprt"), (st.st_atime + 7200, st.st_mtime + 7200))   # new modified date
    z = profile(src, t, "o", tier=0)                                # no hashing: duplicates are found by size + modified date
    info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z).read("Run_info.csv").decode("utf-8-sig")))}
    check("the profile was made without hashing", info["hashing"], "none")
    c, f, s, rev, asm, ev = comp(z, False, "auto", False)
    flagged = [r for r in rev if r[1] == "possible copy"]
    check("backup that kept its dates is flagged as a probable copy of the original (83%)", ([r[0] for r in flagged], "83%" in flagged[0][2]), (["Orig backup"], True))
    check("KNOWN LIMIT: a copy whose dates were reset is not recognised without hashing (not flagged)", "Orig restored" in [r[0] for r in flagged], False)
    st = {r[0]: r for r in s}
    ci, si = cp.STATS_HEADER.index("possible_copy_of"), cp.STATS_HEADER.index("copy_share")
    check("Project_Stats names the original and the share", (st["Orig backup"][ci], st["Orig backup"][si], st["Orig"][ci]), ("Orig", "83%", ""))
    names, rv = cli(z); check("default: copy is flagged but still counted (nothing silently dropped)", sorted(r[0] for r in names), ["Orig", "Orig backup", "Orig restored"])
    names, rv = cli(z, "--copies", "exclude"); check("--copies exclude drops the flagged copy from Project Compiled", sorted(r[0] for r in names), ["Orig", "Orig restored"])
    n_all = {r[0]: int(r[4]) for r in cli(z, "--unique", "--unique-scope", "all")[0]}
    n_proj = {r[0]: int(r[4]) for r in cli(z, "--unique")[0]}
    check("--unique (per project): every project keeps its parts", n_proj, {"Orig": 30, "Orig backup": 30, "Orig restored": 25})
    check("--unique --unique-scope all: matching files counted once in the whole profile (reset dates stay counted)", n_all, {"Orig": 30, "Orig backup": 5, "Orig restored": 25})
    names, rv = cli(z, "--copies", "keep"); check("--copies keep: no detection output", "possible copy" in rv, False)
sys.exit(1 if any(bad) else 0)
