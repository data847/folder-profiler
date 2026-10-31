"""Fusion: Step 1 (quick count, no names) and Step 2 (full profile) over one resumable listing. Step 2 reuses a recent Step 1 listing with no new calls to
Autodesk; a run that crashes, is stopped or gives up under throttling carries on from what is saved - also by a newer version of the tool - and ends with
exactly the output of an uninterrupted run; a locked folder is reported without blocking reuse; a different state format starts afresh."""
import csv, http.server, io, json, os, shutil, sys, tempfile, threading, time, urllib.parse, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import fusion_cloud_profiler as fc
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
def rd(z, n): return list(csv.DictReader(io.StringIO(zipfile.ZipFile(z).read(n).decode("utf-8-sig"))))
def info(z): return {r["key"]: r["value"] for r in rd(z, "Run_info.csv")}

# ---- a fake hub: project Gear (3 levels, unique names), project Small, project Locked (403)
TREE = {}                                  # folder id -> (subfolder ids, items)
def f(fid, name): return {"type": "folders", "id": fid, "attributes": {"name": name, "displayName": name}}
def it(i, n, tip): return {"type": "items", "id": i, "attributes": {"displayName": n, "extension": {"type": "items:autodesk.core:File"}}, "relationships": {"tip": {"data": {"type": "versions", "id": tip}}}}
def ve(i, n, size, ft): return {"type": "versions", "id": i, "attributes": {"name": n, "versionNumber": 1, "createTime": "2024-01-01T00:00:00.000Z", "lastModifiedTime": "2024-05-01T10:00:00.000Z", "storageSize": size, "fileType": ft, "extension": {"type": "versions:autodesk.core:File"}}}
NAMES = {}
def mk(fid, name, subs, n_items):
    NAMES[fid] = name; TREE[fid] = (subs, [(f"{fid}_i{k}", f"SECRETNAME_{fid}_{k}.{['dwg', 'pdf', 'f3d', 'step'][k % 4]}", 100 * (k + 1), ['dwg', 'pdf', 'f3d', 'step'][k % 4]) for k in range(n_items)])
mk("g_root", "Gear Files", ["g_a", "g_b", "g_c", "g_d"], 3)
for x in "abcd":
    mk(f"g_{x}", f"SecretFolder_{x}", [f"g_{x}1", f"g_{x}2", f"g_{x}3"], 4)
    for n in "123": mk(f"g_{x}{n}", f"SecretSub_{x}{n}", [], 5)
mk("s_root", "Small Top", [], 6)
TOTAL_ITEMS = sum(len(v[1]) for k, v in TREE.items() if k.startswith(("g_", "s_")))
GEAR_FOLDERS = len([k for k in TREE if k.startswith("g_")])
class Hub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, doc, hdr=None):
        b = json.dumps(doc).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        for k, v in (hdr or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if p == "/project/v1/hubs": return self.reply(200, {"data": [{"id": "b.hub", "type": "hubs", "attributes": {"name": "Test Hub"}}]})
        if p.endswith("/projects"): return self.reply(200, {"data": [{"id": "b.gear", "attributes": {"name": "Gear"}}, {"id": "b.small", "attributes": {"name": "Small"}}, {"id": "b.locked", "attributes": {"name": "Locked"}}]})
        if p.endswith("/b.gear/topFolders"): return self.reply(200, {"data": [f("g_root", "Gear Files")]})
        if p.endswith("/b.small/topFolders"): return self.reply(200, {"data": [f("s_root", "Small Top")]})
        if p.endswith("/b.locked/topFolders"): return self.reply(200, {"data": [f("l_root", "Vault")]})
        if "/folders/" in p and p.endswith("/contents"):
            fid = p.split("/folders/")[1].rsplit("/contents", 1)[0]
            with self.server.lock: self.server.contents.append(fid); n = len(self.server.contents)
            hook = self.server.hook(fid, n)
            if hook: return self.reply(*hook)
            if fid == "l_root": return self.reply(403, {"errors": []})
            subs, items = TREE[fid]
            return self.reply(200, {"data": [it(i, nm, "v" + i) for i, nm, _s, _t in items] + [f(s, NAMES[s]) for s in subs],
                                    "included": [ve("v" + i, nm, sz, ft) for i, nm, sz, ft in items]})
        self.reply(404, {})
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hub); srv.lock, srv.contents, srv.hook = threading.Lock(), [], (lambda fid, n: None)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
class Fixed:
    def uniform(self, a, b): return 1.0
def new_api(**kw):
    gov = fc.Governor(3, base_pause=0.05, max_pause=0.3, rng=Fixed())
    return fc.Api("T", BASE, workers=3, governor=gov, **kw)
