"""Fusion cloud profiler against a local imitation of the APS Data Management API (shapes taken from Autodesk's OpenAPI spec)."""
import http.server, json, os, sys, tempfile, threading, urllib.parse, zipfile, csv, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "claude-skill", "profile-compile", "scripts"))
import fusion_cloud_profiler as fc
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)

def folder(i, n): return {"type": "folders", "id": i, "attributes": {"name": n, "displayName": n}}
def item(i, n, tip, **t): return {"type": "items", "id": i, "attributes": {"displayName": n, "extension": {"type": t.get("itype", "items:autodesk.core:File")}},
                                  "relationships": {"tip": {"data": {"type": "versions", "id": tip}}}}
def ver(i, n, size=None, ft=None, vtype="versions:autodesk.core:File", mod="2024-05-01T10:00:00.000Z"):
    a = {"name": n, "displayName": n, "versionNumber": 1, "createTime": "2024-01-01T00:00:00.000Z", "lastModifiedTime": mod, "extension": {"type": vtype}}
    if size is not None: a["storageSize"] = size
    if ft: a["fileType"] = ft
    return {"type": "versions", "id": i, "attributes": a}
HITS = {"429": 0}
PAGES = {  # (folder id, page) -> (data, included)
    ("f_top", 0): ([item("i1", "plan.pdf", "v1"), folder("f_sub", "Sub")], [ver("v1", "plan.pdf", 4000, "pdf")]),
    ("f_top", 1): ([item("i2", "Gearbox", "v2", itype="items:autodesk.core:Item")], [ver("v2", "Gearbox", None, None, "versions:autodesk.a360:CompositeDesign", "2024-06-02T08:30:00+05:30")]),
    ("f_sub", 0): ([item("i3", "bracket.f3d", "v3"), item("i4", "mystery", "v4", itype="items:weird:Thing")], [ver("v3", "bracket.f3d", 9000, "f3d"), ver("v4", "mystery", None, None, "versions:weird:Thing")]),
}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, doc, hdr=None):
        b = json.dumps(doc).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        for k, v in (hdr or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.headers["Authorization"] == "Bearer OLD": return self.send(401, {})
        assert self.headers["Authorization"] == "Bearer TOKEN"
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); p = urllib.parse.unquote(u.path)
        base = f"http://127.0.0.1:{self.server.server_port}"
        if p == "/project/v1/hubs": return self.send(200, {"data": [{"id": "b.hub1", "type": "hubs", "attributes": {"name": "Acme Hub"}}]})
        if p == "/project/v1/hubs/b.hub1/projects": return self.send(200, {"data": [{"id": "b.p1", "attributes": {"name": "Pumps"}}, {"id": "b.p2", "attributes": {"name": "Locked"}}]})
        if p.endswith("/b.p1/topFolders"): return self.send(200, {"data": [folder("f_top", "Project Files")]})
        if p.endswith("/b.p2/topFolders"): return self.send(200, {"data": [folder("f_locked", "Secret")]})
        if "/folders/" in p and p.endswith("/contents"):
            fid = p.split("/folders/")[1].rsplit("/contents", 1)[0]
            if fid == "f_locked": return self.send(403, {"errors": []})
            if fid == "f_top" and HITS["429"] == 0:
                HITS["429"] = 1; return self.send(429, {}, {"Retry-After": "0"})
            pg = int(q.get("page[number]", ["0"])[0]); data, inc = PAGES[(fid, pg)]
            doc = {"data": data, "included": inc, "links": {"self": {"href": base + self.path}}}
            if (fid, pg + 1) in PAGES:
                doc["links"]["next"] = {"href": f"{base}/data/v1/projects/b.p1/folders/{fid}/contents?page%5Bnumber%5D={pg + 1}"}
            return self.send(200, doc)
        self.send(404, {})
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
api = fc.Api("TOKEN", f"http://127.0.0.1:{srv.server_port}", retries=3)
check("hubs", fc.hubs(api), [("b.hub1", "Acme Hub")])
calls = []
exp = fc.Api("OLD", f"http://127.0.0.1:{srv.server_port}", refresher=lambda: calls.append(1) or "TOKEN")
check("expired token (401) is refreshed once and the call succeeds", (fc.hubs(exp), len(calls), exp.token), ([("b.hub1", "Acme Hub")], 1, "TOKEN"))
try:
    fc.hubs(fc.Api("OLD", f"http://127.0.0.1:{srv.server_port}"))
    check("401 without refresher raises", False, True)
except fc.ApiError as e:
    check("401 without refresher raises a clear error", "no read permission" in str(e), True)
with tempfile.TemporaryDirectory() as t:
    path, n, np_, stats = fc.run(api, "b.hub1", "Acme Hub", t, quiet=True)
    z = zipfile.ZipFile(path); rd = lambda name: list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))
    files = rd("Files.csv")
    check("4 items found in 2 projects", (n, np_), (4, 2))
    row = {f["rel_path"].split("/")[-1]: f for f in files}
    check("pdf: ext from fileType, size", (row["plan.pdf"]["ext"], row["plan.pdf"]["size_bytes"]), ("pdf", "4000"))
    check("design with no storageSize: f3d, size 0, FUSION tag, date converted to UTC", (row["Gearbox"]["ext"], row["Gearbox"]["size_bytes"], row["Gearbox"]["header_signature"], row["Gearbox"]["modified_utc"]), ("f3d", "0", "FUSION:Design", "2024-06-02T03:00:00Z"))
    check("name extension used (bracket.f3d), path and depth", (row["bracket.f3d"]["ext"], row["bracket.f3d"]["rel_path"], row["bracket.f3d"]["depth"]), ("f3d", "Project Files/Sub/bracket.f3d", "2"))
    check("unknown type is reported, not guessed", (row["mystery"]["ext"], stats["unknown_types"]), ("", {"versions:weird:Thing items:weird:thing".split()[0] if False else "items:weird:Thing versions:weird:Thing".lower(): 1}))
    check("size-unknown items counted (design + mystery)", stats["size_unknown"], 2)
    check("forbidden folder is listed, run not aborted", [(u[0], u[1], u[2]) for u in stats["unreadable"]], [("Locked", "folder", "Secret")])
    check("429 was retried", api.retried >= 1, True)
    info = {r["key"]: r["value"] for r in rd("Run_info.csv")}
    check("Run_info", (info["files"], info["total_bytes"], info["source"], info["items_size_unknown"]), ("4", "13000", "autodesk-cloud-hub", "2"))
    check("zip has the compiler's files", {"Files.csv", "Projects.csv", "Dirs.csv", "Run_info.csv", "Unreadable.csv", "Unknown_Item_Types.csv"} <= set(z.namelist()), True)
    import compile_profiles as cp
    em, sf = cp.load_mapping(os.path.join(ROOT, "claude-skill", "profile-compile", "scripts", "mapping"))
    compiled, *_ = cp.compile_runs(cp.load_profiles([path]), em, sf, False, "auto", True, "folder")
    pc = {c[0].split("/")[-1]: dict(zip(cp.HEADINGS, c)) for c in compiled}
    check("compiler reads it: Pumps = 2 parts (cloud design + bracket), Fusion 360", (pc["Pumps"]["Part Files"], pc["Pumps"]["Native CAD Software"]), (2, "Fusion 360"))
srv.shutdown()
sys.exit(1 if any(bad) else 0)
