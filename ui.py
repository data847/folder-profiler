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
import fusion_cloud_profiler as fcp  # noqa: E402
TOKEN = os.urandom(16).hex()
STATE = {"state": "idle", "log": [], "result": None, "error": None,
         "progress": {"phase": "", "files": 0, "total_files": 0, "bytes": 0, "total_bytes": 0}}
LOCK = threading.Lock()
# Fusion 360 (Autodesk cloud) sign-in: lives in memory only, never written to disk
FUSION = {"api": None, "hubs": [], "pending": None, "error": "", "client_id": ""}
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
nav.tabs{display:flex;gap:6px;margin:0 0 16px;border-bottom:1px solid var(--bd)}nav.tabs button{background:transparent;color:var(--mut);border:0;border-bottom:3px solid transparent;border-radius:0;padding:8px 14px}nav.tabs button.on{color:var(--fg);border-bottom-color:var(--ac)}
select{flex:1;min-width:0;padding:9px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
table.qt{border-collapse:collapse}table.qt td,table.qt th{border:1px solid #8884;padding:3px 10px;text-align:left}</style></head><body><main>
<h1>Folder Profiler</h1><p class="s">Two steps: a very fast count of files per type, then the full profile. Never copies or changes your files. Runs only on this computer.</p>
<nav class="tabs"><button id="t_folder" class="on">Folders</button><button id="t_fusion">Fusion 360 (cloud)</button></nav>
<div id="tab_folder">
<div class="card"><b>1. What do you want to measure?</b>
<div class="modes" style="margin-top:8px"><label><input type="radio" name="m" value="parent" checked> One parent folder <span class="mut">(each sub-folder = a project)</span></label>
<label><input type="radio" name="m" value="pick"> Pick project folders <span class="mut">(tick one or more)</span></label></div></div>
<div class="card"><b id="h2">2. Choose the parent folder</b>
<div class="row" style="margin-top:8px"><input type="text" id="p" placeholder="Folder path" spellcheck="false"><button class="g" id="up">Up</button><button class="g" id="addcur" style="display:none" title="Tick the folder shown in the box">Add this folder</button></div>
<ul id="ls"></ul>
<div id="selbox" style="display:none;margin-top:12px"><span class="mut" id="selcount"></span><ul id="sel" style="max-height:150px"></ul></div></div>
<div class="card"><b>3. Run</b>
<label style="display:block;margin-top:8px"><input type="checkbox" id="anonn"> Anonymise file and folder names in the result <span class="mut">(optional; applies to Step 1 and Step 2. Only the result is renamed, never your files. You also get a separate name map to keep private; extra time is negligible)</span></label>
<label style="display:block;margin:6px 0 0 24px"><input type="checkbox" id="anonmap" checked disabled> Also give me the name map <span class="mut">(a separate file, real name &rarr; anonymised name, with its own download button. Keep it private.)</span></label>
<label style="display:block;margin-top:8px"><input type="checkbox" id="fresh"> Start from scratch <span class="mut">(normally a stopped run continues where it stopped; tick this to ignore saved progress)</span></label>
<div class="row" style="margin-top:8px"><button id="goquick" title="Counts the files of each type and their size. Nothing is opened, so it is very fast.">Step 1: quick count</button>
<button id="go" title="The complete profile (file details, references inside assemblies, drawing sheets). Takes much longer.">Step 2: full profile</button></div>
<div class="mut" style="margin-top:6px">Step 1 only counts files per type and size (very fast; no file is opened). Step 2 is the complete profile.</div>
<div class="bar" id="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i id="barfill"></i></div><div class="mut" id="barlabel" style="margin-top:6px"></div>
<pre id="log" class="mut" style="margin-top:10px"></pre><div id="res" style="margin-top:10px"></div></div>
</div>
<div id="tab_fusion" style="display:none">
<div class="card"><b>1. Autodesk app credentials</b>
<div style="margin:8px 0"><a class="btn" style="background:var(--ac)" href="/help/fusion" target="_blank" rel="noopener">How to create the app and how this works (opens in a new tab)</a></div>
<p class="mut" style="margin:6px 0">For data that lives in an Autodesk cloud hub (Fusion Team). Read-only and metadata only: names, types, sizes, dates and folders - nothing is downloaded or opened.
Create an app once at <b>aps.autodesk.com/myapps</b> (type <i>Desktop, Mobile, Single-Page App</i>; API <i>Data Management</i>) and register exactly this callback URL:</p>
<div class="row"><input type="text" id="fcb" readonly></div>
<div class="row" style="margin-top:8px"><input type="text" id="fcid" placeholder="Client ID of that app" spellcheck="false" autocomplete="off"><button id="fsign">Sign in with Autodesk</button></div>
<div class="mut" style="margin-top:6px">No client secret is needed. A new tab asks the hub user to sign in and approve read access. The sign-in is kept in memory only and ends when you close this program.</div>
<div id="fstat" style="margin-top:8px"></div></div>
<div class="card" id="fhubcard" style="display:none"><b>2. Choose the hub and projects</b>
<div class="row" style="margin-top:8px"><select id="fhub"></select><button class="g" id="fout">Sign out</button></div>
<div class="mut" style="margin-top:8px" id="fpl"></div><ul id="fproj"></ul></div>
<div class="card" id="frun" style="display:none"><b>3. Run</b>
<div class="row" style="margin-top:8px"><button id="fgo">Profile this hub</button></div>
<div class="mut" style="margin-top:6px">Every Fusion design is counted as a part (the cloud does not say part or assembly) and designs have no size in the cloud, so size totals are understated. The result is the same kind of zip as the Folders tab and goes into the same compile step.</div>
<pre id="flog" class="mut" style="margin-top:10px"></pre><div id="fres" style="margin-top:10px"></div></div>
</div>
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
$("anonn").onchange=()=>{$("anonmap").disabled=!$("anonn").checked};
$("go").onclick=()=>start("full");$("goquick").onclick=()=>start("quick");
const busy=v=>{$("go").disabled=v;$("goquick").disabled=v};
async function start(kind){$("res").textContent="";$("bar").style.display="none";$("barlabel").textContent="";
 if(mode=="pick"&&!sel.length){$("res").innerHTML="<span class='er'>Tick at least one folder first.</span>";return}
 busy(true);
 const o={fresh:$("fresh").checked,kind:kind,anon_names:$("anonn").checked,anon_map:$("anonmap").checked};
 const body=mode=="pick"?{t:T,paths:sel,...o}:{t:T,path:$("p").value,...o};
 const r=await fetch("/api/start",{method:"POST",body:JSON.stringify(body)});const j=await r.json();
 if(j.error){$("res").innerHTML="<span class='er'>"+esc(j.error)+"</span>";busy(false);return}poll()}
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
 if(j.state=="running"){setTimeout(poll,700);return}busy(false);
 if(j.state=="done"&&j.result.quick){let t="<table class='qt'><tr><th>File type</th><th>Files</th><th>Size</th></tr>";
  j.result.top.forEach(x=>{t+="<tr><td>."+esc(x[0])+"</td><td>"+x[1].toLocaleString()+"</td><td>"+mb(x[2])+"</td></tr>"});t+="</table>";
  $("res").innerHTML="<span class='ok'>Step 1 done: "+j.result.files.toLocaleString()+" files, "+mb(j.result.total_bytes)+", "+j.result.types+" file types in "+j.result.projects+" folders ("+j.result.seconds+" s).</span><br><br>"+t+
   "<br><a class='btn' href='/api/download?t="+T+"'>Download quick count zip</a> <span class='mut'>"+esc(j.result.name)+"</span><br><br><span class='mut'>When you are ready, click <b>Step 2: full profile</b> for the complete profile.</span>"+(j.result.map_name?"<br><br><a class='btn' style='background:var(--er)' href='/api/download_map?t="+T+"'>Download name map</a> <span class='mut'>"+esc(j.result.map_name)+" - KEEP PRIVATE: it turns the codes back into the real names. Do not send it with the profile.</span>":"")}
 else if(j.state=="done")$("res").innerHTML="<span class='ok'>Done: "+j.result.projects+" folders, "+j.result.files.toLocaleString()+" files"+(j.result.errors?", "+j.result.errors+" unreadable":"")+
  ".</span><br><br><a class='btn' href='/api/download?t="+T+"'>Download metrics zip</a> <span class='mut'>"+esc(j.result.name)+"</span><br><br>"+
  (j.result.stats_name?"<a class='btn' style='background:var(--ac)' href='/api/download_log?t="+T+"'>Download run statistics</a> <span class='mut'>timings and your machine's speed - no file names; safe to share</span>":"")+(j.result.map_name?"<br><br><a class='btn' style='background:var(--er)' href='/api/download_map?t="+T+"'>Download name map</a> <span class='mut'>"+esc(j.result.map_name)+" - KEEP PRIVATE: it turns the codes back into the real names. Do not send it with the profile.</span>":"");
 else if(j.state=="error")$("res").innerHTML="<span class='er'>"+esc(j.error)+"</span>"}
function showTab(n){["folder","fusion"].forEach(x=>{$("tab_"+x).style.display=x==n?"":"none";$("t_"+x).classList.toggle("on",x==n)});if(n=="fusion")fstatus()}
$("t_folder").onclick=()=>showTab("folder");$("t_fusion").onclick=()=>showTab("fusion");
$("fcb").value="http://localhost:"+location.port+"/callback";
try{$("fcid").value=localStorage.getItem("fcid")||""}catch(e){}
let fwaiting=0;
$("fsign").onclick=async()=>{const id=$("fcid").value.trim();if(!id){$("fstat").innerHTML="<span class='er'>Enter the Client ID first.</span>";return}
 try{localStorage.setItem("fcid",id)}catch(e){}
 const j=await (await fetch("/api/fusion/login?t="+T+"&client_id="+encodeURIComponent(id))).json();
 if(j.error){$("fstat").innerHTML="<span class='er'>"+esc(j.error)+"</span>";return}
 window.open(j.url,"_blank");$("fstat").innerHTML="<span class='mut'>Waiting for you to sign in in the new tab...</span>";fwaiting=Date.now();fwait()};
async function fwait(){const j=await (await fetch("/api/fusion/status?t="+T)).json();
 if(j.signed_in||j.error||Date.now()-fwaiting>300000){fshow(j);return}setTimeout(fwait,1000)}
async function fstatus(){fshow(await (await fetch("/api/fusion/status?t="+T)).json())}
function fshow(j){const on=j.signed_in;$("fhubcard").style.display=on?"":"none";$("frun").style.display=on?"":"none";
 if(j.error){$("fstat").innerHTML="<span class='er'>"+esc(j.error)+"</span>"}
 else if(on){$("fstat").innerHTML=j.hubs.length?"<span class='ok'>Signed in.</span>":"<span class='er'>Signed in, but no hub can be read. For BIM 360 Docs / Forma hubs the account admin must first allow the app; Fusion Team hubs need only your approval.</span>"}
 if(on){const keep=$("fhub").value;$("fhub").innerHTML="";j.hubs.forEach(h=>{const o=document.createElement("option");o.value=h[0];o.textContent=h[1];$("fhub").appendChild(o)});if(keep)$("fhub").value=keep;fprojects()}}
$("fhub").onchange=fprojects;
async function fprojects(){const h=$("fhub").value;$("fproj").innerHTML="";if(!h){$("fpl").textContent="";return}
 $("fpl").textContent="Loading projects...";const j=await (await fetch("/api/fusion/projects?t="+T+"&hub="+encodeURIComponent(h))).json();
 if(j.error){$("fpl").innerHTML="<span class='er'>"+esc(j.error)+"</span>";return}
 $("fpl").textContent=j.projects.length+" project(s). Untick any you do not want.";
 j.projects.forEach(n=>{const li=document.createElement("li"),cb=document.createElement("input");cb.type="checkbox";cb.checked=true;cb.dataset.n=n;
  const sp=document.createElement("span");sp.className="n";sp.textContent=n;sp.onclick=()=>cb.click();li.appendChild(cb);li.appendChild(sp);$("fproj").appendChild(li)})}
$("fout").onclick=async()=>{await fetch("/api/fusion/logout",{method:"POST",body:JSON.stringify({t:T})});$("fstat").textContent="Signed out.";fshow({signed_in:false,hubs:[]})};
$("fgo").onclick=async()=>{const names=[...$("fproj").querySelectorAll("input")].filter(c=>c.checked).map(c=>c.dataset.n);
 if(!names.length){$("fres").innerHTML="<span class='er'>Tick at least one project.</span>";return}
 $("fres").textContent="";$("fgo").disabled=true;
 const hn=$("fhub").options[$("fhub").selectedIndex].textContent;
 const r=await fetch("/api/start",{method:"POST",body:JSON.stringify({t:T,kind:"fusion",hub:$("fhub").value,hub_name:hn,projects:names})});const j=await r.json();
 if(j.error){$("fres").innerHTML="<span class='er'>"+esc(j.error)+"</span>";$("fgo").disabled=false;return}fpoll()};
async function fpoll(){const j=await (await fetch("/api/status?t="+T)).json();$("flog").textContent=j.log.join("\n");
 if(j.state=="running"){$("fres").innerHTML="<span class='mut'>Reading the hub...</span>";setTimeout(fpoll,1000);return}$("fgo").disabled=false;
 if(j.state=="done"){const r=j.result;$("fres").innerHTML="<span class='ok'>Done: "+r.projects+" projects, "+r.files.toLocaleString()+" items"+(r.errors?", "+r.errors+" folder(s) could not be read":"")+".</span><br>"+
  (r.notes||[]).map(n=>"<div class='mut'>"+esc(n)+"</div>").join("")+"<br><a class='btn' href='/api/download?t="+T+"'>Download profile zip</a> <span class='mut'>"+esc(r.name)+"</span>"}
 else if(j.state=="error")$("fres").innerHTML="<span class='er'>"+esc(j.error)+"</span>"}
renderSel();ls("");
</script></body></html>"""

HELP_FUSION = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fusion 360 cloud: setup guide</title><style>
:root{--bg:#f6f7f9;--fg:#1b2030;--mut:#5d6578;--card:#fff;--bd:#d9dde6;--ac:#1f4fd8;--ok:#127a3e;--er:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e8ebf2;--mut:#98a1b5;--card:#1b2030;--bd:#2d3447;--ac:#6d93ff;--ok:#4cc27f;--er:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 60px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:30px 0 8px}
.s,.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin:12px 0}
ol,ul{padding-left:22px}li{margin:6px 0}code,.url{background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:1px 6px;font:13px ui-monospace,monospace;word-break:break-all}
.url{display:block;padding:8px 10px;margin:8px 0;font-size:14px}a{color:var(--ac)}.er{color:var(--er)}.ok{color:var(--ok)}
table{border-collapse:collapse;width:100%}td,th{border:1px solid var(--bd);padding:6px 10px;text-align:left;vertical-align:top;font-size:14px}th{background:var(--card)}
</style></head><body><main>
<h1>Fusion 360 cloud: setup guide</h1>
<p class="s">How to let the Folder Profiler read the file list of an Autodesk cloud hub (Fusion Team), what it reads, and what to do when something goes wrong. About 10 minutes, once.</p>

<h2>What this does, in one paragraph</h2>
<div class="card">Fusion 360 designs normally live in Autodesk's cloud, not in a folder, so the folder profiler cannot see them. This tab signs in to the hub with the Autodesk account of someone who is a member of it and asks Autodesk for a <b>list</b>: hubs, projects, folders, and for every item its name, type, size, date and folder. It is <b>read-only and metadata only</b>: no design is downloaded, opened or converted, and nothing is changed in the hub. The result is a zip in the same format as a folder profile, which the <i>profile-compile</i> skill turns into the usual workbook.</div>

<h2>Who does what</h2>
<table><tr><th>Person</th><th>Does</th></tr>
<tr><td><b>App owner</b><br><span class="mut">anyone with an Autodesk account; can be you or the client</span></td><td>Creates the small "app" below once and gets its <b>Client ID</b>. The Client ID alone gives no access to any data.</td></tr>
<tr><td><b>Hub member</b><br><span class="mut">the person whose Autodesk account can see the designs</span></td><td>Signs in when the browser asks and approves <b>read-only</b> access. Without this approval nothing can be read.</td></tr></table>
<p class="mut">If the client does not want to give you access, they can do both parts themselves on their own computer and send you only the finished zip.</p>

<h2>Part 1 - Create the app (once)</h2>
<div class="card"><ol>
<li>Open <a href="https://aps.autodesk.com/myapps" target="_blank" rel="noopener">aps.autodesk.com/myapps</a> and sign in with an Autodesk account (a free account is enough).</li>
<li>Choose <b>Create application</b> (the button may be called "Create app").</li>
<li>Name it anything, for example <code>Folder Profiler</code>.</li>
<li>Application type: choose <b>Desktop, Mobile, Single-Page App</b>. This is the type that signs in without a secret.</li>
<li>Under <b>API access</b> tick <b>Data Management API</b> (that is the one that lists hubs, projects and files). Other APIs are not needed. Save.</li>
<li><b>Callback URL</b> - paste exactly this, character for character, and save:
<span class="url" id="cb">http://localhost:8765/callback</span>
<span class="mut">It must match exactly. If it does not, Autodesk refuses the sign-in. This is the address of the Folder Profiler on this computer; it works only while the profiler is running.</span></li>
<li>Copy the <b>Client ID</b> shown for the app (a long string of letters and digits). You do <b>not</b> need the Client Secret - leave it where it is and never paste it anywhere.</li>
</ol>
<span class="mut">Autodesk changes its portal now and then, so a button or field may be named a little differently from the above; the five things that matter are the type (Desktop, Mobile, Single-Page App), the API (Data Management), the exact callback URL, a Client ID, and no secret.</span></div>

<h2>Part 2 - Use it in the Folder Profiler</h2>
<div class="card"><ol>
<li>Go back to the Folder Profiler page, tab <b>Fusion 360 (cloud)</b>.</li>
<li>Paste the <b>Client ID</b> and click <b>Sign in with Autodesk</b>. A new browser tab opens at Autodesk.</li>
<li>Sign in <b>as the hub member</b> (the account that can see the designs) and approve the request. It asks only for <code>data:read</code> - read access to data. When it says "Signed in", close that tab.</li>
<li>Choose the <b>hub</b>, untick any <b>projects</b> you do not want, and click <b>Profile this hub</b>.</li>
<li>When it finishes, click <b>Download profile zip</b>. Give that zip to the <i>profile-compile</i> skill like any other profile.</li>
</ol></div>

<h2>What is read, and what is not</h2>
<table><tr><th>Read (a list, nothing more)</th><th>Never done</th></tr>
<tr><td>Hub, project and folder names<br>For every item: name, type, size (if Autodesk gives one), last-changed date, folder path</td>
<td>Downloading, opening or converting a design<br>Reading geometry, properties or comments<br>Changing, moving or deleting anything<br>Saving your password (it is never seen by this program; you type it at Autodesk)</td></tr></table>
<p>The sign-in lives in this program's memory only. It is never written to disk and ends when you close the Folder Profiler or click <b>Sign out</b>. The Client ID is remembered in your browser so you do not retype it. The hub member can also withdraw the approval at any time in their Autodesk account's settings for authorised apps.</p>

<h2>How the sign-in works (for the curious or for the client's IT)</h2>
<div class="card">It is the standard <b>OAuth 2.0 authorization-code flow with PKCE</b>, the one Autodesk provides for apps that cannot keep a secret. The profiler sends the browser to Autodesk with your Client ID and a one-time challenge; you sign in at Autodesk (never on this page); Autodesk sends the browser back to the callback URL with a one-time code; the profiler swaps the code for an access token, which lasts about an hour and is renewed automatically while a run is going. Only the Data Management API is called, only with GET requests (reads).</div>

<h2>What to expect in the result</h2>
<ul>
<li><b>Every Fusion design counts as one part</b> (<code>.f3d</code>). The cloud list does not say whether a design is a part or an assembly, so the parts / assemblies split for these projects is not measured.</li>
<li><b>Designs have no size</b> in the cloud list (Autodesk gives a size only for uploaded files), so they count as 0 bytes and size totals are understated. The page tells you how many items had no size.</li>
<li>No drawing sheet counts and no references between designs: nothing is opened.</li>
<li>Uploaded files (DWG, PDF, STEP ...) are listed with their real type and size.</li>
<li>Anything Autodesk lists that has no recognisable type is written to <code>Unknown_Item_Types.csv</code> inside the zip rather than guessed; folders that could not be read are in <code>Unreadable.csv</code>.</li>
</ul>

<h2>Troubleshooting</h2>
<table><tr><th>You see</th><th>Cause and fix</th></tr>
<tr><td>"did not accept the sign-in" or Autodesk shows an error about the callback / redirect</td><td>The callback URL registered for the app is not exactly the one in Part 1, step 6, or the Client ID is wrong. Edit the app at aps.autodesk.com/myapps and compare character by character. If the Folder Profiler was started while port 8765 was busy it uses another port: then the callback URL shown on the tab is the one to register, and it is shown there every time.</td></tr>
<tr><td>Signed in, but "no hub can be read"</td><td>The signed-in account is not a member of the hub (sign in again with the right account), or the hub is an Autodesk Docs / BIM 360 / Forma hub, where the account administrator must first allow the app in the admin portal. Fusion Team hubs need only the member's approval.</td></tr>
<tr><td>Some folders listed as unreadable / "no read permission"</td><td>That member cannot see those folders. Sign in as someone who can, or accept that they are missing; the count is shown in the result.</td></tr>
<tr><td>"sign in again"</td><td>The sign-in expired (for example the computer slept for a long time). Click <b>Sign in with Autodesk</b> again; a finished run is not affected.</td></tr>
<tr><td>Nothing happens after clicking Sign in</td><td>The browser blocked the new tab. Allow pop-ups for this page and click again.</td></tr></table>

<h2>Be sure of the numbers</h2>
<p>The listing, paging, sign-in and zip are tested against Autodesk's published API description and an imitation of it. The first run on a real hub is the real check: look at <code>Run_info.csv</code> and <code>Unknown_Item_Types.csv</code> in the zip before quoting totals, and say that Fusion part / assembly counts are not measured.</p>
</main><script>document.getElementById("cb").textContent="http://localhost:"+location.port+"/callback"</script></body></html>"""


def _run(path, folders=None, anon=False, tier=2, depth=1, fresh=False, quick=False, anon_names=False, anon_map=True):
    def log(msg):
        with LOCK:
            STATE["log"].append(msg.rstrip())
    def progress(phase, files, total_files, done_bytes, total_bytes):
        with LOCK:
            STATE["progress"] = {"phase": phase, "files": files, "total_files": total_files,
                                 "bytes": done_bytes, "total_bytes": total_bytes}
    try:
        label = os.environ.get("PROFILER_FOLDER_NAME") if IN_DOCKER and path == DATA_ROOT else None
        if quick:
            zip_path, nproj, nfiles, errors = fp.quick_scan(path, OUT_DIR, log=log, label=label, folders=folders, progress=progress, project_depth=depth,
                                                            anonymize_names=anon_names or None, name_map=anon_map)
            with LOCK:
                STATE.update(state="done", result={"path": zip_path, "name": os.path.basename(zip_path), "stats_path": "", "stats_name": "",
                                                   "map_path": fp.LAST_RUN.get("map_zip", ""), "map_name": os.path.basename(fp.LAST_RUN.get("map_zip", "")),
                                                   "projects": nproj, "files": nfiles, "errors": errors, "quick": True,
                                                   "top": fp.LAST_RUN.get("top", []), "total_bytes": fp.LAST_RUN.get("total_bytes", 0),
                                                   "types": fp.LAST_RUN.get("distinct_extensions", 0), "seconds": fp.LAST_RUN.get("seconds", 0)})
            return
        zip_path, nproj, nfiles, errors = fp.profile(path, OUT_DIR, log=log, label=label, folders=folders, progress=progress,
                                                      anonymize_assembly_names=anon, tier=tier, project_depth=depth,
                                                      resume=not fresh, anonymize_names=anon_names or None, name_map=anon_map)
        sz = fp.LAST_RUN.get("stats_zip") or ""
        with LOCK:
            STATE.update(state="done", result={"path": zip_path, "name": os.path.basename(zip_path), "stats_path": sz,
                                               "stats_name": os.path.basename(sz), "projects": nproj, "files": nfiles, "errors": errors,
                                               "map_path": fp.LAST_RUN.get("map_zip", ""), "map_name": os.path.basename(fp.LAST_RUN.get("map_zip", ""))})
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


def _run_fusion(hub_id, hub_name, names):
    def log(msg):
        with LOCK:
            STATE["log"].append(msg.rstrip())
    try:
        api = FUSION["api"]
        path, n, nproj, stats = fcp.run(api, hub_id, hub_name, OUT_DIR, only=names or None, log=log)
        notes = []
        if stats["unknown_types"]:
            notes.append(f"{sum(stats['unknown_types'].values())} item(s) of unknown type are listed in Unknown_Item_Types.csv inside the zip.")
        if stats["size_unknown"]:
            notes.append(f"{stats['size_unknown']} item(s) have no size in the cloud (counted as 0 bytes).")
        with LOCK:
            STATE.update(state="done", result={"path": path, "name": os.path.basename(path), "stats_path": "", "stats_name": "",
                                               "projects": nproj, "files": n, "errors": len(stats["unreadable"]), "notes": notes})
    except (fcp.ApiError, SystemExit) as e:
        with LOCK:
            STATE.update(state="error", error=str(e))
    except Exception:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "error_log.txt"), "w") as f:
            f.write(traceback.format_exc())
        with LOCK:
            STATE.update(state="error", error="The cloud run stopped unexpectedly. Details are in output/error_log.txt (no access token is written there).")


