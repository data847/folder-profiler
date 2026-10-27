"""The UI's anonymise box: it reaches both steps, and the map is downloadable separately from the profile."""
import io, json, os, sys, tempfile, threading, time, urllib.request, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import ui
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "Root"); out = os.path.join(t, "out"); os.makedirs(os.path.join(src, "SecretClient", "SecretSub")); os.makedirs(out)
    open(os.path.join(src, "SecretClient", "SecretSub", "SecretDrawing.dwg"), "wb").write(b"0" * 50)
    ui.OUT_DIR = out
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"; T = ui.TOKEN
    def post(doc): return json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/start", data=json.dumps(doc).encode(), method="POST")).read())
    def status(): return json.loads(urllib.request.urlopen(f"{base}/api/status?t={T}").read())
    def wait():
        for _ in range(200):
            s = status()
            if s["state"] != "running": return s
            time.sleep(0.1)
    def get(p): return urllib.request.urlopen(f"{base}{p}?t={T}").read()
    page = urllib.request.urlopen(base + "/").read().decode()
    check("the page has the checkbox, the map checkbox and the map button code", 'id="anonn"' in page and 'id="anonmap"' in page and "download_map" in page, True)
    for kind in ("quick", "full"):
        for anon in (False, True):
            check(f"{kind} anon={anon} starts", post({"t": T, "path": src, "kind": kind, "anon_names": anon}), {"ok": True})
            s = wait(); r = s["result"]
            zf = zipfile.ZipFile(io.BytesIO(get("/api/download")))
            blob = b"".join(zf.read(n) for n in zf.namelist()).lower()
            check(f"{kind} anon={anon}: done; real names {'absent' if anon else 'present'}", (s["state"], b"secretclient" in blob), ("done", not anon))
            if anon:
                check(f"{kind}: name map offered and downloadable, separate from the profile", (bool(r["map_name"]), r["map_name"].startswith("name_map_"), "Name_Map.csv" in zipfile.ZipFile(io.BytesIO(get("/api/download_map"))).namelist(),
                                                                                           "Name_Map.csv" in zf.namelist()), (True, True, True, False))
                m = zipfile.ZipFile(io.BytesIO(get("/api/download_map"))).read("Name_Map.csv").decode().lower()
                check(f"{kind}: map holds the real project name", "secretclient" in m, True)
            else:
                check(f"{kind}: no map when the box is off", r["map_name"], "")
            time.sleep(1.1)           # zips are named to the second
    for kind in ("quick", "full"):
        before = {f for f in os.listdir(out) if f.startswith("name_map_")}
        check(f"{kind} anonymised WITHOUT the map starts", post({"t": T, "path": src, "kind": kind, "anon_names": True, "anon_map": False}), {"ok": True})
        s = wait(); r = s["result"]
        zf = zipfile.ZipFile(io.BytesIO(get("/api/download")))
        check(f"{kind}: names hidden, no map file made, no map offered", (b"secretclient" in b"".join(zf.read(n) for n in zf.namelist()).lower(),
              r["map_name"], {f for f in os.listdir(out) if f.startswith("name_map_")} - before), (False, "", set()))
        time.sleep(1.1)
    srv.shutdown()
sys.exit(1 if any(bad) else 0)
