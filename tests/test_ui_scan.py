"""The UI's step 2 runs the CAD scan and offers the .xlsx workbook for download (not a zip)."""
import os as _os, tempfile as _tf; _os.environ["PROFILER_NAME_KEY_FILE"] = _os.path.join(_tf.mkdtemp(), "name_key")
import io, json, os, sys, tempfile, threading, time, urllib.request, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import ui
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "Root"); out = os.path.join(t, "out"); os.makedirs(os.path.join(src, "Job", "3D")); os.makedirs(out)
    open(os.path.join(src, "Job", "3D", "a.stp"), "wb").write(b"ISO-10303-21;\n#1=MANIFOLD_SOLID_BREP('a');\n")
    open(os.path.join(src, "Job", "notes.txt"), "wb").write(b"hi")
    ui.OUT_DIR = out
    srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"; T = ui.TOKEN
    post = lambda doc: json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/start", data=json.dumps(doc).encode(), method="POST")).read())
    page = urllib.request.urlopen(base + "/").read().decode()
    check("page has the cloud-download box and the scan result text", 'id="dlcloud"' in page and "Download scan workbook" in page, True)
    check("step 2 starts", post({"t": T, "path": src, "kind": "full"}), {"ok": True})
    for _ in range(300):
        s = json.loads(urllib.request.urlopen(f"{base}/api/status?t={T}").read())
        if s["state"] != "running": break
        time.sleep(0.1)
    r = s["result"]
    check("done, flagged as a scan, .xlsx", (s["state"], r.get("scan"), r["name"].endswith(".xlsx")), ("done", True, True))
    resp = urllib.request.urlopen(f"{base}/api/download?t={T}")
    data = resp.read()
    check("download is a spreadsheet", ("spreadsheetml" in resp.headers["Content-Type"], data[:2]), (True, b"PK"))
    check("workbook has the 7 sheets", sum(n.startswith("xl/worksheets/") for n in zipfile.ZipFile(io.BytesIO(data)).namelist()), 7)
sys.exit(1 if any(bad) else 0)
