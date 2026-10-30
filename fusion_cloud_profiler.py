"""Fusion cloud profiler - the Folder Profiler for data that lives in an Autodesk cloud hub (Fusion Team / Autodesk Docs).

READ-ONLY and METADATA-ONLY, like the folder profiler: it lists hubs -> projects -> folders -> items through the Autodesk Platform
Services (APS) Data Management API and records name, type, size, date and folder of every item. It never downloads a design, never asks
Autodesk to translate or open one, and keeps no file contents. The result is a profile zip in the same format as the folder profiler's,
so the profile-compile skill reads it unchanged (project = Fusion project).

Setup (once, by whoever owns the integration):
  1. Create an app at https://aps.autodesk.com/myapps  (type: "Desktop, Mobile, Single-Page App" - a public client, no secret).
     Callback URL: http://localhost:8765/callback      API: Data Management API.
  2. Fusion Team hubs: the signed-in user's consent is the access grant. (Autodesk's docs require extra provisioning only for BIM 360
     Docs hubs - Account Administrator portal - and Forma hubs - Custom Integrations; if the hub list comes back empty, that is why.)
  3. Run:   python fusion_cloud_profiler.py --client-id <APS client id>         (a browser window asks the hub user to sign in
     and approve "data:read" - nothing else is requested; the access token is kept in memory only and is never written to disk)

Usage:
  python fusion_cloud_profiler.py --client-id ID                 list the hubs this user can read
  python fusion_cloud_profiler.py --client-id ID --hub "Name"    profile one hub  -> fusion_<hub>_<date>.zip
  options: --out DIR, --project NAME (repeatable: only these projects), --workers N (default 3: gentler on Autodesk's request limits), --token-env VAR (use an
           access token from this environment variable instead of signing in)

What is VERIFIED and what is not: endpoints, paging (page[limit] <= 200, links.next), the topFolders / folder-contents / included-tip-version
structure and the version attributes (name, fileType, storageSize, createTime, lastModifiedTime) follow Autodesk's published OpenAPI spec for
Data Management. The code is tested against a local imitation of that API, NOT yet against a real hub. Two things come straight from the
spec and shape the numbers: `storageSize` is "only present if this version represents a file", so a Fusion design (a composite design,
versions:autodesk.a360:CompositeDesign) most likely has no size here - such items get 0 bytes and are counted in Run_info.csv items_size_unknown;
and the spec does not list the item type of a Fusion design, so a design is recognised by its version/item type containing "compositedesign"
or "fusion360" and anything else without a file extension is written to Unknown_Item_Types.csv, never dropped or guessed. Every design is
counted as `f3d` (a part, as in the folder profiler): parts vs assemblies cannot be told from metadata alone.
"""
import argparse, base64, csv, datetime, hashlib, http.server, io, json, os, queue, re, secrets, sys, threading, time, urllib.error
import urllib.parse, urllib.request, webbrowser, zipfile
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.1"
API = os.environ.get("FUSION_API_BASE") or "https://developer.api.autodesk.com"   # the override exists for the tests only
SCOPE = "data:read"
REDIRECT_PORT = 8765
FOLDER_TYPES = {"folders"}
FILES_HEADER = ["project", "file_no", "ext", "size_bytes", "modified_utc", "depth", "header_signature", "dir_id", "asm_name", "rel_path",
                "archive", "sheets", "sheets_conf"]
EXT_RX = re.compile(r"\.([A-Za-z0-9_+-]{1,10})$")
EXT_OK = re.compile(r"^[a-z0-9_+-]{1,10}$")


class ApiError(Exception):
    pass


MAX_EVENTS = 20000


def _endpoint(url):
    """The kind of call, without hub / project / folder ids, for the run log."""
    path = urllib.parse.urlparse(url).path
    for key, kind in (("/contents", "folder contents"), ("/topFolders", "top folders"), ("/projects", "projects"), ("/hubs", "hubs"),
                      ("/authentication/", "sign-in")):
        if key in path:
            return kind
    return "call"


