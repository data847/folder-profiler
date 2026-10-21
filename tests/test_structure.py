"""Structure-based grouping: reference resolution, duplicates, shared parts, unresolved refs, confidence, root-assembly split.
Run: python tests/test_structure.py"""
import csv, io, os, struct, sys, tempfile, zipfile, zlib
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

with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    P = lambda rel, body=b"x": put(src, "P/" + rel, body)
    # A (root) -> Sub (assembly), p1, p2, lib, gear, bolt          Sub -> p3
    P("A/A.sldasm", sw_file(["Sub.sldasm", "p1.sldprt", "p2.sldprt", "lib.sldprt", "gear.sldprt", "bolt.sldprt"]))
    P("A/Sub/Sub.sldasm", sw_file(["p3.sldprt"])); P("A/Sub/p3.sldprt", "p3 data")
    P("A/p1.sldprt", "p1 data"); P("A/p2.sldprt", "p2 data!"); P("A/gear.sldprt", "same gear"); P("A/bolt.sldprt", "bolt-from-A")
    P("A/p1.slddrw", sw_file(["p1.sldprt"]))                         # drawing that references p1
    # B (root) -> p4, lib, gear, bolt, ghost (missing)
    P("B/B.sldasm", sw_file(["p4.sldprt", "lib.sldprt", "gear.sldprt", "bolt.sldprt", "ghost.sldprt"]))
    P("B/p4.sldprt", "p4 data"); P("B/gear.sldprt", "same gear"); P("B/bolt.sldprt", "bolt-from-B-longer"); P("B/orphan.sldprt", "orphan data")
    P("Lib/lib.sldprt", "library part")                              # shared by A and B
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2, hash_mode="all")
    runs = cp.load_profiles([z]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, _f, _s, review, asm, ev = cp.compile_runs(runs, ext_map, soft, False, "auto", True)
    rows = {r[1]: r for r in asm}
    check("three assemblies", sorted(rows), ["A.sldasm", "B.sldasm", "Sub.sldasm"])
    check("A: p1, p2, own gear, own bolt (+1 drawing following p1)", tuple(rows["A.sldasm"][2:]), (4, 1, 0, 0))
    check("Sub: p3", tuple(rows["Sub.sldasm"][2:]), (1, 0, 0, 0))
    check("B: p4, shared lib (most specific), own gear, own bolt, + folder-placed orphan", tuple(rows["B.sldasm"][2:]), (5, 0, 0, 0))
    check("rows sum to the project's part total (10)", sum(r[2] for r in asm), 10)
    p = ev["projects"][0]
    check("confidence High (9 of 10 parts by reference, 11 of 12 references resolve)", (p["confidence"], p["parts_link"], p["parts"], p["refs_resolved"], p["refs_total"]), ("High", 9, 10, 11, 12))
    check("unresolved reference reported by name", ("ghost.sldprt" in p["unresolved_examples"], p["refs_unresolved"]), (True, 1))
    check("shared library part is counted once and reported", p["shared_parts"], 1)
    check("orphan placed by folder, not by reference", p["parts_folder"], 1)
    check("copies in own folders are NOT ambiguous (nearest copy wins)", p["refs_ambiguous"], 0)
    evrow = {e["assembly"]: e for e in ev["rows"]}
    check("evidence: B has 5 references, 4 resolved, 1 unresolved", (evrow["B.sldasm"]["refs_total"], evrow["B.sldasm"]["refs_resolved"], evrow["B.sldasm"]["refs_unresolved"]), (5, 4, 1))
    # ---- split into sub-projects by root assembly
    c2, f2, s2, rev2, asm2, ev2 = cp.compile_runs(runs, ext_map, soft, False, "auto", True, "structure", 1)
    names = sorted(c[0] for c in c2)
    check("structure split: one sub-project per root assembly", names, ["P / A", "P / B"])
    pc = {c[0]: c for c in c2}
    check("P / A: parts 4+1 p1,p2,gear,bolt,p3 = 5 parts; assemblies A+Sub = 2; 1 drawing", (pc["P / A"][4], pc["P / A"][5], pc["P / A"][6]), (5, 2, 1))
    check("P / B: lib + p4 + gear + bolt + orphan = 5 parts, 1 assembly", (pc["P / B"][4], pc["P / B"][5]), (5, 1))
    check("no file lost in the split", sum(c[4] + c[5] + c[6] for c in c2), 10 + 3 + 1)
    check("split is reported in Review", any(r[1] == "project split" for r in rev2), True)
    # min size merges tiny groups
    c3, *_ = cp.compile_runs(runs, ext_map, soft, False, "auto", False, "structure", 8)
    check("small sub-projects merge into (other)", sorted(c[0] for c in c3), ["P / (other)", "P / A"])
    # level < 2 -> structure split does not apply
    z1, *_ = fp.profile(src, os.path.join(t, "out1"), log=lambda *_: None, workers=1, tier=1)
    c4, *_ = cp.compile_runs(cp.load_profiles([z1]), ext_map, soft, False, "auto", True, "structure", 1)
    check("level-1 profile: split not applicable, folder project kept", [c[0] for c in c4], ["P"])
    # level 1 confidence is Low and says why
    _c, _f, _s, _r, _a, ev1 = cp.compile_runs(cp.load_profiles([z1]), ext_map, soft, False, "auto", True)
    check("level 1: confidence Low (no references)", ev1["projects"][0]["confidence"], "Low")
# ---- a reference whose copies are equally near and different really is ambiguous (and is reported as such)
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "Q/C/C.sldasm", sw_file(["bolt.sldprt"])); put(src, "Q/X/bolt.sldprt", "bolt version one"); put(src, "Q/Y/bolt.sldprt", "bolt version TWO!!")
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2, hash_mode="all")
    runs = cp.load_profiles([z]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    *_x, ev = cp.compile_runs(runs, ext_map, soft, False, "auto", True)
    check("equidistant differing copies -> ambiguous reference counted", (ev["projects"][0]["refs_ambiguous"], ev["projects"][0]["refs_resolved"]), (1, 1))
# ---- split: a neutral file named like a part follows the NEAREST same-named part, not the first root that claimed the name
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "S/R1/R1.sldasm", sw_file(["m.sldprt"])); put(src, "S/R1/m.sldprt", "model one")
    put(src, "S/R2/R2.sldasm", sw_file(["m.sldprt"])); put(src, "S/R2/m.sldprt", "model 2 longer"); put(src, "S/R2/m.step", "ISO-10303-21;\nEND-ISO-10303-21;\n")
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2, hash_mode="all")
    runs = cp.load_profiles([z]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    c, *_x = cp.compile_runs(runs, ext_map, soft, False, "auto", False, "structure", 1)
    got = {r[0]: r[8] for r in c}                          # Neutral 3D Files per sub-project
    check("split: neutral file lands in the group of its nearest same-named part", got, {"S / R1": 0, "S / R2": 1})
# ---- defaults of the command line: structure split + assembly breakdown ON; each can be turned off; guard on poor resolution
import subprocess
SCRIPT = os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py")
def cli(zip_path, *flags):
    out = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, SCRIPT, zip_path, "--out", out, *flags], capture_output=True, text=True)
    pc = list(csv.reader(open(os.path.join(out, "Project_Compiled.csv"), encoding="utf-8-sig")))[1:]
    return sorted(x[0] for x in pc), os.path.exists(os.path.join(out, "Assembly_Breakdown_ESTIMATE.csv")), open(os.path.join(out, "Review.csv"), encoding="utf-8-sig").read(), r
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "D/R1/R1.sldasm", sw_file(["a.sldprt"])); put(src, "D/R1/a.sldprt", "aaa" * 20)
    put(src, "D/R2/R2.sldasm", sw_file(["b.sldprt"])); put(src, "D/R2/b.sldprt", "bbb" * 21)
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2)
    names, has_asm, rev, _r = cli(z, "--min-project-files", "1")
    check("plain command: splits by structure AND fills the Assembly Breakdown", (names, has_asm), (["D / R1", "D / R2"], True))
    names, has_asm, rev, _r = cli(z, "--min-project-files", "1", "--project-by", "folder", "--no-assembly-breakdown")
    check("--project-by folder --no-assembly-breakdown restores the old behaviour", (names, has_asm), (["D"], False))
    names, has_asm, rev, _r = cli(z, "--min-project-files", "1", "--estimate-assemblies")
    check("old --estimate-assemblies flag still accepted", (names, has_asm), (["D / R1", "D / R2"], True))
    # guard: assemblies whose references mostly do not resolve are not trusted to define projects
    put(src, "G/R1/R1.sldasm", sw_file(["g1.sldprt", "x1.sldprt", "x2.sldprt", "x3.sldprt"])); put(src, "G/R1/g1.sldprt", "g1")
    put(src, "G/R2/R2.sldasm", sw_file(["g2.sldprt", "y1.sldprt", "y2.sldprt", "y3.sldprt"])); put(src, "G/R2/g2.sldprt", "g2 g2")
    z, *_ = fp.profile(src, os.path.join(t, "out2"), log=lambda *_: None, workers=1, tier=2)
    names, has_asm, rev, _r = cli(z, "--min-project-files", "1")
    check("poor resolution (2 of 8 references): folder kept, reason reported", ("G" in names, any("G / R1" in n for n in names), "only 2 of 8 references resolve" in rev), (True, False, True))
