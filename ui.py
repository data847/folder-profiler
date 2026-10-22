"""Folder Profiler - local browser UI. Standard library only.

Starts a tiny web server on 127.0.0.1 (this computer only, never the network), opens your browser,
lets you pick a folder, shows live progress, and offers the metrics zip for download.
All profiling is done by file_profiler.profile(); this file only adds the page around it.
"""
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # the bundled embeddable Python runs isolated and does not add the script folder
import file_profiler as fp  # noqa: E402
TOKEN = os.urandom(16).hex()
STATE = {"state": "idle", "log": [], "result": None, "error": None,
         "progress": {"phase": "", "files": 0, "total_files": 0, "bytes": 0, "total_bytes": 0}}
LOCK = threading.Lock()
# frozen exe: HERE is a temp unpack dir, so put output next to the exe the client double-clicked
IN_DOCKER = bool(os.environ.get("PROFILER_IN_DOCKER"))
DATA_ROOT = "/data"  # docker: the only folder tree the page may browse or profile (mounted read-only)
OUT_DIR = "/out" if IN_DOCKER else os.path.join(os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE, "output")

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Folder Profiler</title><style>
:root{--bg:#f6f7f9;--fg:#1b2030;--mut:#5d6578;--card:#fff;--bd:#d9dde6;--ac:#1f4fd8;--ok:#127a3e;--er:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e8ebf2;--mut:#98a1b5;--card:#1b2030;--bd:#2d3447;--ac:#6d93ff;--ok:#4cc27f;--er:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}p.s{color:var(--mut);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:8px}input[type=text]{flex:1;min-width:0;padding:9px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:9px 16px;border-radius:6px;border:1px solid var(--ac);background:var(--ac);color:#fff;font:inherit;cursor:pointer}
button.g{background:transparent;color:var(--ac)}button:disabled{opacity:.5;cursor:default}
ul{list-style:none;margin:10px 0 0;padding:0;max-height:260px;overflow:auto;border:1px solid var(--bd);border-radius:6px}
li{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--bd);display:flex;gap:8px;align-items:center}li:last-child{border:0}li:hover{background:var(--bg)}
li span.n{flex:1}pre{margin:0;max-height:220px;overflow:auto;font-size:13px;white-space:pre-wrap}.mut{color:var(--mut);font-size:13px}
.ok{color:var(--ok)}.er{color:var(--er)}a.btn{display:inline-block;padding:9px 16px;border-radius:6px;background:var(--ok);color:#fff;text-decoration:none}
.modes label{margin-right:18px;cursor:pointer}.chip{display:flex;justify-content:space-between;gap:8px;padding:5px 10px;border-bottom:1px solid var(--bd);font-size:13px}
.bar{height:14px;border-radius:7px;background:var(--bd);overflow:hidden;margin-top:12px;display:none}.bar i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s}
.bar.ind i{width:35%;animation:slide 1.1s infinite ease-in-out}@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
@media(prefers-reduced-motion:reduce){.bar.ind i{animation:none;width:100%;opacity:.5}}
.chip b{cursor:pointer;color:var(--er)}input[type=checkbox]{width:17px;height:17px}
</style></head><body><main>
<h1>Folder Profiler</h1><p class="s">Measures file extensions, sizes, dates and fingerprints. Never copies files. How much it records is your choice (step 3). Runs only on this computer.</p>
<div class="card"><b>1. What do you want to measure?</b>
<div class="modes" style="margin-top:8px"><label><input type="radio" name="m" value="parent" checked> One parent folder <span class="mut">(each sub-folder = a project)</span></label>
<label><input type="radio" name="m" value="pick"> Pick project folders <span class="mut">(tick one or more)</span></label></div></div>
<div class="card"><b id="h2">2. Choose the parent folder</b>
<div class="row" style="margin-top:8px"><input type="text" id="p" placeholder="Folder path" spellcheck="false"><button class="g" id="up">Up</button><button class="g" id="addcur" style="display:none" title="Tick the folder shown in the box">Add this folder</button></div>
<ul id="ls"></ul>
<div id="selbox" style="display:none;margin-top:12px"><span class="mut" id="selcount"></span><ul id="sel" style="max-height:150px"></ul></div></div>
<div class="card"><b>3. Run</b>
<div style="margin-top:8px"><label>Detail level&nbsp;
<select id="lvl"><option value="0">0 - metrics only (no folder names)</option><option value="1">1 - plus file and folder names</option><option value="2">2 - plus links inside assemblies</option></select></label>
<div class="mut" id="lvlhelp" style="margin-top:4px"></div></div>
<div style="margin-top:8px"><label>Projects are&nbsp;<input type="number" id="depth" value="1" min="1" max="6" style="width:60px"> level(s) below the chosen folder</label>
<span class="mut"> (1 = each folder directly inside it; use 2 if it holds containers such as DESIGN)</span></div>
<label style="display:block;margin-top:8px"><input type="checkbox" id="arch" checked> Look inside archives <span class="mut">(.zip and .tar.gz are opened; .7z and .rar are listed only. Nothing is extracted to disk.)</span></label>
<label style="display:block;margin-top:8px"><input type="checkbox" id="fresh"> Start from scratch <span class="mut">(normally a stopped run continues where it stopped; tick this to ignore saved progress)</span></label>
<label style="display:block;margin-top:8px" id="anonrow"><input type="checkbox" id="anon"> Hide assembly file names <span class="mut">(use codes like ASM-3F9A1C2B.SLDASM instead of the real names)</span></label>
<div class="row" style="margin-top:8px"><button id="go">Start profiling</button></div>
<div class="bar" id="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i id="barfill"></i></div><div class="mut" id="barlabel" style="margin-top:6px"></div>
<pre id="log" class="mut" style="margin-top:10px"></pre><div id="res" style="margin-top:10px"></div></div>
</main><script>
const T="__TOKEN__",$=id=>document.getElementById(id);let cur="",sep="/",mode="parent";const sel=[];
const esc=s=>s.replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function renderSel(){$("selbox").style.display=mode=="pick"?"block":"none";
 $("selcount").textContent=sel.length?sel.length+" folder(s) selected:":"No folders selected yet. Tick the boxes above.";
 $("sel").innerHTML="";sel.forEach((p,i)=>{const li=document.createElement("li");li.className="chip";
  li.innerHTML="<span>"+esc(p)+"</span><b title='Remove'>✕</b>";li.querySelector("b").onclick=()=>{sel.splice(i,1);renderSel();ls(cur)};$("sel").appendChild(li)})}
