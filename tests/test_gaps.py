"""The three gaps: (1) references into another folder, (2) archived files in the compiler, (3) copies of whole projects.
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
    z = profile(src, t, "o", tier=2, hash_mode="all")
    *_x, ev = comp(z, False, "auto", True)
    e = {x["project"]: x for x in ev["projects"]}["Proj"]
    check("reference to a part in another top-level folder now resolves (external), the missing one stays unresolved",
          (e["refs_total"], e["refs_resolved"], e["refs_external"], e["refs_unresolved"]), (3, 2, 1, 1))
    check("only the truly missing file is listed as unresolved", e["unresolved_examples"], "nowhere.sldprt")
    check("the external part is counted in ITS project, not double counted in Proj", (e["parts"], e["parts_link"]), (1, 1))

# ---------------------------------------------------------------- (2) archives in the compiler
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "P/Main.sldasm", sw_file(["a.sldprt", "b.sldprt"])); put(src, "P/a.sldprt", "part a" * 9)
    with zipfile.ZipFile(os.path.join(src, "P/bk.zip"), "w") as zf:
        zf.writestr("old/a.sldprt", "part a" * 9)                    # identical to the loose a.sldprt -> must not count twice
        zf.writestr("old/b.sldprt", "part b is only inside the zip")  # exists nowhere else -> counts
        zf.writestr("old/Sub.sldasm", sw_file(["b.sldprt"]))          # archived assembly with a reference to an archived part
    z = profile(src, t, "o", tier=2, hash_mode="all")
    c, f, s, rev, asm, ev = comp(z, False, "auto", True)
    pc = {r[0]: r for r in c}["P"]
    check("parts: loose a + archived b counted; archived duplicate of a NOT counted again", pc[4], 2)
    check("assemblies: loose Main + archived Sub", pc[5], 2)
    check("File_Level marks archive rows and the duplicate that was not counted",
          (sum(1 for r in f if r["archive"]), [r["name"] for r in f if r["adup"]]), (3, ["a.sldprt"]))
    check("Review explains the archive handling", any("found inside archives" in r[2] and "1 identical" in r[2] for r in rev), True)
    evp = ev["projects"][0]
    check("level 2 links work through the archive (Main -> a, b ; Sub -> b)", (evp["refs_total"], evp["refs_resolved"], evp["parts_link"]), (3, 3, 2))

# ---------------------------------------------------------------- (3) whole-project copies and cross-project duplicates
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    for i in range(30):                                             # distinct sizes: unique inside each project -> only a RUN-WIDE size check can hash them
        put(src, f"Orig/f{i}.sldprt", ("part %d " % i) * (i + 3))
    for i in range(25):
        put(src, f"Orig backup/f{i}.sldprt", ("part %d " % i) * (i + 3))   # identical copy of 25 files
    for i in range(5):
        put(src, f"Orig backup/new{i}.sldprt", "new only here " * (40 + i))
    z = profile(src, t, "o", tier=0)                                # default smart hashing
    files = list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read("Files.csv").decode("utf-8-sig"))))
    cp_rows = [r for r in files if r["project"] == "Orig backup" and r["ext"] == "sldprt"]
    check("smart hashing compares sizes across the WHOLE run (copy files are fingerprinted)", (sum(1 for r in cp_rows if r["sha256"]), len(cp_rows)), (25, 30))
    c, f, s, rev, asm, ev = comp(z, False, "auto", False)
    flagged = [r for r in rev if r[1] == "possible copy"]
    check("backup flagged as a probable copy of the original (83%)", (len(flagged), flagged[0][0] if flagged else None, "83%" in (flagged[0][2] if flagged else "")), (1, "Orig backup", True))
    st = {r[0]: r for r in s}
    check("Project_Stats names the original and the share", (st["Orig backup"][-2], st["Orig backup"][-1], st["Orig"][-2]), ("Orig", "83%", ""))
    names, rv = cli(z); check("default: copy is flagged but still counted (nothing silently dropped)", sorted(r[0] for r in names), ["Orig", "Orig backup"])
    names, rv = cli(z, "--copies", "exclude"); check("--copies exclude drops it from Project Compiled", sorted(r[0] for r in names), ["Orig"])
    n_all = {r[0]: int(r[4]) for r in cli(z, "--unique", "--unique-scope", "all")[0]}
    n_proj = {r[0]: int(r[4]) for r in cli(z, "--unique")[0]}
    check("--unique (per project): both projects keep their parts", n_proj, {"Orig": 30, "Orig backup": 30})
    check("--unique --unique-scope all: shared content counted once in the whole profile", n_all, {"Orig": 30, "Orig backup": 5})
    names, rv = cli(z, "--copies", "keep"); check("--copies keep: no detection output", "possible copy" in rv, False)
sys.exit(1 if any(bad) else 0)