def _in_data(p):
    p = os.path.realpath(p)
    return p == DATA_ROOT or p.startswith(DATA_ROOT + os.sep)



def _net_hint(path, e):
    """On Windows, tell the person what to do about the usual file-server errors (logon failure, access denied, drive letter not visible)."""
    if os.name != "nt":
        return ""
    code = getattr(e, "winerror", None)
    if code in (1326, 1219, 1244, 86):       # logon failure / multiple connections with other credentials / not authenticated / bad password
        unc = "\\".join(path.split("\\")[:4]) if path.startswith("\\\\") else r"\\server\share"
        return (". Windows has no valid login for that server for the account running this program. Fix: open a Command Prompt AS THE SAME USER "
                f"that starts the profiler and run:  net use {unc} /user:DOMAIN\\yourname  (it asks for the password), or open the folder once in File "
                "Explorer and tick 'Remember my credentials'. If 'net use' says error 1219, first run  net use * /delete  and try again")
    if code in (5, 53, 67):                  # access denied / network path not found / network name not found
        return (". Check the path and that this account may read the share. Drive letters mapped in Explorer are not visible to a program "
                "started 'as administrator' or as another user: type the full \\\\server\\share\\folder path instead")
    return ""

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
        if u.path == "/help/fusion":
            return self._send(200, HELP_FUSION, "text/html; charset=utf-8")
        if u.path == "/callback":           # Autodesk redirects the browser here after sign-in (no page token; the state value is the check)
            return self._fusion_callback(q)
        if q.get("t", [""])[0] != TOKEN:
            return self._send(403, "forbidden", "text/plain")
        if u.path == "/api/fusion/login":
            cid = q.get("client_id", [""])[0].strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", cid):
                return self._json({"error": "That does not look like an Autodesk Client ID (letters and digits only)."})
            redirect = f"http://localhost:{self.server.server_address[1]}/callback"
            url, verifier, state = fcp.auth_begin(cid, redirect)
            with LOCK:
                FUSION.update(pending={"state": state, "verifier": verifier, "client_id": cid, "redirect": redirect}, error="", api=None, hubs=[])
            return self._json({"url": url})
        if u.path == "/api/fusion/status":
            with LOCK:
                return self._json({"signed_in": FUSION["api"] is not None, "hubs": FUSION["hubs"], "error": FUSION["error"]})
        if u.path == "/api/fusion/projects":
            api = FUSION["api"]
            if api is None:
                return self._json({"error": "Sign in first."})
            try:
                return self._json({"projects": sorted((n for _i, n in fcp.projects(api, q.get("hub", [""])[0])), key=str.lower)})
            except fcp.ApiError as e:
                return self._json({"error": str(e)})
        if u.path == "/api/ls":
            p = q.get("path", [""])[0] or (DATA_ROOT if IN_DOCKER else os.path.expanduser("~"))
            p = os.path.abspath(p)
            if IN_DOCKER and not _in_data(p):
                return self._json({"error": "Only the folder you chose in PROFILE_DIR can be used here. Pick from the list instead of typing a path."})
            try:
                dirs = sorted((d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d)) and not d.startswith(".")),
                              key=str.lower)
            except OSError as e:
                return self._json({"error": f"Cannot open that folder ({e.strerror})" + _net_hint(p, e)})
            return self._json({"path": p, "parent": p if IN_DOCKER and os.path.realpath(p) == DATA_ROOT else os.path.dirname(p), "dirs": dirs, "sep": os.sep})
        if u.path == "/api/status":
            with LOCK:
                return self._json(STATE)
        if u.path in ("/api/download", "/api/download_log", "/api/download_map"):
            with LOCK:
                res = STATE.get("result")
            key = ("stats_path", "stats_name") if u.path.endswith("_log") else ("map_path", "map_name") if u.path.endswith("_map") else ("path", "name")
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

    def _fusion_callback(self, q):
        with LOCK:
            pend = FUSION.get("pending")
        msg = "Sign-in was not completed. Close this tab and try again."
        if pend and q.get("state", [""])[0] == pend["state"] and q.get("code") and not q.get("error"):
            try:
                token, refresher = fcp.auth_finish(pend["client_id"], pend["redirect"], q["code"][0], pend["verifier"])
                api = fcp.Api(token, refresher=refresher)
                hubs = fcp.hubs(api)
                with LOCK:
                    FUSION.update(api=api, hubs=[list(h) for h in hubs], pending=None, error="", client_id=pend["client_id"])
                msg = "Signed in to Autodesk. You can close this tab and go back to the Folder Profiler."
            except fcp.ApiError as e:
                with LOCK:
                    FUSION.update(error=str(e), pending=None)
                msg = "Sign-in failed: " + str(e)
        elif q.get("error"):
            with LOCK:
                FUSION.update(error="Autodesk sign-in was cancelled or refused (" + q["error"][0] + ").", pending=None)
        self._send(200, "<!doctype html><meta charset=utf-8><title>Autodesk sign-in</title><body style='font:16px system-ui;margin:40px'><p>"
                   + msg.replace("&", "&amp;").replace("<", "&lt;") + "</p>", "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_ok() or self.path not in ("/api/start", "/api/fusion/logout"):
            return self._send(403, "forbidden", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if body.get("t") != TOKEN:
            return self._send(403, "forbidden", "text/plain")
        if self.path == "/api/fusion/logout":
            with LOCK:
                FUSION.update(api=None, hubs=[], pending=None, error="")
            return self._json({"ok": True})
        if body.get("kind") == "fusion":
            hub = str(body.get("hub") or "")
            names = [str(n) for n in body.get("projects") or []]
            if FUSION["api"] is None or not hub or not names:
                return self._json({"error": "Sign in, choose a hub and tick at least one project first."})
            with LOCK:
                if STATE["state"] == "running":
                    return self._json({"error": "A run is already in progress"})
                STATE.update(state="running", log=[], result=None, error=None,
                             progress={"phase": "scan", "files": 0, "total_files": 0, "bytes": 0, "total_bytes": 0})
            threading.Thread(target=_run_fusion, args=(hub, str(body.get("hub_name") or "hub"), names), daemon=True).start()
            return self._json({"ok": True})
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
        threading.Thread(target=_run, args=(path, folders or None, bool(body.get("anon")), 2, 1,
                                                          bool(body.get("fresh")), body.get("kind") == "quick", bool(body.get("anon_names")), body.get("anon_map", True) is not False), daemon=True).start()
        self._json({"ok": True})


def main(argv=None, open_browser=True):
    global OUT_DIR
    import argparse
    ap = argparse.ArgumentParser(description="Folder Profiler browser UI (local only).")
    ap.add_argument("--host", default="0.0.0.0" if IN_DOCKER else "127.0.0.1", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=8765 if IN_DOCKER else 0, help="port (default: 8765, else any free port)")
    ap.add_argument("--workers", type=int, help="parallel hashing threads (default: auto)")
    ap.add_argument("--out", help="where to write the zip (default: ./output next to this script)")
    a = ap.parse_args(argv)
    OUT_DIR = a.out or OUT_DIR
    if a.workers:
        os.environ["PROFILER_WORKERS"] = str(a.workers)
    try:
        srv = ThreadingHTTPServer((a.host, a.port or 8765), Handler)   # 8765 first: the Autodesk callback URL is registered once for this port
    except OSError:
        if a.port:
            raise
        srv = ThreadingHTTPServer((a.host, 0), Handler)
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