THROTTLE_RX = re.compile(r"capacity|rate.?limit|quota|too many|throttl", re.I)


def _is_throttle(code, why):
    """Autodesk answers 'slow down' with 429 - and sometimes with 403 + 'request capacity exceeded'. That is waiting-room, not a permission problem."""
    return code == 429 or (code == 403 and bool(THROTTLE_RX.search(why or "")))


def _body_of(e, limit=300):
    """Autodesk's own explanation of an HTTP error (a short JSON text such as invalid_client). Never raises."""
    try:
        text = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            bits = [str(d.get(k)) for k in ("error", "error_description", "developerMessage", "message", "reason") if d.get(k)]
            if bits:
                text = " - ".join(bits)
    except ValueError:
        pass
    return re.sub(r"\s+", " ", text).strip()[:limit]


class Api:
    def __init__(self, token, base=API, retries=6, refresher=None, throttle_retries=10):
        self.token, self.base, self.retries, self.refresher = token, base.rstrip("/"), retries, refresher
        self.throttle_retries = throttle_retries
        self.calls = 0
        self.retried = 0
        self.lock = threading.Lock()
        self.events, self.events_dropped, self.t0 = [], 0, time.time()   # (utc time, seconds since start, level, phase, message): becomes Run_log.csv

    def event(self, phase, message, level="INFO"):
        """One line of the run log. Never call it while holding self.lock."""
        with self.lock:
            if len(self.events) >= MAX_EVENTS:
                self.events_dropped += 1
                return
            self.events.append((datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), f"{time.time() - self.t0:.1f}", level, phase, message))

    def get(self, path_or_url):
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        kind = _endpoint(url)
        delay = 2.0
        refreshed = False
        last = ""                                     # why the most recent try failed: kept for the final error and the run log
        fails = throttles = 0                         # ordinary failures and "slow down" answers have separate budgets
        while True:
            sent = self.token
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + sent, "Accept": "application/json"})
            throttled = False
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    with self.lock:
                        self.calls += 1
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                why = _body_of(e)
                if e.code == 401 and self.refresher and not refreshed:
                    refreshed = True                  # access tokens last about an hour: get a new one once per call and retry
                    self.event("api", f"{kind}: 401 (token expired?) - asking Autodesk for a new access token" + (f" [{why}]" if why else ""), "WARN")
                    with self.lock:
                        if self.token == sent:
                            self.token = self.refresher()
                    continue
                if _is_throttle(e.code, why):         # 429, or 403 + "request capacity exceeded": wait, do not give up as 'no permission'
                    throttled = True
                    try:
                        wait = max(float(e.headers.get("Retry-After") or 0), delay)
                    except ValueError:
                        wait = delay
                    wait = min(wait, 120.0)
                    delay = min(delay * 2, 60)
                    last = f"HTTP {e.code} throttled" + (f" [{why}]" if why else "")
                elif e.code in (401, 403):
                    self.event("api", f"{kind}: refused ({e.code})" + (f" [{why}]" if why else ""), "ERROR")
                    raise ApiError(f"Autodesk refused access ({e.code}) to {url.split('?')[0]}: the signed-in user, or the app on this hub, "
                                   "has no read permission (the hub admin must add the app under Custom Integrations)"
                                   + (f" [Autodesk says: {why}]" if why else ""))
                elif e.code == 404:
                    self.event("api", f"{kind}: not found (404)" + (f" [{why}]" if why else ""), "WARN")
                    raise ApiError(f"not found (404): {url.split('?')[0]}" + (f" [{why}]" if why else ""))
                elif e.code >= 500:
                    wait = float(e.headers.get("Retry-After") or delay)
                    delay = min(delay * 2, 60)
                    last = f"HTTP {e.code}" + (f" [{why}]" if why else "")
                else:
                    self.event("api", f"{kind}: HTTP {e.code}" + (f" [{why}]" if why else ""), "ERROR")
                    raise ApiError(f"HTTP {e.code} for {url.split('?')[0]}" + (f" [Autodesk says: {why}]" if why else ""))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                wait, delay = delay, min(delay * 2, 60)
                reason = getattr(e, "reason", None) or e                   # URLError wraps the real cause (DNS, refused, timed out, TLS ...)
                last = f"{type(reason).__name__}" + (f" ({str(reason)[:120]})" if str(reason) else "")
            if throttled:
                throttles += 1
                budget, used, what = self.throttle_retries, throttles, "throttled"
            else:
                fails += 1
                budget, used, what = self.retries, fails, "failed"
            if used >= budget:
                self.event("api", f"{kind}: try {used} of {budget} {what} ({last})", "WARN")
                self.event("api", f"{kind}: gave up after {used} {what} tries ({last})", "ERROR")
                if throttled:
                    raise ApiError(f"Autodesk is throttling requests and kept refusing after {used} waits ({last}). This is a rate / capacity limit, "
                                   "not a permission problem: wait 10-30 minutes and run again (fewer workers, e.g. --workers 2, makes it gentler).")
                raise ApiError(f"gave up after {used} tries: {url.split('?')[0]}" + (f" (last problem: {last})" if last else ""))
            self.event("api", f"{kind}: try {used} of {budget} {what} ({last}); waiting {wait:.0f}s", "WARN")
            with self.lock:
                self.retried += 1
            time.sleep(wait)

    def pages(self, path):
        """Every page of a list call (follows links.next)."""
        url = path
        while url:
            doc = self.get(url)
            yield doc
            nxt = (doc.get("links") or {}).get("next") or {}
            url = nxt.get("href") if isinstance(nxt, dict) else None


