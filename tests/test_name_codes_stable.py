"""Name codes are the same on every run on one computer (Step 1 and Step 2 line up by code), unreadable without the key, unique inside a run,
and fall back to per-run codes (said so in Run_info) when no key can be kept."""
import csv, io, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
t0 = tempfile.mkdtemp()
KEY = os.path.join(t0, "keys", "name_key")
os.environ["PROFILER_NAME_KEY_FILE"] = KEY
import file_profiler as fp, name_anon
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.reader(io.StringIO(z.read(n).decode("utf-8-sig"))))

# ---- unit
key, where = name_anon.load_key(t0)
check("key made on first use, in the named file, 32 bytes, readable only by the owner", (where == KEY, len(key), oct(os.stat(KEY).st_mode & 0o777) if os.name != "nt" else "0o600"), (True, 32, "0o600"))
check("second load returns the same key", name_anon.load_key(t0)[0], key)
a, b = name_anon.NameMap(key=key), name_anon.NameMap(key=key)
names = [f"ZQXPart{i}.sldprt" for i in range(500)] + ["Gear.pdf", "gear.PDF"]
ca = [a.file(n) for n in names]; cb = [b.file(n) for n in reversed(names)][::-1]
check("same names, different order, different NameMap -> identical codes", ca, cb)
check("case-insensitive: Gear/gear share one code, extension kept", (a.file("Gear.pdf").rsplit(".", 1)[0] == a.file("gear.PDF").rsplit(".", 1)[0], a.file("gear.PDF").endswith(".PDF")), (True, True))
check("project, folder and file codes have their own shapes", (a.project("Client").startswith("P-" + a.run + "-"), len(a.project("Client").split("-")[2]), len(a.folder("Sub").split("-")[2]), len(a.file("x.dwg").split("-")[2].split(".")[0])), (True, 6, 8, 10))
other = name_anon.NameMap(key=bytes(32))
check("another key -> other id and other codes (a code cannot be recomputed without the key)", (other.run != a.run, other.file("Gear.pdf") != a.file("Gear.pdf")), (True, True))
check("run id derives from the key and is the same every time", name_anon.NameMap(key=key).run, a.run)
# a clash inside one run lengthens the later code, and the map still decodes both
HW = dict(name_anon.HEX_WIDTH); name_anon.HEX_WIDTH.update({"file": 1})
c = name_anon.NameMap(key=key); codes = [c.file(f"n{i}") for i in range(200)]
name_anon.HEX_WIDTH.update(HW)
dec = {r[2]: r[1] for r in c.rows()}
check("200 names squeezed into 1 hex digit: all codes still unique and decode back", (len(set(codes)) == 200, sorted(dec.values()) == sorted(f"n{i}" for i in range(200))), (True, True))
check("without a key the old per-run counter codes are used", (name_anon.NameMap().stable, name_anon.NameMap("ab12").file("x.dwg")), (False, "F-ab12-000001.dwg"))

# ---- the real thing: Step 1 and Step 2, two output folders, two moments
src = os.path.join(t0, "ZQXRoot"); out1, out2, out3 = (os.path.join(t0, d) for d in ("o1", "o2", "o3"))
for d in (out1, out2, out3): os.makedirs(d)
for rel, data in (("ZQXClientOne/ZQXSub/ZQXPart.sldprt", b"p" * 300), ("ZQXClientOne/ZQXAsm.sldasm", b"a" * 90), ("ZQXClientTwo/readme.txt", b"hi")):
    p = os.path.join(src, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data)
q = fp.quick_scan(src, out1, anonymize_names=True)[0]
s2 = fp.profile(src, out2, label="lbl", resume=False, system_info=False, anonymize_names=True)[0]
q2 = fp.quick_scan(src, out3, anonymize_names=True)[0]
zq, zs, zq2 = zipfile.ZipFile(q), zipfile.ZipFile(s2), zipfile.ZipFile(q2)
pq = sorted(r[0] for r in rd(zq, "Projects.csv")[1:] if r[0] != "(root)"); ps = sorted(r[0] for r in rd(zs, "Projects.csv")[1:] if r[0] != "(root)")
check("Step 1 and Step 2 give the same project codes", (pq, pq == ps, len(pq)), (ps, True, 2))
check("a second Step 1 (other output folder) gives the same codes again", sorted(r[0] for r in rd(zq2, "Projects.csv")[1:] if r[0] != "(root)"), pq)
paths = lambda z: {r[0] + "|" + r[1] for r in csv.reader(io.StringIO(z.read("Paths/sldprt.csv").decode("utf-8-sig")))} - {"project|path"}
fl = rd(zs, "Files.csv"); h = fl[0]; ip, ir = h.index("project"), h.index("rel_path")
check("the same file has the same project + path in Step 1's Paths list and Step 2's Files.csv",
      paths(zq), {r[ip] + "|" + r[ir] for r in fl[1:] if r[ir].endswith(".sldprt")})
info = lambda z: {r[0]: r[1] for r in rd(z, "Run_info.csv")}
check("Run_info says the codes are kept", (info(zq)["name_codes"], info(zs)["name_codes"]), ("same on every run (kept key)",) * 2)
check("same root id in both steps", info(zq)["folder_name"] == info(zs)["folder_name"] and info(zq)["folder_name"].startswith("ROOT-"), True)
maps = [f for f in os.listdir(out2) if f.startswith("name_map_")]
mz = zipfile.ZipFile(os.path.join(out2, maps[0]))
check("the map zip carries the key copy and says so; the profile zips do not", ("Name_Key.txt" in mz.namelist(), mz.read("Name_Key.txt").decode().strip() == key.hex(), any("Key" in n for n in zs.namelist() + zq.namelist())), (True, True, False))
leak = [(n, key.hex()[:16]) for z in (zs, zq) for n in z.namelist() if key.hex()[:16] in z.read(n).decode("utf-8", "replace")]
check("the key itself is in no shareable zip", leak, [])

# ---- no key can be kept -> per-run codes, said so
os.environ["PROFILER_NAME_KEY_FILE"] = os.path.join(KEY, "cannot", "be", "made")      # a path under an existing FILE
r = fp.quick_scan(src, os.path.join(t0, "o4"), anonymize_names=True)[0] if os.makedirs(os.path.join(t0, "o4")) is None else None
zr = zipfile.ZipFile(r)
check("unwritable key place -> Run_info says new for this run only", info(zr)["name_codes"].startswith("new for this run only"), True)
sys.exit(1 if any(bad) else 0)
