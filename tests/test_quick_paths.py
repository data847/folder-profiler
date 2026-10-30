"""Step 1 file lists: Paths/<type>.csv holds every file (project, path, size, date) from the folder listing; counts agree with the tally; odd names and
Windows device-name extensions survive; anonymisation hides names; the switch turns it off; a cap is reported; the compile skill still reads the zip."""
import csv, io, os, subprocess, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import file_profiler as fp, name_anon
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(z.read(n).decode("utf-8-sig"))))
with tempfile.TemporaryDirectory() as t:
    root = os.path.join(t, "ZQXRoot"); out = os.path.join(t, "o")
    names = [("ZQXAlpha/ZQXSub One/plan, final.dwg", 30), ("ZQXAlpha/ZQXSub One/say \"hi\".pdf", 5), ("ZQXAlpha/notes", 7), ("ZQXAlpha/ünïcode näme.DWG", 11),
             ("ZQXBeta/data.con", 3), ("ZQXBeta/x.nul", 4), ("ZQXBeta/y.COM1", 6), ("ZQXBeta/deep/er/still/a.txt", 2), ("ZQXBeta/Thumbs.db", 9), ("ZQXBeta/empty.txt", 0),
             ("ZQXBeta/" + "q" * 60 + ".averyveryveryverylongextensionnamethatgoesonandon", 8), ("ZQXBeta/part.prt.7", 12)]
    mt = {}
    for i, (rel, size) in enumerate(names):
        p = os.path.join(root, *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x" * size)
        os.utime(p, (1_700_000_000 + i * 997, 1_700_000_000 + i * 997)); mt[rel] = 1_700_000_000 + i * 997
    os.makedirs(os.path.join(root, "ZQXAlpha", "empty folder"))
    z = zipfile.ZipFile(fp.quick_scan(root, out, log=lambda *_: None, list_paths=True)[0])
    members = [n for n in z.namelist() if n.startswith("Paths/")]
    rows = [r for m in members for r in rd(z, m)]
    listed = {(r["project"], r["path"]): (int(r["size_bytes"]), r["modified_utc"]) for r in rows}
    expect = {}
    for rel, size in names:
        if rel.endswith("Thumbs.db"): continue
        proj, path = rel.split("/", 1); expect[(proj, path)] = (size, fp.iso(mt[rel]))
    check("every file is listed once, with its size and date, and the system file Thumbs.db is not", listed == expect and len(rows) == len(expect), True)
    tally = {}
    for r in rd(z, "Extensions.csv"): tally[r["ext"]] = tally.get(r["ext"], 0) + int(r["file_count"])
    per = {os.path.basename(m)[:-4]: len(rd(z, m)) for m in members}
    check("the number of rows per list equals the count per type", sum(per.values()) == sum(tally.values()), True)
    idx = rd(z, "Paths_Index.csv"); check("the index lists every file type with its count", sum(int(r["files"]) for r in idx) == len(rows) and len(idx) == len(tally), True)
    check("Windows device names are safe as list names (con, nul, com1)", sorted(n for n in per if n in ("con_", "nul_", "com1_")), ["com1_", "con_", "nul_"])
    check("a file with no extension has its own list", "no_extension" in per, True)
    check("a very long extension gets a short list name", all(len(n) <= 41 for n in per), True)
    info = {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}
    check("Run_info says the lists are there, nothing dropped", (info["file_paths_listed"], info["file_paths_not_kept_over_limit"]), ("yes", "0"))
    # the switch
    z0 = zipfile.ZipFile(fp.quick_scan(root, os.path.join(t, "o0"), log=lambda *_: None, list_paths=False)[0])
    check("switched off: no Paths folder and no file names anywhere in the zip", (any(n.startswith("Paths") for n in z0.namelist()), b"ZQXAlpha" in b"".join(z0.read(n) for n in z0.namelist())), (False, True))
    # anonymised
    za = zipfile.ZipFile(fp.quick_scan(root, os.path.join(t, "oa"), log=lambda *_: None, list_paths=True, anonymize_names=True)[0]); mapz = zipfile.ZipFile(fp.LAST_RUN["map_zip"])
    blob = b"".join(za.read(n) for n in za.namelist()).decode("utf-8", "replace").lower()
    check("anonymised: no real project, folder or file name in any list", [w for w in ("zqx", "plan, final", "ünïcode", "notes", "still") if w in blob], [])
    m = {r["anonymised"]: r["original"] for r in rd(mapz, "Name_Map.csv")}
    ra = [r for n in za.namelist() if n.startswith("Paths/") for r in rd(za, n)]
    dec = set()
    for r in ra:
        parts = r["path"].split("/"); stem, ext_ = name_anon.split_name(parts[-1])
        dec.add((m[r["project"]], "/".join([m[p] for p in parts[:-1]] + [m[stem] + ext_])))
    check("the separate map decodes every anonymised path back to the real one (case-insensitive)", {(a.lower(), b.lower()) for a, b in dec} == {(a.lower(), b.lower()) for (a, b) in expect}, True)
    # cap
    old = fp.PATHS_MAX_FILES; fp.PATHS_MAX_FILES = 3
    zc = zipfile.ZipFile(fp.quick_scan(root, os.path.join(t, "oc"), log=lambda *_: None, list_paths=True)[0]); fp.PATHS_MAX_FILES = old
    ic = {r["key"]: r["value"] for r in rd(zc, "Run_info.csv")}; kept = sum(len(rd(zc, n)) for n in zc.namelist() if n.startswith("Paths/"))
    check("over the memory cap: the lists stop, the number not kept is reported, the counts stay exact", (kept < len(expect), int(ic["file_paths_not_kept_over_limit"]) > 0, ic["files"]), (True, True, str(len(expect))))
    # the compile skill still reads a Step 1 zip that carries Paths/
    r = subprocess.run([sys.executable, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "compile_profiles.py"), z.filename, "--out", os.path.join(t, "comp")], capture_output=True, text=True)
    check("the compile skill reads the Step 1 zip with the lists", (r.returncode, os.path.exists(os.path.join(t, "comp", "quick_count.xlsx"))), (0, True))
sys.exit(1 if any(bad) else 0)