# ---- edge cases found by review (each of these was wrong before)
import stat
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")
    put(src, "P/A/A.sldasm", sw_file(["g.sldprt", "R1.sldprt"])); put(src, "P/A/g.sldprt", "g"); put(src, "P/A/R1.sldprt", "rr")
    put(src, "Q/Gear.sldasm", sw_file(["Gear.sldprt", "bolt.sldprt"])); put(src, "Q/Gear.sldprt", "gp"); put(src, "Q/bolt.sldprt", "b")
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2, hash_mode="all")
    runs = cp.load_profiles([z]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    *_x, ev = cp.compile_runs(runs, ext_map, soft, False, "auto", True)
    e = {x["project"]: x for x in ev["projects"]}
    check("a part named only 'R1' no longer breaks linking for the whole project", (e["P"]["parts_link"], e["P"]["parts"]), (2, 2))
    check("an assembly keeps its reference to a base part with the same name (Gear.sldasm -> Gear.sldprt)", (e["Q"]["refs_total"], e["Q"]["parts_link"]), (2, 2))
    check("stem() never returns an empty name", [cp.stem(n) for n in ("R1.par", "v2.sldprt", "(1).par", "Copy.par")], ["r1", "v2", "(1)", "copy"])
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")                               # X <-> Y reference each other (a cycle); Z is independent
    put(src, "P/X/X.sldasm", sw_file(["Y.sldasm", "x.sldprt"])); put(src, "P/X/x.sldprt", "x")
    put(src, "P/Y/Y.sldasm", sw_file(["X.sldasm", "y.sldprt"])); put(src, "P/Y/y.sldprt", "yy")
    put(src, "P/Z/Z.sldasm", sw_file(["z.sldprt"])); put(src, "P/Z/z.sldprt", "zzz")
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2, hash_mode="all")
    runs = cp.load_profiles([z]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    c, *_x = cp.compile_runs(runs, ext_map, soft, False, "auto", False, "structure", 1)
    check("mutually referencing assemblies form ONE project; independent Z is separate", sorted(x[0] for x in c), ["P / X", "P / Z"])
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent")                               # Alpha holds a stale reference to Beta, which has its own parts
    put(src, "P/Alpha/Alpha.sldasm", sw_file(["a.sldprt", "Beta.sldasm"])); put(src, "P/Alpha/a.sldprt", "a")
    put(src, "P/Beta/Beta.sldasm", sw_file(["b1.sldprt", "b2.sldprt", "b3.sldprt"]))
    for n, body in (("b1", "x1"), ("b2", "x22"), ("b3", "x333")): put(src, f"P/Beta/{n}.sldprt", body)
    put(src, "P/Gamma/Gamma.sldasm", sw_file(["c.sldprt"])); put(src, "P/Gamma/c.sldprt", "cccc")
    z, *_ = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1, tier=2, hash_mode="all")
    runs = cp.load_profiles([z]); ext_map, soft = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    c, _f, _s, rev, *_x = cp.compile_runs(runs, ext_map, soft, False, "auto", False, "structure", 1)
    check("folded-in assembly is WARNED about (possible stale / library reference)", any("Beta.sldasm (inside Alpha)" in r[2] for r in rev), True)
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "parent"); put(src, "P/ok/a.sldprt", "a"); put(src, "P/locked/b.sldprt", "b")
    os.chmod(os.path.join(src, "P", "locked"), 0)
    try:
        z, _np, nfiles, errs = fp.profile(src, os.path.join(t, "out"), log=lambda *_: None, workers=1)
        info = {r["key"]: r["value"] for r in csv.DictReader(io.StringIO(zipfile.ZipFile(z).read("Run_info.csv").decode("utf-8-sig")))}
        check("an unreadable folder is reported, not silently skipped", (info["unreadable_folders"], int(info["read_errors"]) >= 1), ("1", True))
    finally:
        os.chmod(os.path.join(src, "P", "locked"), 0o755)
sys.exit(1 if any(bad) else 0)
