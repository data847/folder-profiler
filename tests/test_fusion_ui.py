"""The Fusion 360 tab end to end: sign-in callback, hubs, projects, a run and the download - against a fake Autodesk server."""
import http.server, json, os, sys, tempfile, threading, urllib.parse, urllib.request, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, doc, hdr=None):
        b = json.dumps(doc).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        for k, v in (hdr or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        ok = body["grant_type"] == ["authorization_code"] and body["code"] == ["CODE1"] and body["client_id"] == ["MY_CLIENT_ID_12345"] and body.get("code_verifier")
        self.send(200 if ok else 400, {"access_token": "TOKEN", "refresh_token": "R", "expires_in": 3599})
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); p = urllib.parse.unquote(u.path)
        if p == "/authentication/v2/authorize":
            q = urllib.parse.parse_qs(u.query)
            assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["data:read"] and q["response_type"] == ["code"]
            return self.send(302, {}, {"Location": q["redirect_uri"][0] + "?code=CODE1&state=" + q["state"][0]})
        assert self.headers["Authorization"] == "Bearer TOKEN"
        if p == "/project/v1/hubs": return self.send(200, {"data": [{"id": "b.h1", "attributes": {"name": "Acme Hub"}}]})
        if p.endswith("/b.h1/projects"): return self.send(200, {"data": [{"id": "b.p1", "attributes": {"name": "Pumps"}}, {"id": "b.p2", "attributes": {"name": "Valves"}}]})
        if p.endswith("/topFolders"): return self.send(200, {"data": [{"type": "folders", "id": "f" + p.split("/")[-2][-1], "attributes": {"name": "Files"}}]})
        if p.endswith("/contents"):
            n = p.split("/folders/")[1][1]
            return self.send(200, {"data": [{"type": "items", "id": "i" + n, "attributes": {"displayName": "Part" + n, "extension": {"type": "items:autodesk.core:Item"}},
                              "relationships": {"tip": {"data": {"id": "v" + n}}}}],
                                   "included": [{"type": "versions", "id": "v" + n, "attributes": {"extension": {"type": "versions:autodesk.a360:CompositeDesign"}, "lastModifiedTime": "2024-01-01T00:00:00Z"}}]})
        self.send(404, {})
fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake); threading.Thread(target=fake.serve_forever, daemon=True).start()
os.environ["FUSION_API_BASE"] = f"http://127.0.0.1:{fake.server_port}"
import ui
with tempfile.TemporaryDirectory() as out:
    ui.OUT_DIR = out
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://localhost:{srv.server_address[1]}"; T = ui.TOKEN
    def get(path, follow=True):
        return urllib.request.urlopen(base + path, timeout=30).read()
    def api(path): return json.loads(get(path))
    h = get("/help/fusion").decode()
    check("help page is served with the steps, callback URL script and no secret asked for", all(x in h for x in ("Desktop, Mobile, Single-Page App", "Data Management API", "/callback", "Client ID", "PKCE")) and "Client Secret" in h, True)
    check("main page links to the help in a new tab", b'href="/help/fusion" target="_blank"' in get("/"), True)
    check("not signed in at the start", api(f"/api/fusion/status?t={T}")["signed_in"], False)
    check("bad client id is refused", "error" in api(f"/api/fusion/login?t={T}&client_id=%3Cscript%3E"), True)
    check("projects refused before sign-in", "error" in api(f"/api/fusion/projects?t={T}&hub=b.h1"), True)
    j = api(f"/api/fusion/login?t={T}&client_id=MY_CLIENT_ID_12345")
    check("login gives the Autodesk address with our callback", urllib.parse.parse_qs(urllib.parse.urlparse(j["url"]).query)["redirect_uri"][0], base + "/callback")
    # the "browser": Autodesk's page redirects to the callback on this UI
    page = urllib.request.urlopen(j["url"], timeout=30).read().decode()           # follows the 302 to /callback
    check("callback page says signed in", "Signed in to Autodesk" in page, True)
    st = api(f"/api/fusion/status?t={T}")
    check("signed in, hub listed", (st["signed_in"], st["hubs"]), (True, [["b.h1", "Acme Hub"]]))
    check("projects of the hub", api(f"/api/fusion/projects?t={T}&hub=b.h1")["projects"], ["Pumps", "Valves"])
    bad_cb = urllib.request.urlopen(base + "/callback?code=CODE1&state=WRONG", timeout=30).read().decode()
    check("a callback with the wrong state does nothing", ("not completed" in bad_cb, api(f"/api/fusion/status?t={T}")["signed_in"]), (True, True))
    def post(path, doc):
        return json.loads(urllib.request.urlopen(urllib.request.Request(base + path, data=json.dumps(doc).encode(), method="POST"), timeout=30).read())
    try:
        post("/api/start", {"t": "wrong", "kind": "fusion"}); check("wrong token refused", False, True)
    except urllib.error.HTTPError as e:
        check("wrong page token is refused (403)", e.code, 403)
    check("run starts", post("/api/start", {"t": T, "kind": "fusion", "hub": "b.h1", "hub_name": "Acme Hub", "projects": ["Pumps", "Valves"]}), {"ok": True})
    import time
    for _ in range(100):
        stt = api(f"/api/status?t={T}")
        if stt["state"] != "running": break
        time.sleep(0.2)
    r = stt["result"]
    check("run finished with 2 projects, 2 items", (stt["state"], r["projects"], r["files"]), ("done", 2, 2))
    check("notes mention the missing sizes", any("no size" in n for n in r["notes"]), True)
    z = zipfile.ZipFile(r["path"])
    check("download serves the zip", get(f"/api/download?t={T}")[:2], b"PK")
    check("zip has Files.csv with f3d rows", z.read("Files.csv").decode("utf-8-sig").count(",f3d,"), 2)
    post("/api/fusion/logout", {"t": T})
    check("sign-out clears the sign-in", api(f"/api/fusion/status?t={T}")["signed_in"], False)
    check("the access token is nowhere on disk in the output folder", all(b"TOKEN" not in open(os.path.join(out, f), "rb").read() for f in os.listdir(out)), True)
    srv.shutdown()
fake.shutdown()
sys.exit(1 if any(bad) else 0)