def run(out, step=2, api=None, **kw):
    srv.contents.clear()
    return fc.run(api or new_api(), "b.hub", "Test Hub", out, quiet=True, step=step, **kw)

with tempfile.TemporaryDirectory() as t:
    # ---- baseline: one uninterrupted Step 2
    out0 = os.path.join(t, "base"); z0, n0, np0, st0 = run(out0, 2)
    base_files = rd(z0, "Files.csv"); base_dirs = sorted(r["name"] for r in rd(z0, "Dirs.csv"))
    check("baseline: all items listed", (n0, np0), (TOTAL_ITEMS, 3))
    check("the finished Step 2 leaves no saved progress behind", os.path.exists(os.path.join(out0, ".resume-fusion")) and bool(os.listdir(os.path.join(out0, ".resume-fusion"))), False)
    check("the locked folder is reported in Unreadable.csv and Dirs.csv still holds it", ([(r["project"], r["path"]) for r in rd(z0, "Unreadable.csv")], "Vault" in base_dirs), ([("Locked", "Vault")], True))
    check("rows are in a fixed order (project, then path)", [(r["project"], r["rel_path"]) for r in base_files] == sorted((r["project"], r["rel_path"]) for r in base_files), True)
    # ---- Step 1: counts only, no names
    out1 = os.path.join(t, "s1"); zq, nq, npq, stq = run(out1, 1)
    check("Step 1 counts the same items", (nq, npq), (TOTAL_ITEMS, 3))
    members = zipfile.ZipFile(zq).namelist()
    check("Step 1 zip is a quick-count zip (Extensions_All.csv, no Files.csv)", ("Extensions_All.csv" in members, "Files.csv" in members), (True, False))
    blob = b"".join(zipfile.ZipFile(zq).read(m) for m in members).decode("utf-8", "replace")
    check("Step 1 holds NO item or folder names (only project names and the unreadable folder)", ("SECRETNAME" in blob, "SecretFolder" in blob, "SecretSub" in blob, "Gear Files" in blob), (False, False, False, False))
    ext_all = {r["ext"]: (int(r["file_count"]), int(r["total_bytes"])) for r in rd(zq, "Extensions_All.csv")}
    exp = {}
    for r in base_files: exp[r["ext"]] = (exp.get(r["ext"], (0, 0))[0] + 1, exp.get(r["ext"], (0, 0))[1] + int(r["size_bytes"]))
    check("Step 1 per-type counts and bytes equal the full profile's", ext_all, exp)
    import compile_profiles as cp
    check("the profile-compile skill recognises it as a quick count", cp.is_quick_zip(zq), True)
    check("Step 1 keeps the saved listing for Step 2", os.path.isdir(os.path.join(out1, ".resume-fusion")) and len(os.listdir(os.path.join(out1, ".resume-fusion"))) == 1, True)
    # ---- Step 2 right after Step 1: no new listing calls
    z2, n2, np2, st2 = run(out1, 2)
    check("Step 2 after Step 1 asks Autodesk for NO folder listing (reuses it)", len(srv.contents), 0)
    check("...and equals an uninterrupted Step 2 exactly", (rd(z2, "Files.csv") == base_files, sorted(r["name"] for r in rd(z2, "Dirs.csv")) == base_dirs), (True, True))
    i2 = info(z2)
    check("Run_info says the listing was reused and by which versions", (i2["listing_reused_from_earlier_step"], i2["step"], i2["versions_used"]), ("yes", "2", fc.VERSION))
    check("saved progress (and its folder) is gone after Step 2", os.path.exists(os.path.join(out1, ".resume-fusion")), False)
    # ---- reuse limits: stale, or 'from scratch'
    out3 = os.path.join(t, "s3"); run(out3, 1)
    run(out3, 2, reuse_hours=0.0); check("a listing older than the limit is made again", len(srv.contents) > 0, True)
    out4 = os.path.join(t, "s4"); run(out4, 1)
    run(out4, 2, fresh=True); check("'start from scratch' lists again", len(srv.contents) > 0, True)
    # ---- crash in the middle, then carry on (by a NEWER version)
    out5 = os.path.join(t, "crash"); real_get = fc.Api.get; counter = [0]
    class Crash(BaseException): pass
    def crashing(self, url):
        if "/contents" in url:
            counter[0] += 1
            if counter[0] == 9: raise Crash()
        return real_get(self, url)
    fc.Api.get = crashing; real_v = fc.VERSION; fc.VERSION = "1.0"
    try:
        try: run(out5, 2)
        except Crash: pass
    finally: fc.Api.get = real_get
    listed_before = len(srv.contents); fc.VERSION = "1.1"
    z5, n5, np5, st5 = run(out5, 2)
    i5 = info(z5)
    check("the crash left saved progress; the next run (new version) listed only what was left", listed_before + len(srv.contents) <= GEAR_FOLDERS + 2 + 1 + 3 + 3, True)
    check("the resumed run's output equals an uninterrupted run's", (rd(z5, "Files.csv") == base_files, n5), (True, TOTAL_ITEMS))
    check("Run_info names both versions and the restart", (i5["versions_used"], i5["restarts_resumed"]), ("1.0 -> 1.1", "1"))
    check("the run log keeps the earlier part and says it resumed", any("RESUMED" in r["message"] for r in rd(z5, "Run_log.csv")), True)
    fc.VERSION = real_v
    # ---- a different state format is not trusted
    out6 = os.path.join(t, "fmt"); fc.Api.get = crashing; counter[0] = 0
    try:
        try: run(out6, 2)
        except Crash: pass
    finally: fc.Api.get = real_get
    fc.STATE_FORMAT = 2
    z6, n6, *_ = run(out6, 2); fc.STATE_FORMAT = 1
    check("progress saved in another format is not used: everything is listed again, and the result is still right", (len(srv.contents) >= GEAR_FOLDERS + 2, n6), (True, TOTAL_ITEMS))
    # ---- heavy throttling: gives up on the folder, keeps everything else, finishes when Autodesk recovers
    out7 = os.path.join(t, "thr")
    srv.hook = lambda fid, n: (429, {}, {}) if fid == "g_b2" else None
    try:
        run(out7, 2, api=new_api(patience=0.8)); check("endless throttling of one folder ends the run as Incomplete", False, True)
    except fc.Incomplete as e:
        check("the run says the listing is unfinished and nothing is lost", ("not finished" in str(e), "carries on" in str(e)), (True, True))
        check("no zip is produced from an unfinished listing", [x for x in os.listdir(out7) if x.endswith(".zip") and "run_stats" not in x], [])
    done_before = len(srv.contents)
    srv.hook = lambda fid, n: None
    z7, n7, *_ = run(out7, 2)
    check("after Autodesk recovers, ONLY the folder that failed (and any not reached) is listed again", len(srv.contents) < GEAR_FOLDERS, True)
    check("the final output equals an uninterrupted run's", (rd(z7, "Files.csv") == base_files, n7), (True, TOTAL_ITEMS))
    i7 = info(z7)
    check("the rate-limit cost is in Run_info", (int(i7["api_throttled_answers"]) >= 1, int(i7["throttle_episodes"]) >= 1, float(i7["seconds_waiting_for_rate_limit"]) >= 0), (True, True, True))

    # ---- through the page's own endpoints: Step 1, Step 2 (reusing it), and an unfinished listing shown as 'stopped', not as a failure
    import ui, urllib.request
    from http.server import ThreadingHTTPServer
    web = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=web.serve_forever, daemon=True).start()
    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{web.server_port}{path}", data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
        return json.load(urllib.request.urlopen(req))
    def wait_state():
        for _ in range(300):
            st = call("/api/status?t=" + ui.TOKEN)
            if st["state"] != "running": return st
            time.sleep(0.2)
        raise SystemExit("UI run did not finish")
    ui.OUT_DIR = os.path.join(t, "ui"); ui.FUSION["api"] = new_api()
    def start(step, fresh=False):
        ui.FUSION["api"] = new_api(patience=0.8); srv.contents.clear()
        r = call("/api/start", {"t": ui.TOKEN, "kind": "fusion", "hub": "b.hub", "hub_name": "Test Hub", "projects": ["Gear", "Small", "Locked"], "step": step, "fresh": fresh})
        return r, wait_state()
    r, st = start(1)
    check("UI Step 1: done, quick result with the type table and the counts", (st["state"], st["result"].get("quick"), st["result"]["files"], len(st["result"]["top"]) > 0), ("done", True, TOTAL_ITEMS, True))
    r, st = start(2)
    check("UI Step 2 right after: done, no folder listing asked of Autodesk, and it says so", (st["state"], len(srv.contents), any("reused" in n for n in st["result"]["notes"])), ("done", 0, True))
    srv.hook = lambda fid, n: (429, {}, {}) if fid == "g_b2" else None
    r, st = start(2, fresh=True)
    check("UI: a listing Autodesk would not finish is 'stopped' with the reason - not an error, no fail log", (st["state"], st.get("fail"), "not finished" in (st.get("message") or "")), ("stopped", None, True))
    srv.hook = lambda fid, n: None
    r, st = start(2)
    check("UI: run again and it carries on and finishes", (st["state"], st["result"]["files"]), ("done", TOTAL_ITEMS))
    web.shutdown()
srv.shutdown()
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