async function ls(p){const r=await fetch("/api/ls?t="+T+"&path="+encodeURIComponent(p||""));const j=await r.json();
 if(j.error){$("ls").innerHTML="<li class='er'>"+esc(j.error)+"</li><li onclick='ls(\"\")'>Back to start</li>";return}
 cur=j.path;sep=j.sep;$("p").value=j.path;$("up").dataset.parent=j.parent;
 $("ls").innerHTML=j.dirs.length?"":"<li class='mut'>No sub-folders here</li>";
 j.dirs.forEach(d=>{const full=j.path+(j.path.endsWith(sep)?"":sep)+d,li=document.createElement("li");
  if(mode=="pick"){const cb=document.createElement("input");cb.type="checkbox";cb.checked=sel.includes(full);
   cb.onclick=e=>{e.stopPropagation();const i=sel.indexOf(full);if(cb.checked&&i<0)sel.push(full);if(!cb.checked&&i>=0)sel.splice(i,1);renderSel()};li.appendChild(cb)}
  const sp=document.createElement("span");sp.className="n";sp.textContent="📁 "+d;sp.onclick=()=>ls(full);li.appendChild(sp);$("ls").appendChild(li)})}
function setMode(m){mode=m;$("h2").textContent=m=="pick"?"2. Tick the project folders (you can browse into other folders and keep ticking)":"2. Choose the parent folder";
 $("addcur").style.display=m=="pick"?"":"none";renderSel();ls(cur)}
document.querySelectorAll("input[name=m]").forEach(r=>r.onchange=()=>setMode(r.value));
$("addcur").onclick=()=>{if(cur&&!sel.includes(cur)){sel.push(cur);renderSel()}};
$("up").onclick=()=>ls($("up").dataset.parent);$("p").onchange=()=>ls($("p").value);
$("go").onclick=async()=>{$("res").textContent="";$("bar").style.display="none";$("barlabel").textContent="";
 if(mode=="pick"&&!sel.length){$("res").innerHTML="<span class='er'>Tick at least one folder first.</span>";return}
 $("go").disabled=true;
 const o={anon:$("anon").checked,tier:+$("lvl").value,depth:+$("depth").value,arch:$("arch").checked,fresh:$("fresh").checked};
 const body=mode=="pick"?{t:T,paths:sel,...o}:{t:T,path:$("p").value,...o};
 const r=await fetch("/api/start",{method:"POST",body:JSON.stringify(body)});const j=await r.json();
 if(j.error){$("res").innerHTML="<span class='er'>"+esc(j.error)+"</span>";$("go").disabled=false;return}poll()};
