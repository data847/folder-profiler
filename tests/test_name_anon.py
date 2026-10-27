"""Optional name anonymisation (step 1 and step 2): nothing identifying survives in the shared zips, the separate map decodes every path
exactly, counts are identical to a plain run, references still resolve, and the source files are untouched."""
import csv, hashlib, io, os, stat, sys, tempfile, time, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import file_profiler as fp, name_anon
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.reader(io.StringIO(z.read(n).decode("utf-8-sig"))))

# ---- unit: the naming rules
check("split: plain / dotted / versioned / none", [name_anon.split_name(x) for x in ("a.dwg", "a.b.dwg", "part.prt.7", "notes", "v1.2 final")],
      [("a", ".dwg"), ("a.b", ".dwg"), ("part", ".prt.7"), ("notes", ""), ("v1.2 final", "")])
nm = name_anon.NameMap("ab12")
check("same name, any case, any folder -> one code; extension kept", (nm.file("Gear.SLDPRT"), nm.file("gear.sldprt"), nm.file("Gear.pdf")),
      ("F-ab12-000001.SLDPRT", "F-ab12-000001.sldprt", "F-ab12-000001.pdf"))
check("placeholders are not renamed", (nm.project("(root)"), nm.folder("")), ("(root)", ""))

SENT = ["ZQXClient One", "ZQXFolderSecret", "ZQXPartOne", "ZQXTop", "ZQXLeg", "ZQXSecondClient", "ZQXDeep", "ZQXLocked", "ZQXAsm"]
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "ZQXRootFolder"); out = os.path.join(t, "out"); out2 = os.path.join(t, "out_anon"); os.makedirs(out); os.makedirs(out2)
    def put(rel, data=b"x" * 100):
        p = os.path.join(src, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data)
    put("ZQXClient One/ZQXFolderSecret/ZQXPartOne.sldprt", b"p" * 500)
    put("ZQXClient One/ZQXFolderSecret/ZQXDeep/zqxpartone.PDF", b"d" * 300)          # same name, other case + folder + extension
    put("ZQXClient One/ZQXFolderSecret/ZQXLeg.sldprt", b"l" * 200)
    put("ZQXClient One/ZQXAsm.sldasm", b"not a real assembly" * 20)
    put("ZQXClient One/part.prt.7", b"c" * 150)                                          # Creo version suffix
    put("ZQXClient One/ZQXTop.step",
        b"ISO-10303-21;\nDATA;\n#1=PRODUCT('A','ZQXTop','',(#9));\n#2=PRODUCT('B','ZQXLeg','',(#9));\n#4=PRODUCT_DEFINITION_FORMATION('','',#1);\n"
        b"#5=PRODUCT_DEFINITION_FORMATION('','',#2);\n#10=PRODUCT_DEFINITION('d','',#4,#9);\n#11=PRODUCT_DEFINITION('d','',#5,#9);\n"
        b"#20=NEXT_ASSEMBLY_USAGE_OCCURRENCE('n1','','',#10,#11,$);\nENDSEC;\n")
    put("ZQXSecondClient/readme.txt", b"hello")
    put("loose-root-file.dwg", b"0" * 64)
    locked = os.path.join(src, "ZQXSecondClient", "ZQXLocked"); os.makedirs(locked); open(os.path.join(locked, "f.txt"), "w").write("x")
    os.chmod(locked, 0)
    def digest():
        h = hashlib.sha256()
        for dp, dn, fn in sorted(os.walk(src)):
            dn.sort()
            for f in sorted(fn):
                try: h.update(open(os.path.join(dp, f), "rb").read())
                except OSError: pass
            h.update(("|".join(sorted(dn))).encode())
        return h.hexdigest()
    before = digest()
    t0 = time.time(); plain = fp.profile(src, out, label="rootlabel", resume=False, system_info=False)[0]; t_plain = time.time() - t0
    t0 = time.time(); anon = fp.profile(src, out2, label="rootlabel", resume=False, system_info=False, anonymize_names=True)[0]; t_anon = time.time() - t0
    map_zip = fp.LAST_RUN["map_zip"]
    print(f"     (seconds: plain {t_plain:.2f}, anonymised {t_anon:.2f})")
    zp, za = zipfile.ZipFile(plain), zipfile.ZipFile(anon)
    check("source files and folders unchanged", digest(), before)
    check("the map is a SEPARATE zip, and the profile holds no map", (os.path.exists(map_zip), any("map" in n.lower() for n in za.namelist())), (True, False))
    stats = [f for f in os.listdir(out2) if f.startswith("run_stats_") and f.endswith(".zip")]
    leaks = []
    for name in za.namelist():
        blob = za.read(name).decode("utf-8", "replace").lower()
        for s in SENT + ["rootlabel"]:
            if s.lower() in blob: leaks.append((name, s))
    check("no real name anywhere in the anonymised profile zip", leaks, [])
    # the stats zip of the anonymised run: the newest one
    newest = os.path.join(out2, stats[0])
    sl = []
    for name in zipfile.ZipFile(newest).namelist():
        blob = zipfile.ZipFile(newest).read(name).decode("utf-8", "replace").lower()
        sl += [(name, s) for s in SENT + ["rootlabel"] if s.lower() in blob]
    check("no real name in the shareable run statistics either", sl, [])
    check("a plain run still has them (the test would be blind otherwise)", any("zqx" in zp.read(n).decode("utf-8", "replace").lower() for n in zp.namelist()), True)

    # ---- same numbers, row for row
    fa, fb = rd(za, "Files.csv"), rd(zp, "Files.csv")
    keep = lambda rows: [[c for i, c in enumerate(r) if i not in (0, 7, 8, 9)] for r in rows]
    check("every other column of Files.csv is identical (sizes, dates, tags, sheets)", keep(fa) == keep(fb) and len(fa) == len(fb), True)
    grp = lambda rows: sorted(sorted(i for i, r in enumerate(rows[1:]) if r[7] == d) for d in {r[7] for r in rows[1:]})
    check("dir_id (salted per run) groups the same files together in both runs", grp(fa) == grp(fb), True)
    sz = lambda z, n, col: sorted(r[col] for r in rd(z, n)[1:])
    check("Projects.csv counts equal", [r[1:] for r in rd(za, "Projects.csv")[1:]] == [r[1:] for r in rd(zp, "Projects.csv")[1:]], True)
    check("Extensions.csv counts equal", [r[1:] for r in rd(za, "Extensions.csv")[1:]] == [r[1:] for r in rd(zp, "Extensions.csv")[1:]], True)

    # ---- the map decodes every path exactly
    mp = zipfile.ZipFile(map_zip); rows = rd(mp, "Name_Map.csv")[1:]
    dec = {r[2].lower(): r[1] for r in rows}
    def decode_part(c):
        stem, ext = name_anon.split_name(c); return dec.get(stem.lower(), stem) + ext
    hdr = fa[0]; ip, ir = hdr.index("project"), hdr.index("rel_path")
    ok = all("/".join(decode_part(p) for p in a[ir].split("/")).lower() == b[ir].lower() and dec.get(a[ip].lower(), a[ip]).lower() == b[ip].lower() for a, b in zip(fa[1:], fb[1:]))
    check("decoding every project + rel_path with the map gives back the real ones", ok, True)
    check("map has projects, folders and files", sorted({r[0] for r in rows}), ["file (name without extension)", "folder", "project", "root folder"])
    # ---- structure kept
    code = lambda n: next(r[2] for r in rows if r[1].lower() == n.lower())
    byfile = {r[ir].split("/")[-1]: r for r in fa[1:]}
    check("ZQXPartOne.sldprt and zqxpartone.PDF share one stem code, extensions kept",
          sorted(k for k in byfile if k.startswith(code("ZQXPartOne"))), [code("ZQXPartOne") + ".PDF", code("ZQXPartOne") + ".sldprt"])
    check("Creo version suffix kept", any(k.endswith(".prt.7") for k in byfile), True)
    check("loose file in the root keeps '(root)'", any(r[ip] == "(root)" for r in fa[1:]), True)
    la = rd(za, "Links.csv")[1:]
    check("STEP structure: product names became the SAME codes as the files with those names", (code("ZQXTop"), code("ZQXLeg")) in {(r[4], r[5]) for r in la if r[3] == "step"}, True)
    ua = rd(za, "Unreadable.csv")[1:]
    check("unreadable folder listed with a coded path", [(r[1], "ZQX" in r[2]) for r in ua], [("folder", False)] if ua else [])
    check("Run_info says so", {r[0]: r[1] for r in rd(za, "Run_info.csv")}["names_anonymized"], "yes")
    info = {r[0]: r[1] for r in rd(za, "Run_info.csv")}
    check("root folder name replaced", info["folder_name"].startswith("ROOT-"), True)

    # ---- the compiler reads the anonymised zip and gets the same numbers
    import compile_profiles as cp
    em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    ca = cp.compile_runs(cp.load_profiles([anon]), em, sf, False, "auto", True, "folder")[0]
    cb = cp.compile_runs(cp.load_profiles([plain]), em, sf, False, "auto", True, "folder")[0]
    check("compiled workbook rows: same numbers, only the names differ", [r[4:9] for r in ca] == [r[4:9] for r in cb] and len(ca) == len(cb), True)

    # ---- step 1
    q_plain = fp.quick_scan(src, out)[0]
    q_anon = fp.quick_scan(src, out2, anonymize_names=True)[0]
    zq, zqa = zipfile.ZipFile(q_plain), zipfile.ZipFile(q_anon)
    ql = [(n, s) for n in zqa.namelist() for s in SENT + ["rootlabel", "ZQXRootFolder"] if s.lower() in zqa.read(n).decode("utf-8", "replace").lower()]
    check("step 1: no real name in the anonymised quick count", ql, [])
    check("step 1: counts equal to the plain quick count", [r[1:] for r in rd(zqa, "Projects.csv")[1:]] == [r[1:] for r in rd(zq, "Projects.csv")[1:]], True)
    check("step 1: separate map zip decodes the project codes", sorted(r[1] for r in rd(zipfile.ZipFile(fp.LAST_RUN["map_zip"]), "Name_Map.csv")[1:] if r[0] == "project"),
          sorted(r[0] for r in rd(zq, "Projects.csv")[1:] if r[0] != "(root)"))
    check("step 1: the anonymised Projects.csv holds those codes", sorted(r[0] for r in rd(zqa, "Projects.csv")[1:] if r[0] != "(root)"),
          sorted(r[2] for r in rd(zipfile.ZipFile(fp.LAST_RUN["map_zip"]), "Name_Map.csv")[1:] if r[0] == "project"))
    os.chmod(locked, stat.S_IRWXU)
sys.exit(1 if any(bad) else 0)