def auth_begin(client_id, redirect, base=API):
    """Step 1 of the 3-legged OAuth PKCE sign-in (public client, no secret): -> (address to open in the browser, code verifier, state)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = f"{base}/authentication/v2/authorize?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": SCOPE, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"})
    return url, verifier, state


def auth_finish(client_id, redirect, code, verifier, base=API):
    """Step 2: exchange the code Autodesk sent to the callback for an access token. -> (access token, refresher). Nothing is stored on disk."""
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                   "redirect_uri": redirect, "code_verifier": verifier}).encode()
    req = urllib.request.Request(base + "/authentication/v2/token", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        why = _body_of(e)
        raise ApiError(f"Autodesk did not accept the sign-in ({e.code})" + (f" [Autodesk says: {why}]" if why else "")
                       + f": check the client id, that the app type is Desktop, Mobile, Single-Page App (no secret), and that the callback URL registered for the app is exactly {redirect}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"could not reach Autodesk to finish the sign-in ({type(getattr(e, 'reason', None) or e).__name__}): check the internet connection and any proxy / firewall")
    state_ = {"refresh": tok.get("refresh_token")}

    def refresher():
        if not state_["refresh"]:
            raise ApiError("the access token expired (about an hour) and Autodesk gave no refresh token: sign in again")
        data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": state_["refresh"], "client_id": client_id, "scope": SCOPE}).encode()
        rq = urllib.request.Request(base + "/authentication/v2/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(rq, timeout=60) as r2:
                t2 = json.loads(r2.read().decode())
        except urllib.error.HTTPError as e:
            why = _body_of(e)
            raise ApiError(f"could not refresh the sign-in ({e.code})" + (f" [Autodesk says: {why}]" if why else "") + ": sign in again")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(f"could not reach Autodesk to refresh the sign-in ({type(getattr(e, 'reason', None) or e).__name__}): sign in again")
        state_["refresh"] = t2.get("refresh_token") or state_["refresh"]
        return t2["access_token"]
    return tok["access_token"], refresher


def sign_in(client_id, base=API):
    """Command-line sign-in: opens the browser and waits for the callback on a loopback port. Returns (access token, refresher)."""
    redirect = f"http://localhost:{REDIRECT_PORT}/callback"
    url, verifier, state = auth_begin(client_id, redirect, base)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got["code"], got["state"], got["error"] = q.get("code", [""])[0], q.get("state", [""])[0], q.get("error", [""])[0]
            got["why"] = q.get("error_description", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), H)
    print("Opening the browser to sign in to Autodesk (read-only access: data:read). If it does not open, copy this address:\n  " + url)
    webbrowser.open(url)
    srv.timeout = 300
    srv.handle_request()
    srv.server_close()
    if got.get("error") or not got.get("code") or got.get("state") != state:
        raise SystemExit("Sign-in did not complete" + (f" ({got['error']}" + (f": {got['why'][:200]}" if got.get("why") else "") + ")" if got.get("error")
                                                        else " (no answer from Autodesk within 5 minutes, or the sign-in page was closed)" if not got.get("code")
                                                        else " (the answer did not match this sign-in; close old Autodesk tabs and try once)") + ".")
    return auth_finish(client_id, redirect, got["code"], verifier, base)


def hubs(api):
    out = []
    for doc in api.pages("/project/v1/hubs"):
        out += [(h["id"], (h.get("attributes") or {}).get("name") or h["id"]) for h in doc.get("data", [])]
    return out


def projects(api, hub_id):
    out = []
    for doc in api.pages(f"/project/v1/hubs/{urllib.parse.quote(hub_id, safe='')}/projects"):
        out += [(p["id"], (p.get("attributes") or {}).get("name") or p["id"]) for p in doc.get("data", [])]
    return out


def top_folders(api, hub_id, project_id):
    doc = api.get(f"/project/v1/hubs/{urllib.parse.quote(hub_id, safe='')}/projects/{urllib.parse.quote(project_id, safe='')}/topFolders")
    return [(f["id"], (f.get("attributes") or {}).get("displayName") or (f.get("attributes") or {}).get("name") or f["id"]) for f in doc.get("data", [])
            if f.get("type") in FOLDER_TYPES]


def _iso(s):
    """APS timestamps are ISO-8601 already; normalise to the profiler's 2026-01-31T12:00:00Z."""
    if not s:
        return ""
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def classify(item_attrs, version_attrs, name):
    """-> (ext, tag, unknown_type). Order: the version's fileType (spec: present when the version is a file), then an extension in the
    name, then a Fusion / A360 composite-design type -> f3d (drawing -> f2d), else '' and the type is reported."""
    types = " ".join(str((x.get("extension") or {}).get("type") or "") for x in (item_attrs, version_attrs)).lower()
    ft = str(version_attrs.get("fileType") or "").strip().lstrip(".").lower()
    if ft and EXT_OK.match(ft):
        return ft, "CLOUD", ""
    m = EXT_RX.search(name or "")
    if m:
        return m.group(1).lower(), "CLOUD", ""
    if "fusion360" in types or "compositedesign" in types:
        if "drawing" in types:
            return "f2d", "FUSION:Drawing", ""
        if any(w in types for w in ("schematic", "board", "pcb", "library", "electronics")):
            return "", "CLOUD", types.strip()          # Fusion Electronics type names are unknown: reported, not guessed
        return "f3d", "FUSION:Design", ""
    return "", "CLOUD", types.strip() or "(none)"