const mb=b=>b>=1e9?(b/1e9).toFixed(1)+" GB":(b/1e6).toFixed(0)+" MB";
function bar(j){const p=j.progress,b=$("bar");
 if(j.state=="idle"){b.style.display="none";$("barlabel").textContent="";return}
 b.style.display="block";
 if(j.state=="done"){b.classList.remove("ind");$("barfill").style.width="100%";b.setAttribute("aria-valuenow",100);$("barlabel").textContent="100% complete";return}
 if(j.state=="error"){b.classList.remove("ind");return}
 if(p.phase=="links"){b.classList.remove("ind");$("barfill").style.width=Math.round(100*p.files/Math.max(1,p.total_files))+"%";$("barlabel").textContent="Reading references inside assemblies... "+p.files.toLocaleString()+" of "+p.total_files.toLocaleString();return}
 if(p.phase=="scan"||!p.total_files){b.classList.add("ind");$("barlabel").textContent="Counting files... "+p.total_files.toLocaleString()+" found";return}
 b.classList.remove("ind");
 const pct=p.total_bytes?Math.min(100,p.bytes/p.total_bytes*100):p.files/p.total_files*100;
 $("barfill").style.width=pct+"%";b.setAttribute("aria-valuenow",Math.round(pct));
 $("barlabel").textContent=Math.round(pct)+"%  -  "+p.files.toLocaleString()+" of "+p.total_files.toLocaleString()+" files  ("+mb(p.bytes)+" of "+mb(p.total_bytes)+")"}
async function poll(){const j=await (await fetch("/api/status?t="+T)).json();bar(j);$("log").textContent=j.log.join("\n");$("log").scrollTop=1e9;
 if(j.state=="running"){setTimeout(poll,700);return}$("go").disabled=false;
 if(j.state=="done")$("res").innerHTML="<span class='ok'>Done: "+j.result.projects+" folders, "+j.result.files.toLocaleString()+" files"+(j.result.errors?", "+j.result.errors+" unreadable":"")+
  ".</span><br><br><a class='btn' href='/api/download?t="+T+"'>Download metrics zip</a> <span class='mut'>"+esc(j.result.name)+"</span><br><br>"+
  "<a class='btn' style='background:var(--ac)' href='/api/download_log?t="+T+"'>Download run statistics</a> <span class='mut'>timings and your machine's speed - no file or folder names, safe to share</span>";
 else if(j.state=="error")$("res").innerHTML="<span class='er'>"+esc(j.error)+"</span>"}
const HELP={0:"Records extensions, sizes, dates and fingerprints, anonymous folder ids, and the names of assembly files only (tick below to hide those).",
 1:"Level 0 plus the names and relative paths of every file and folder. No file contents.",
 2:"Level 1 plus the NAMES of files that assemblies, drawings and STEP files point to (so parts can be grouped under their real assembly). Nothing else is read out of the files."};