def _short(e):
    """Reason text without the URL, so identical failures are counted together."""
    return re.sub(r" (?:to|for) https?://\S*?(?=:\s|$)", "", str(e)).strip()


def dir_id(project_id, folder_id):
    return hashlib.sha256(f"{project_id}|{folder_id}".encode()).hexdigest()[:16]


def crawl_project(api, project_id, project_name, folders, sink, stats):
    """Depth-first walk of one project's folders; every item becomes a Files.csv row. sink(row) is thread-safe."""
    stack = [(fid, fname, 1, [], None) for fid, fname in folders]
    dirs = []
    while stack:
        fid, fname, depth, path, parent = stack.pop()
        here = path + [fname]
        did = dir_id(project_id, fid)
        dirs.append((did, project_name, parent or "", depth, fname))
        try:
            pages = list(api.pages(f"/data/v1/projects/{urllib.parse.quote(project_id, safe='')}/folders/{urllib.parse.quote(fid, safe='')}/contents?page%5Blimit%5D=200"))
        except ApiError as e:
            stats["unreadable"].append((project_name, "folder", "/".join(here), _short(e)))
            api.event("crawl", f"{project_name}: folder '{'/'.join(here)}' could not be read: {_short(e)}", "ERROR")
            continue
        for doc in pages:
            versions = {v["id"]: v for v in doc.get("included", []) if v.get("type") == "versions"}
            for it in doc.get("data", []):
                a = it.get("attributes") or {}
                if it.get("type") in FOLDER_TYPES:
                    stack.append((it["id"], a.get("displayName") or a.get("name") or it["id"], depth + 1, here, did))
                    continue
                tip = ((it.get("relationships") or {}).get("tip") or {}).get("data") or {}
                v = (versions.get(tip.get("id")) or {}).get("attributes") or {}
                name = a.get("displayName") or v.get("name") or it["id"]
                ext, tag, unknown = classify(a, v, name)
                if unknown:
                    stats["unknown_types"][unknown] = stats["unknown_types"].get(unknown, 0) + 1
                size = v.get("storageSize")
                if size is None:
                    stats["size_unknown"] += 1
                    size = 0
                modified = _iso(v.get("lastModifiedTime") or a.get("lastModifiedTime") or v.get("createTime") or a.get("createTime"))
                sink([project_name, 0, ext, int(size), modified, depth, tag, did, "", "/".join(here + [name]), "", "", ""])
    return dirs


def write_zip(path, hub_name, files, dirs, stats, started):
    by = {}
    for r in files:
        by.setdefault(r[0], []).append(r)
    ordered = []
    n = 0
    for proj in sorted(by):
        for r in by[proj]:
            n += 1
            r[1] = n
            ordered.append(r)

    def table(header, rows):
        b = io.StringIO(newline="")
        w = csv.writer(b)
        w.writerow(header)
        w.writerows(rows)
        return "﻿" + b.getvalue()
    proj_rows = []
    for proj, rows in sorted(by.items()):
        mods = sorted(r[4] for r in rows if r[4])
        proj_rows.append([proj, len(rows), sum(r[3] for r in rows), len({r[2] for r in rows}), mods[0] if mods else "", mods[-1] if mods else ""])
    info = [("tool_version", VERSION), ("run_utc", datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("source", "autodesk-cloud-hub"), ("folder_name", hub_name), ("mode", "cloud-metadata"), ("projects", len(by)), ("files", len(ordered)),
            ("total_bytes", sum(r[3] for r in ordered)), ("hashing", "none"), ("assembly_names", "none"), ("detail_level", 1), ("project_depth", 1),
            ("archives_mode", "off"), ("api_calls", stats["calls"]), ("api_retries", stats["retried"]), ("items_size_unknown", stats["size_unknown"]),
            ("unreadable_folders", len(stats["unreadable"])), ("unknown_item_types", sum(stats["unknown_types"].values())),
            ("log_events", len(stats.get("events", []))), ("log_events_dropped", stats.get("events_dropped", 0)),
            ("seconds", round(time.time() - started, 1))]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Files.csv", table(FILES_HEADER, ordered))
        z.writestr("Projects.csv", table(["project", "file_count", "total_bytes", "distinct_extensions", "first_modified_utc", "last_modified_utc"], proj_rows))
        z.writestr("Dirs.csv", table(["dir_id", "project", "parent_dir_id", "depth", "name"], dirs))
        z.writestr("Run_info.csv", table(["key", "value"], info))
        if stats["unreadable"]:
            z.writestr("Unreadable.csv", table(["project", "kind", "path", "reason"], stats["unreadable"]))
        if stats.get("events") is not None:
            z.writestr("Run_log.csv", table(["timestamp_utc", "elapsed_s", "level", "phase", "message"], stats["events"]))
        if stats["unknown_types"]:
            z.writestr("Unknown_Item_Types.csv", table(["autodesk_item_type", "items"], sorted(stats["unknown_types"].items())))
        z.writestr("WHAT_IS_IN_THIS_ZIP.txt",
                   "Made by the Fusion cloud profiler: read-only, metadata only. Per item: name, type, size, date, folder path of a design or file in an\n"
                   "Autodesk cloud hub. Nothing was downloaded or opened; no geometry, no file contents. Fusion designs are listed as f3d (cloud designs\n"
                   "have no extension; parts and assemblies cannot be told apart from metadata). Items with no size in the cloud (the API gives a size only for files) have size 0 and are\n"
                   "counted in Run_info.csv items_size_unknown. Open the CSVs with Excel or Notepad.\n")
    return len(ordered)


def write_stats_zip(path, stats, started, n_projects, n_items):
    """The run's diary as a SEPARATE small zip (like the folder profiler's run statistics): Run_log.csv and Run_stats.csv. It repeats what is
    already inside the profile zip so it can be sent or read on its own. No file contents; project and folder names appear in the log lines."""
    def table(header, rows):
        b = io.StringIO(newline="")
        w = csv.writer(b, lineterminator="\r\n")
        w.writerow(header)
        w.writerows(rows)
        return ("\ufeff" + b.getvalue()).encode("utf-8")
    levels = {}
    for ev in stats.get("events", []):
        levels[ev[2]] = levels.get(ev[2], 0) + 1
    rows = [("tool_version", VERSION), ("run_utc", datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("projects", n_projects), ("items", n_items), ("api_calls", stats.get("calls", 0)), ("api_retries", stats.get("retried", 0)),
            ("unreadable_folders", len(stats["unreadable"])), ("items_size_unknown", stats["size_unknown"]),
            ("log_events", len(stats.get("events", []))), ("log_events_dropped", stats.get("events_dropped", 0)),
            ("log_warnings", levels.get("WARN", 0)), ("log_errors", levels.get("ERROR", 0)), ("seconds", round(time.time() - started, 1))]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Run_log.csv", table(["timestamp_utc", "elapsed_s", "level", "phase", "message"], stats.get("events", [])))
        z.writestr("Run_stats.csv", table(["key", "value"], rows))
        z.writestr("README.txt", ("Run statistics from the Fusion cloud profiler: a timestamped diary of the run (retries and their causes, folders that could not be read) "
                                  "and a few counters. No file contents and no access token. Project and folder names can appear in the log lines.\r\n").encode("utf-8"))
    return path


def run(api, hub_id, hub_name, out_dir, only=None, workers=3, quiet=False, log=None):
    started = time.time()
    api.event("start", f"v{VERSION}: profiling hub '{hub_name}'" + (f", only {len(only)} project(s) asked for" if only else ""))
    try:
        projs = projects(api, hub_id)
    except ApiError as e:
        api.event("start", f"the project list could not be read: {_short(e)}", "ERROR")
        raise
    api.event("start", f"{len(projs)} project(s) in the hub")
    if only:
        want = {o.lower() for o in only}
        projs = [p for p in projs if p[1].lower() in want]
        missing = want - {p[1].lower() for p in projs}
        if missing:
            api.event("start", "no such project in the hub: " + ", ".join(sorted(missing)), "ERROR")
            raise SystemExit("No such project in the hub: " + ", ".join(sorted(missing)))
    files, dirs, lock = [], [], threading.Lock()
    stats = {"unreadable": [], "unknown_types": {}, "size_unknown": 0}

    def sink(row):
        with lock:
            files.append(row)

    def one(p):
        pid, pname = p
        t_p = time.time()
        api.event("project", f"{pname}: started")
        try:
            tf = top_folders(api, hub_id, pid)
        except ApiError as e:
            with lock:
                stats["unreadable"].append((pname, "folder", "(project)", _short(e)))
            api.event("project", f"{pname}: top folders could not be read, project skipped: {_short(e)}", "ERROR")
            return
        n_items = [0]

        def counting_sink(row):
            n_items[0] += 1
            sink(row)
        d = crawl_project(api, pid, pname, tf, counting_sink, stats)
        with lock:
            dirs.extend(d)
        api.event("project", f"{pname}: done ({n_items[0]:,} items, {len(d)} folders, {time.time() - t_p:.1f}s)")
        if log:
            log(f"{pname}: done")
        elif not quiet:
            print(f"  {time.strftime('%H:%M:%SZ', time.gmtime())}  {pname}: done", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = [f for f in [ex.submit(one, p) for p in projs]]
        for p, f in zip(projs, results):
            try:
                f.result()
            except Exception as e:                          # one project failing for an unexpected reason must not lose the others
                if isinstance(e, SystemExit):
                    raise
                stats["unreadable"].append((p[1], "folder", "(project)", f"unexpected {type(e).__name__}"))
                api.event("project", f"{p[1]}: stopped by an unexpected {type(e).__name__}: {str(e)[:200]}", "ERROR")
    stats["calls"], stats["retried"] = api.calls, api.retried
    api.event("finish", f"{len(files):,} items in {len(projs)} project(s); {api.calls} API calls, {api.retried} retried, "
                        f"{len(stats['unreadable'])} folder(s) not readable, {stats['size_unknown']} item(s) without a size")
    stats["events"], stats["events_dropped"] = list(api.events), api.events_dropped
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", hub_name).strip("_") or "hub"
    path = os.path.join(out_dir, f"fusion_{safe}_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    n = write_zip(path, hub_name, files, dirs, stats, started)
    stats["stats_zip"] = write_stats_zip(os.path.join(out_dir, os.path.basename(path).replace("fusion_", "fusion_run_stats_", 1)), stats, started, len(projs), n)
    return path, n, len(projs), stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", help="APS app client id (public client; no secret needed)")
    ap.add_argument("--token-env", help="read the access token from this environment variable instead of signing in")
    ap.add_argument("--hub", help="hub name or id to profile (omit to list the hubs)")
    ap.add_argument("--project", action="append", help="only this project (repeatable)")
    ap.add_argument("--out", default=".")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--api-base", default=API, help=argparse.SUPPRESS)
    a = ap.parse_args()
    token, refresher = (os.environ.get(a.token_env, "") if a.token_env else ""), None
    if not token:
        if not a.client_id:
            raise SystemExit("Give --client-id (or --token-env). See the top of this file for the one-time app setup.")
        token, refresher = sign_in(a.client_id, a.api_base)
    api = Api(token, a.api_base, refresher=refresher)
    try:
        hs = hubs(api)
        if not a.hub:
            print("Hubs this user can read:" if hs else "No hub is readable. The hub admin must allow the app (Custom Integrations).")
            for hid, hn in hs:
                print(f"  {hn}    ({hid})")
            return
        match = [h for h in hs if a.hub in (h[0], h[1])]
        if len(match) != 1:
            raise SystemExit(f"Hub '{a.hub}' not found" if not match else f"More than one hub is called '{a.hub}': pass its id")
        path, n, np_, stats = run(api, match[0][0], match[0][1], a.out, a.project, a.workers)
    except ApiError as e:
        raise SystemExit(str(e))
    print(f"\n{n} items in {np_} projects -> {path}")
    if stats["unreadable"]:
        print(f"  {len(stats['unreadable'])} folders could not be read (Unreadable.csv)")
    if stats["unknown_types"]:
        print(f"  items of unknown type (Unknown_Item_Types.csv): {sum(stats['unknown_types'].values())}")
    if stats["size_unknown"]:
        print(f"  items with no size in the cloud (counted as 0 bytes): {stats['size_unknown']}")
    print("Send this ONE file; the profile-compile skill reads it like any folder profile.")


if __name__ == "__main__":
    main()