function lv(){const v=+$("lvl").value;$("lvlhelp").textContent=HELP[v];$("anon").disabled=v>0;$("anonrow").style.opacity=v>0?.5:1}
$("lvl").onchange=lv;lv();
renderSel();ls("");
</script></body></html>"""


def _run(path, folders=None, anon=False, tier=0, depth=1, arch=True, fresh=False):
    def log(msg):
        with LOCK:
            STATE["log"].append(msg.rstrip())
    def progress(phase, files, total_files, done_bytes, total_bytes):
        with LOCK:
            STATE["progress"] = {"phase": phase, "files": files, "total_files": total_files,
                                 "bytes": done_bytes, "total_bytes": total_bytes}
    try:
        label = os.environ.get("PROFILER_FOLDER_NAME") if IN_DOCKER and path == DATA_ROOT else None
        zip_path, nproj, nfiles, errors = fp.profile(path, OUT_DIR, log=log, label=label, folders=folders, progress=progress,
                                                      anonymize_assembly_names=anon, tier=tier, project_depth=depth,
                                                      archive_mode="full" if arch else "off", resume=not fresh)
        sz = fp.LAST_RUN.get("stats_zip") or ""
        with LOCK:
            STATE.update(state="done", result={"path": zip_path, "name": os.path.basename(zip_path), "stats_path": sz,
                                               "stats_name": os.path.basename(sz), "projects": nproj, "files": nfiles, "errors": errors})
    except SystemExit as e:
        with LOCK:
            STATE.update(state="error", error=str(e))
    except Exception:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "error_log.txt"), "w") as f:
            f.write(fp.scrub_paths(traceback.format_exc(), path))
        with LOCK:
            STATE.update(state="error", error="The run stopped. Everything finished so far is saved: click Start profiling again (same settings) and it "
                                              "continues where it stopped. If it keeps failing, send us output/error_log.txt")


def _in_data(p):
    p = os.path.realpath(p)
    return p == DATA_ROOT or p.startswith(DATA_ROOT + os.sep)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):  # blocks DNS-rebinding: only our own loopback name is accepted
        return re.fullmatch(r"(127\.0\.0\.1|localhost)(:\d+)?", self.headers.get("Host", "")) is not None

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if q.get("t", [""])[0] != TOKEN:
            return self._send(403, "forbidden", "text/plain")
        if u.path == "/api/ls":
            p = q.get("path", [""])[0] or (DATA_ROOT if IN_DOCKER else os.path.expanduser("~"))
            p = os.path.abspath(p)
            if IN_DOCKER and not _in_data(p):
                return self._json({"error": "Only the folder you chose in PROFILE_DIR can be used here. Pick from the list instead of typing a path."})
            try:
                dirs = sorted((d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d)) and not d.startswith(".")),
                              key=str.lower)
            except OSError as e:
                return self._json({"error": f"Cannot open that folder ({e.strerror})"})
            return self._json({"path": p, "parent": p if IN_DOCKER and os.path.realpath(p) == DATA_ROOT else os.path.dirname(p), "dirs": dirs, "sep": os.sep})
        if u.path == "/api/status":
            with LOCK:
                return self._json(STATE)
        if u.path in ("/api/download", "/api/download_log"):
            with LOCK:
                res = STATE.get("result")
            key = ("stats_path", "stats_name") if u.path.endswith("_log") else ("path", "name")
            if not res or not res.get(key[0]) or not os.path.exists(res[key[0]]):
                return self._send(404, "no result", "text/plain")
            size = os.path.getsize(res[key[0]])
            self.send_response(200)
            self.send_header("Content-Type", "application/zip"); self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{res[key[1]]}"'); self.end_headers()
            with open(res[key[0]], "rb") as f:                     # streamed: the zip can be hundreds of MB
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or self.path != "/api/start":
            return self._send(403, "forbidden", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if body.get("t") != TOKEN:
            return self._send(403, "forbidden", "text/plain")
        folders = [str(p).strip().strip('"') for p in body.get("paths") or []]
        path = (body.get("path") or "").strip().strip('"')
        if folders:
            bad = next((p for p in folders if not os.path.isdir(p) or (IN_DOCKER and not _in_data(p))), None)
            if bad:
                return self._json({"error": "Not a folder: " + bad})
            real = sorted({os.path.realpath(p) for p in folders})
            nested = next((a for a in real for b in real if a != b and a.startswith(b + os.sep)), None)
            if nested:  # a folder and one of its own sub-folders would be counted twice
                return self._json({"error": "One selected folder is inside another selected folder: " + nested})
            folders = real
        elif not path or not os.path.isdir(path) or (IN_DOCKER and not _in_data(path)):
            return self._json({"error": "Not a folder: " + path})
        with LOCK:
            if STATE["state"] == "running":
                return self._json({"error": "A run is already in progress"})
            STATE.update(state="running", log=[], result=None, error=None,
                         progress={"phase": "scan", "files": 0, "total_files": 0, "bytes": 0, "total_bytes": 0})
        threading.Thread(target=_run, args=(path, folders or None, bool(body.get("anon")), int(body.get("tier") or 0), int(body.get("depth") or 1),
                                                          body.get("arch", True) is not False, bool(body.get("fresh"))), daemon=True).start()
        self._json({"ok": True})


def main(argv=None, open_browser=True):
    global OUT_DIR
    import argparse
    ap = argparse.ArgumentParser(description="Folder Profiler browser UI (local only).")
    ap.add_argument("--host", default="0.0.0.0" if IN_DOCKER else "127.0.0.1", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=8765 if IN_DOCKER else 0, help="port (default: any free port)")
    ap.add_argument("--workers", type=int, help="parallel hashing threads (default: auto)")
    ap.add_argument("--out", help="where to write the zip (default: ./output next to this script)")
    a = ap.parse_args(argv)
    OUT_DIR = a.out or OUT_DIR
    if a.workers:
        os.environ["PROFILER_WORKERS"] = str(a.workers)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Folder Profiler UI: {url}\n(this computer only; press Ctrl+C or close this window to stop)")
    if open_browser and not IN_DOCKER:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
