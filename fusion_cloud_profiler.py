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
  python fusion_cloud_profiler.py --client-id ID --hub "Name"    profile one hub  -> fusion_<hub>_<date>.zip   (Step 2, the full profile)
  python fusion_cloud_profiler.py --client-id ID --hub "Name" --step 1   quick count: items and bytes per project and file type, NO names -> quick_fusion_<hub>_<date>.zip
  options: --out DIR, --project NAME (repeatable: only these projects), --workers N (default 3: gentler on Autodesk's request limits), --token-env VAR (use an
           access token from this environment variable instead of signing in), --step 1|2, --fresh (ignore saved progress)

Steps, saved progress and rate limits:
  Both steps list the hub through ONE resumable listing (a SQLite file in <out>/.resume-fusion/<id>/, no token and no contents in it). Step 1 keeps it and writes
  counts only; Step 2 reuses a Step 1 listing younger than FUSION_REUSE_HOURS (default 24) with no new calls to Autodesk, then deletes it. A run that stops,
  crashes or gives up carries on from what is saved - also when a newer version of this tool is run (STATE_FORMAT says when that is not possible).
  Rate limits: ONE governor for all threads. A throttle answer (429, 403 'capacity exceeded', 503) pauses every thread together (longer of Retry-After and a doubling
  pause, with jitter), halves the requests allowed at once and slows the pace; speed comes back slowly after good answers. A call is given up only after
  `patience` (30 min) of throttling, and then as an unfinished listing, not a failure: run again later and only what is left is listed.

What is VERIFIED and what is not: endpoints, paging (page[limit] <= 200, links.next), the topFolders / folder-contents / included-tip-version
structure and the version attributes (name, fileType, storageSize, createTime, lastModifiedTime) follow Autodesk's published OpenAPI spec for
Data Management. The code is tested against a local imitation of that API, NOT yet against a real hub. Two things come straight from the
spec and shape the numbers: `storageSize` is "only present if this version represents a file", so a Fusion design (a composite design,
versions:autodesk.a360:CompositeDesign) most likely has no size here - such items get 0 bytes and are counted in Run_info.csv items_size_unknown;
and the spec does not list the item type of a Fusion design, so a design is recognised by its version/item type containing "compositedesign"
or "fusion360" and anything else without a file extension is written to Unknown_Item_Types.csv, never dropped or guessed. Every design is
counted as `f3d` (a part, as in the folder profiler): parts vs assemblies cannot be told from metadata alone.
"""
import argparse, base64, csv, datetime, email.utils, hashlib, http.server, io, json, os, queue, random, re, secrets, sqlite3, sys, threading, time, urllib.error
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


class TransientError(ApiError):
    """A failure that may well pass by itself - Autodesk throttling for a long time, the network down. Progress is saved: run again later."""


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


def retry_after_seconds(value):
    """The Retry-After header is either a number of seconds or an HTTP date. -> seconds (0 when absent or unreadable)."""
    if not value:
        return 0.0
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return 0.0


class Governor:
    """ONE policy for every request of a run, shared by all worker threads (before, each thread backed off alone while the others went on
    hammering a service that had just said 'slow down'):
      - a throttle answer (429, 403 'capacity', 503) pauses EVERY thread until the same moment (the longer of Retry-After and a doubling
        pause, with random jitter so threads do not all return together), halves how many requests may run at once (down to 1) and slows
        the pace (a minimum gap between request starts);
      - many throttle answers that arrive together are one episode: they do not halve / double again and again;
      - after the pause, successes win the speed back slowly: one more concurrent request every `grow_every` good answers, and the gap shrinks.
    Everything is measured (stats) and reported in Run_info.csv, so the log can say how much time the rate limit cost."""

    def __init__(self, workers=3, base_pause=2.0, max_pause=120.0, max_gap=5.0, grow_every=20, floor_gap=0.0, notify=None, rng=None):
        self.max_limit = max(1, int(workers))
        self.limit = self.max_limit                       # requests allowed at the same moment (shrinks on throttle, grows back on success)
        self.base_pause, self.max_pause, self.max_gap, self.grow_every, self.floor_gap = base_pause, max_pause, max_gap, grow_every, floor_gap
        self.gap = floor_gap                              # seconds between two request starts (all threads together)
        self.cv = threading.Condition()
        self.active, self.next_start, self.blocked_until = 0, 0.0, 0.0
        self.streak, self.episodes_in_a_row = 0, 0
        self.notify = notify or (lambda msg, level="INFO": None)
        self.rng = rng or random.Random()
        self.stats = {"throttled": 0, "episodes": 0, "wait_s": 0.0, "min_limit": self.limit, "max_gap": self.gap, "longest_pause_s": 0.0}

    def acquire(self):
        """Wait for a turn: a free slot, outside any pause, and the pacing gap since the previous start."""
        t0 = time.monotonic()
        with self.cv:
            while True:
                now = time.monotonic()
                ready = max(self.blocked_until, self.next_start)
                if self.active < self.limit and now >= ready:
                    break
                self.cv.wait(timeout=max(0.02, min(1.0, ready - now)) if now < ready else 0.25)
            self.active += 1
            self.next_start = time.monotonic() + self.gap
            waited = time.monotonic() - t0
            if waited > 0.05:
                self.stats["wait_s"] += waited

    def release(self):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()

    def success(self):
        grew = None
        with self.cv:
            if time.monotonic() >= self.blocked_until:
                self.episodes_in_a_row = 0
            self.streak += 1
            if self.streak >= self.grow_every and time.monotonic() >= self.blocked_until:
                self.streak = 0
                if self.limit < self.max_limit:
                    self.limit += 1
                    grew = f"requests are going through again: allowing {self.limit} at a time"
                self.gap = max(self.floor_gap, self.gap * 0.7) if self.gap > self.floor_gap + 0.01 else self.floor_gap
            self.cv.notify_all()
        if grew:
            self.notify(grew)

    def throttled(self, retry_after=0.0, why="throttled"):
        """A 'slow down' answer. -> seconds every thread will now be held for (0 if this answer belongs to an episode already handled)."""
        msg = None
        with self.cv:
            now = time.monotonic()
            self.stats["throttled"] += 1
            self.streak = 0
            new_episode = now >= self.blocked_until
            if new_episode:
                self.episodes_in_a_row += 1
                self.stats["episodes"] += 1
                self.limit = max(1, self.limit // 2)
                self.gap = min(self.max_gap, max(self.gap * 2, 0.25))
                pause = max(retry_after, min(self.max_pause, self.base_pause * (2 ** (self.episodes_in_a_row - 1))))
                pause = min(self.max_pause, pause * self.rng.uniform(0.85, 1.3))
                self.blocked_until = now + pause
                self.stats["min_limit"] = min(self.stats["min_limit"], self.limit)
                self.stats["max_gap"] = max(self.stats["max_gap"], self.gap)
                self.stats["longest_pause_s"] = max(self.stats["longest_pause_s"], pause)
                msg = (f"Autodesk said slow down ({why}): everything waits {pause:.0f}s, then goes on with {self.limit} request(s) at a time and "
                       f"{self.gap:.2f}s between requests")
            elif retry_after > 0 and now + retry_after > self.blocked_until:
                self.blocked_until = min(now + self.max_pause, now + retry_after)       # a later, longer Retry-After extends the pause
                pause = self.blocked_until - now
            else:
                pause = 0.0
            self.cv.notify_all()
        if msg:
            self.notify(msg, "WARN")
        return pause

    def snapshot(self):
        with self.cv:
            return dict(self.stats, limit=self.limit, gap=self.gap)


class Api:
    def __init__(self, token, base=API, retries=6, refresher=None, throttle_retries=100, workers=3, patience=1800.0, governor=None):
        self.token, self.base, self.retries, self.refresher = token, base.rstrip("/"), retries, refresher
        self.throttle_retries = throttle_retries          # a safety cap on 'slow down' answers for ONE call; the real limit is `patience`
        self.patience = patience                          # seconds one call keeps being throttled before it is given up (progress is saved anyway)
        self.calls = 0
        self.retried = 0
        self.lock = threading.Lock()
        self.events, self.events_dropped, self.t0 = [], 0, time.time()   # (utc time, seconds since start, level, phase, message): becomes Run_log.csv
        self.on_event = None                                             # set by run(): events are also kept in the state file, so a resumed run keeps its diary
        self.echo = None                                                 # set by run(): warnings, errors and the start / finish lines are also shown live (page, terminal)
        self.gov = governor or Governor(workers, notify=lambda msg, level="INFO": self.event("throttle", msg, level))

    def event(self, phase, message, level="INFO"):
        """One line of the run log. Never call it while holding self.lock."""
        with self.lock:
            if len(self.events) >= MAX_EVENTS:
                self.events_dropped += 1
                return
            ev = (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), f"{time.time() - self.t0:.1f}", level, phase, message)
            self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass
        if self.echo and (level != "INFO" or phase in ("start", "finish")):
            try:
                self.echo(("WARNING: " if level == "WARN" else "ERROR: " if level == "ERROR" else "") + message)
            except Exception:
                pass

    def get(self, path_or_url):
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        kind = _endpoint(url)
        delay = 2.0
        refreshed = False
        last = ""                                     # why the most recent try failed: kept for the final error and the run log
        fails = throttles = 0                         # ordinary failures and "slow down" answers have separate budgets
        t_throttled = None                            # when this call was first told to slow down
        while True:
            self.gov.acquire()                        # a turn: a free slot, outside any pause, at the shared pace
            sent = self.token
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + sent, "Accept": "application/json"})
            throttled, doc, wait, retry_after, why = False, None, 0.0, 0.0, ""
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    doc = json.loads(r.read().decode("utf-8"))
                with self.lock:
                    self.calls += 1
            except urllib.error.HTTPError as e:
                err = e
                why = _body_of(e)
                retry_after = retry_after_seconds(e.headers.get("Retry-After"))
                if _is_throttle(e.code, why) or e.code == 503:       # told to slow down: the governor hears it WHILE this slot is still held, so no waiting
                    throttled = True                                  # thread can slip through in the instant between this answer and the pause starting
                    self.gov.throttled(retry_after, f"HTTP {e.code}" + (f" [{why}]" if why else ""))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                err = e
            else:
                err = None
            finally:
                self.gov.release()                    # the slot is free while this thread waits or handles the answer
            if err is None:
                self.gov.success()
                return doc
            if isinstance(err, urllib.error.HTTPError):
                e = err
                if e.code == 401 and self.refresher and not refreshed:
                    refreshed = True                  # access tokens last about an hour: get a new one once per call and retry
                    self.event("api", f"{kind}: 401 (token expired?) - asking Autodesk for a new access token" + (f" [{why}]" if why else ""), "WARN")
                    with self.lock:
                        if self.token == sent:
                            self.token = self.refresher()
                    continue
                if throttled:                         # 429, 403 + "request capacity exceeded", 503: slow down for everyone, do not give up as 'no permission'
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
                    wait = max(retry_after, delay)
                    delay = min(delay * 2, 60)
                    last = f"HTTP {e.code}" + (f" [{why}]" if why else "")
                else:
                    self.event("api", f"{kind}: HTTP {e.code}" + (f" [{why}]" if why else ""), "ERROR")
                    raise ApiError(f"HTTP {e.code} for {url.split('?')[0]}" + (f" [Autodesk says: {why}]" if why else ""))
            else:
                wait, delay = delay, min(delay * 2, 60)
                reason = getattr(err, "reason", None) or err                   # URLError wraps the real cause (DNS, refused, timed out, TLS ...)
                last = f"{type(reason).__name__}" + (f" ({str(reason)[:120]})" if str(reason) else "")
            if throttled:
                throttles += 1
                t_throttled = t_throttled or time.monotonic()
                over = (time.monotonic() - t_throttled) > self.patience or throttles >= self.throttle_retries
                if over:
                    self.event("api", f"{kind}: gave up after {throttles} throttled answers over {time.monotonic() - t_throttled:.0f}s ({last})", "ERROR")
                    raise TransientError(f"Autodesk kept throttling this request for {time.monotonic() - t_throttled:.0f}s ({last}). This is a rate / capacity limit, "
                                         "not a permission problem. Nothing is lost: run again later (10-30 minutes) and it carries on from here.")
                if throttles <= 10 or throttles % 10 == 0:     # every wait is in the log at first, then every tenth (a long throttling must not drown the log)
                    self.event("api", f"{kind}: throttled ({last}); waiting with everyone else", "WARN")
                with self.lock:
                    self.retried += 1
                continue                              # the next turn is granted only after the shared pause
            fails += 1
            if fails >= self.retries:
                self.event("api", f"{kind}: try {fails} of {self.retries} failed ({last})", "WARN")
                self.event("api", f"{kind}: gave up after {fails} failed tries ({last})", "ERROR")
                raise TransientError(f"gave up after {fails} tries: {url.split('?')[0]}" + (f" (last problem: {last})" if last else "")
                                     + ". Nothing is lost: run again and it carries on from here.")
            wait *= random.uniform(0.85, 1.25)        # jitter: threads that failed together do not retry together
            self.event("api", f"{kind}: try {fails} of {self.retries} failed ({last}); waiting {wait:.0f}s", "WARN")
            with self.lock:
                self.retried += 1
            time.sleep(wait)

    def combined(self):
        """This process's request statistics added to those saved by the earlier parts of the same run (a resumed run reports the whole run)."""
        g, p = self.gov.snapshot(), getattr(self, "prior", None) or {}
        return {"calls": self.calls + p.get("calls", 0), "retried": self.retried + p.get("retried", 0),
                "throttled": g["throttled"] + p.get("throttled", 0), "episodes": g["episodes"] + p.get("episodes", 0),
                "wait_s": g["wait_s"] + p.get("wait_s", 0.0), "longest_pause_s": max(g["longest_pause_s"], p.get("longest_pause_s", 0.0)),
                "min_limit": min(g["min_limit"], p.get("min_limit", g["min_limit"])), "max_gap": max(g["max_gap"], p.get("max_gap", 0.0))}

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


STATE_FORMAT = 1                 # layout of the saved progress (.resume-fusion/<id>/state.db): raise it ONLY when what is saved changes meaning. A new VERSION of
                                 # the tool carries on from the saved progress of an older one while this number is the same.
REUSE_HOURS = float(os.environ.get("FUSION_REUSE_HOURS", "24"))   # how old a finished listing may be and still be reused (Step 1 -> Step 2) without asking Autodesk again


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """The run's saved progress: every project, folder (pending / done / error) and item the listing found, in one SQLite file next to the output.
    A folder's items and its 'done' mark are written in ONE transaction, so a crash can never leave half a folder. No token and no file contents
    are ever stored - only what the listing returns (names, types, sizes, dates). The same file is how Step 2 reuses Step 1's listing and how a
    run - even by a newer version of the tool - carries on after it was stopped, crashed or gave up under heavy throttling."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=NORMAL")
        for ddl in ("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
                    "CREATE TABLE IF NOT EXISTS projects(pid TEXT PRIMARY KEY, name TEXT, status TEXT, reason TEXT, transient INTEGER)",
                    "CREATE TABLE IF NOT EXISTS folders(pid TEXT, fid TEXT, parent TEXT, depth INTEGER, name TEXT, path TEXT, status TEXT, reason TEXT, "
                    "transient INTEGER, PRIMARY KEY(pid, fid))",
                    "CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT, pid TEXT, fid TEXT, project TEXT, ext TEXT, size INTEGER, "
                    "size_known INTEGER, modified TEXT, depth INTEGER, tag TEXT, rel_path TEXT, unknown TEXT)",
                    "CREATE INDEX IF NOT EXISTS items_by_project ON items(project, rel_path)",
                    "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT, el TEXT, level TEXT, phase TEXT, msg TEXT)"):
            self.db.execute(ddl)

    # ---- small helpers
    def meta(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))

    def one(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def clear_listing(self):
        with self.lock:
            for t in ("projects", "folders", "items"):
                self.db.execute(f"DELETE FROM {t}")

    # ---- events (the run log survives a restart)
    def add_event(self, ev):
        with self.lock:
            self.db.execute("INSERT INTO events(t, el, level, phase, msg) VALUES (?,?,?,?,?)", ev)

    def events(self):
        return [tuple(r) for r in self.all("SELECT t, el, level, phase, msg FROM events ORDER BY id")]

    # ---- projects and folders
    def add_projects(self, rows):
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO projects(pid, name, status, reason, transient) VALUES (?,?, 'new', '', 0)", rows)

    def reset_errors(self):
        """Folders / projects that failed last time are tried again: a throttled-out folder is exactly what a second run is for."""
        with self.lock:
            self.db.execute("UPDATE folders SET status='pending', reason='', transient=0 WHERE status='error'")
            self.db.execute("UPDATE projects SET status='new', reason='', transient=0 WHERE status='error'")

    def set_top_folders(self, pid, pname, folders):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR IGNORE INTO folders(pid, fid, parent, depth, name, path, status, reason, transient) VALUES (?,?,?,?,?,?, 'pending', '', 0)",
                                [(pid, fid, "", 1, fname, fname) for fid, fname in folders])
            self.db.execute("UPDATE projects SET status='ready' WHERE pid=?", (pid,))
            self.db.execute("COMMIT")

    def fail_project(self, pid, reason, transient):
        with self.lock:
            self.db.execute("UPDATE projects SET status='error', reason=?, transient=? WHERE pid=?", (reason, int(transient), pid))

    def pending_folders(self):
        return self.all("SELECT pid, fid, parent, depth, name, path FROM folders WHERE status='pending' ORDER BY pid, depth, path")

    def finish_folder(self, pid, fid, items, children):
        """items: rows for the items table; children: (fid, parent dir id, depth, name, path) of sub-folders. One transaction."""
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM items WHERE pid=? AND fid=?", (pid, fid))                  # a folder re-listed after a stale / partial earlier try
            self.db.executemany("INSERT INTO items(pid, fid, project, ext, size, size_known, modified, depth, tag, rel_path, unknown) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(pid, fid) + tuple(r) for r in items])
            self.db.executemany("INSERT OR IGNORE INTO folders(pid, fid, parent, depth, name, path, status, reason, transient) VALUES (?,?,?,?,?,?, 'pending', '', 0)",
                                [(pid,) + tuple(c) for c in children])
            self.db.execute("UPDATE folders SET status='done', reason='', transient=0 WHERE pid=? AND fid=?", (pid, fid))
            self.db.execute("COMMIT")

    def fail_folder(self, pid, fid, reason, transient):
        with self.lock:
            self.db.execute("UPDATE folders SET status='error', reason=?, transient=? WHERE pid=? AND fid=?", (reason, int(transient), pid, fid))

    def close(self):
        with self.lock:                                   # waits for a statement another thread is running: closing under it can crash the process
            try:
                self.db.close()
            except Exception:
                pass


def state_dir_for(out_dir, hub_id, project_ids):
    ident = hashlib.sha256((hub_id + "\0" + "\0".join(sorted(project_ids))).encode()).hexdigest()[:16]
    return os.path.join(out_dir, ".resume-fusion", ident)


def _item_rows(doc, project_name, pid, fid, depth, here, did, stats):
    """Rows for one page of a folder's contents: items, and the sub-folders found. Same reading of the listing as before (tip version -> type, size, date)."""
    versions = {v["id"]: v for v in doc.get("included", []) if v.get("type") == "versions"}
    items, children = [], []
    for it in doc.get("data", []):
        a = it.get("attributes") or {}
        if it.get("type") in FOLDER_TYPES:
            nm = a.get("displayName") or a.get("name") or it["id"]
            children.append((it["id"], did, depth + 1, nm, "/".join(here + [nm])))
            continue
        tip = ((it.get("relationships") or {}).get("tip") or {}).get("data") or {}
        v = (versions.get(tip.get("id")) or {}).get("attributes") or {}
        name = a.get("displayName") or v.get("name") or it["id"]
        ext, tag, unknown = classify(a, v, name)
        size = v.get("storageSize")
        modified = _iso(v.get("lastModifiedTime") or a.get("lastModifiedTime") or v.get("createTime") or a.get("createTime"))
        items.append((project_name, ext, int(size) if size is not None else 0, 0 if size is None else 1, modified, depth, tag, "/".join(here + [name]), unknown))
    return items, children


def crawl(api, store, hub_id, workers, log=None):
    """List every pending folder of every project. A pool of threads works through one shared queue of folders (so one huge project is shared
    too); every request goes through the governor (shared pause / pace on throttling). Each folder is saved as soon as it is listed. -> counts."""
    names = {r[0]: r[1] for r in store.all("SELECT pid, name FROM projects")}
    for pid, pname in list(names.items()):
        st = store.one("SELECT status FROM projects WHERE pid=?", (pid,))[0]
        if st == "new":
            t_p = time.time()
            try:
                tf = top_folders(api, hub_id, pid)
            except TransientError as e:
                store.fail_project(pid, _short(e), True)
                api.event("project", f"{pname}: top folders could not be read yet (will be tried again next run): {_short(e)}", "ERROR")
                continue
            except ApiError as e:
                store.fail_project(pid, _short(e), False)
                api.event("project", f"{pname}: top folders could not be read, project skipped: {_short(e)}", "ERROR")
                continue
            store.set_top_folders(pid, pname, tf)
            api.event("project", f"{pname}: {len(tf)} top folder(s)")
    q = queue.Queue()
    todo = store.pending_folders()
    outstanding = [len(todo)]
    done_evt = threading.Event()
    lock = threading.Lock()
    per_project = {}                                  # project id -> [folders still to list, items found this run, folders listed this run]
    for r in todo:
        per_project.setdefault(r[0], [0, 0, 0])[0] += 1
        q.put(r)
    if not todo:
        done_evt.set()

    crash, abort = [], threading.Event()

    def work():
        try:
            work_loop()
        except BaseException as e:                    # a stop, a crash, an unexpected error: end the whole crawl and let the caller see it. The folder
            crash.append(e)                           # being listed is still 'pending' in the saved progress, so the next run lists it again
            abort.set()
            done_evt.set()

    def work_loop():
        while True:
            row = q.get()
            if row is None or abort.is_set():
                return
            pid, fid, parent, depth, fname, path = row
            pname = names.get(pid, pid)
            here = path.split("/") if path else [fname]
            did = dir_id(pid, fid)
            children, n_items = [], 0
            try:
                pages = list(api.pages(f"/data/v1/projects/{urllib.parse.quote(pid, safe='')}/folders/{urllib.parse.quote(fid, safe='')}/contents?page%5Blimit%5D=200"))
                items = []
                for doc in pages:
                    i_, c_ = _item_rows(doc, pname, pid, fid, depth, here, did, None)
                    items += i_
                    children += c_
                store.finish_folder(pid, fid, items, children)
                n_items = len(items)
            except TransientError as e:
                children = []
                store.fail_folder(pid, fid, _short(e), True)
                api.event("crawl", f"{pname}: folder '{path}' could not be listed yet (kept for the next run): {_short(e)}", "ERROR")
            except ApiError as e:
                children = []
                store.fail_folder(pid, fid, _short(e), False)
                api.event("crawl", f"{pname}: folder '{path}' could not be read: {_short(e)}", "ERROR")
            except Exception as e:                    # an unexpected failure in one folder must not lose the others
                children = []
                store.fail_folder(pid, fid, f"unexpected {type(e).__name__}", False)
                api.event("crawl", f"{pname}: folder '{path}' stopped by an unexpected {type(e).__name__}: {str(e)[:200]}", "ERROR")
            with lock:                                # the counts are settled BEFORE the children become visible to other threads: a fast child
                outstanding[0] += len(children) - 1   # must never be able to bring the count to zero while its siblings are still unqueued
                pp = per_project[pid]
                pp[0] += len(children) - 1
                pp[1] += n_items
                pp[2] += 1
                project_done = pp[0] == 0
                finished = outstanding[0] == 0
                counts = (pp[1], pp[2])
            for c in children:
                q.put((pid, c[0], c[1], c[2], c[3], c[4]))
            if project_done:
                api.event("project", f"{pname}: listed ({counts[0]:,} items in {counts[1]} folders this run)")
                if log:
                    log(f"{pname}: listed")
            if finished:
                done_evt.set()
    # children discovered while listing are queued by the worker that found them (their per-project count was raised above through kids_added)
    threads = [threading.Thread(target=work, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    try:
        while not done_evt.wait(0.5):
            pass
    finally:
        abort.set()
        for _ in threads:
            q.put(None)
        for t in threads:                                 # no thread may still be using the saved progress when the caller closes it
            t.join(timeout=15)
    if crash:
        raise crash[0]
    return per_project


def status_of(store):
    """-> (complete, transient_failures, permanent_failures, pending): is every project and folder listed, and what is left."""
    pend = store.one("SELECT COUNT(*) FROM folders WHERE status='pending'")[0] + store.one("SELECT COUNT(*) FROM projects WHERE status='new'")[0]
    trans = store.one("SELECT COUNT(*) FROM folders WHERE status='error' AND transient=1")[0] + store.one("SELECT COUNT(*) FROM projects WHERE status='error' AND transient=1")[0]
    perm = store.one("SELECT COUNT(*) FROM folders WHERE status='error' AND transient=0")[0] + store.one("SELECT COUNT(*) FROM projects WHERE status='error' AND transient=0")[0]
    return (pend == 0 and trans == 0), trans, perm, pend


def _table(header, rows):
    b = io.StringIO(newline="")
    w = csv.writer(b)
    w.writerow(header)
    w.writerows(rows)
    return "﻿" + b.getvalue()


def _run_info(store, api, stats, started, step, extra):
    gov = api.combined()
    versions = store.meta("versions", [])
    base = [("tool_version", VERSION), ("run_utc", _utcnow()), ("source", "autodesk-cloud-hub"), ("step", step),
            ("versions_used", " -> ".join(v["v"] for v in versions)), ("restarts_resumed", store.meta("restarts", 0))]
    tail = [("api_calls", gov["calls"]), ("api_retries", gov["retried"]), ("api_throttled_answers", gov["throttled"]),
            ("throttle_episodes", gov["episodes"]), ("seconds_waiting_for_rate_limit", round(gov["wait_s"], 1)),
            ("longest_rate_limit_pause_s", round(gov["longest_pause_s"], 1)), ("lowest_concurrency_reached", gov["min_limit"]),
            ("slowest_pace_s_between_requests", round(gov["max_gap"], 2)), ("items_size_unknown", stats["size_unknown"]),
            ("unreadable_folders", len(stats["unreadable"])), ("unknown_item_types", sum(stats["unknown_types"].values())),
            ("log_events", len(stats.get("events", []))), ("log_events_dropped", stats.get("events_dropped", 0)),
            ("seconds", round(time.time() - started, 1))]
    return base + extra + tail


def export_full(path, hub_name, store, api, stats, started):
    """Step 2: the full profile zip, streamed from the saved listing (memory stays flat however many items there are). Same files and columns as
    before; rows are ordered by project then path, so the order is the same however the run was split by stops and resumes."""
    projects_ = store.all("SELECT project, COUNT(*), SUM(size), COUNT(DISTINCT ext), MIN(NULLIF(modified, '')), MAX(NULLIF(modified, '')) "
                          "FROM items GROUP BY project ORDER BY project")
    n_items = sum(r[1] for r in projects_)
    total_bytes = sum(r[2] or 0 for r in projects_)
    extra = [("folder_name", hub_name), ("mode", "cloud-metadata"), ("projects", len(projects_)), ("files", n_items), ("total_bytes", total_bytes),
             ("hashing", "none"), ("assembly_names", "none"), ("detail_level", 1), ("project_depth", 1), ("archives_mode", "off")] + extra_listing(store)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("Files.csv", "w", force_zip64=True) as out:
            out.write(_table(FILES_HEADER, []).encode("utf-8"))
            n = 0
            buf = io.StringIO(newline="")
            w = csv.writer(buf)
            cur = store.db.execute("SELECT project, ext, size, modified, depth, tag, pid, fid, rel_path FROM items ORDER BY project, rel_path, id")
            for proj, ext, size, mod, depth, tag, pid_, fid_, rel in cur:
                n += 1
                w.writerow([proj, n, ext, size, mod, depth, tag, dir_id(pid_, fid_), "", rel, "", "", ""])
                if n % 5000 == 0:
                    out.write(buf.getvalue().encode("utf-8")); buf.seek(0); buf.truncate()
            out.write(buf.getvalue().encode("utf-8"))
        z.writestr("Projects.csv", _table(["project", "file_count", "total_bytes", "distinct_extensions", "first_modified_utc", "last_modified_utc"],
                                          [[r[0], r[1], r[2] or 0, r[3], r[4] or "", r[5] or ""] for r in projects_]))
        z.writestr("Dirs.csv", _table(["dir_id", "project", "parent_dir_id", "depth", "name"],
                                      [[dir_id(r[0], r[1]), r[2], r[3], r[4], r[5]] for r in store.all(
                                          "SELECT f.pid, f.fid, p.name, f.parent, f.depth, f.name FROM folders f JOIN projects p ON p.pid = f.pid "
                                          "ORDER BY p.name, f.depth, f.path")]))
        z.writestr("Run_info.csv", _table(["key", "value"], _run_info(store, api, stats, started, 2, extra)))
        if stats["unreadable"]:
            z.writestr("Unreadable.csv", _table(["project", "kind", "path", "reason"], stats["unreadable"]))
        if stats.get("events") is not None:
            z.writestr("Run_log.csv", _table(["timestamp_utc", "elapsed_s", "level", "phase", "message"], stats["events"]))
        if stats["unknown_types"]:
            z.writestr("Unknown_Item_Types.csv", _table(["autodesk_item_type", "items"], sorted(stats["unknown_types"].items())))
        z.writestr("WHAT_IS_IN_THIS_ZIP.txt",
                   "Made by the Fusion cloud profiler (Step 2, full profile): read-only, metadata only. Per item: name, type, size, date, folder path of a design or file in an\n"
                   "Autodesk cloud hub. Nothing was downloaded or opened; no geometry, no file contents. Fusion designs are listed as f3d (cloud designs\n"
                   "have no extension; parts and assemblies cannot be told apart from metadata). Items with no size in the cloud (the API gives a size only for files) have size 0 and are\n"
                   "counted in Run_info.csv items_size_unknown. Run_info.csv also says which versions of the tool worked on the run, whether the listing was reused\n"
                   "from Step 1, and how much the rate limit cost. Open the CSVs with Excel or Notepad.\n")
    return n_items


def extra_listing(store):
    made = store.meta("listed_utc", "")
    reused = store.meta("reused_listing", False)
    return [("listing_made_utc", made), ("listing_reused_from_earlier_step", "yes" if reused else "no"), ("listing_complete", "yes")]


def export_quick(path, hub_name, store, api, stats, started):
    """Step 1: counts per project and file type only - NO item or folder names (the folder profiler's quick count, for a cloud hub). The listing
    stays in the saved progress so Step 2 can reuse it."""
    by = store.all("SELECT project, ext, COUNT(*), SUM(size) FROM items GROUP BY project, ext ORDER BY project, COUNT(*) DESC, ext")
    allx = store.all("SELECT ext, COUNT(*), SUM(size) FROM items GROUP BY ext ORDER BY COUNT(*) DESC, ext")
    proj = store.all("SELECT project, COUNT(*), SUM(size), COUNT(DISTINCT ext) FROM items GROUP BY project ORDER BY project")
    n_items = sum(r[1] for r in proj)
    nbytes = sum(r[2] or 0 for r in proj)
    nfolders = store.one("SELECT COUNT(*) FROM folders WHERE status='done'")[0]
    extra = [("folder_name", hub_name), ("mode", "quick count (fusion cloud)"), ("projects", len(proj)), ("project_depth", 1), ("files", n_items),
             ("total_bytes", nbytes), ("folders", nfolders), ("distinct_extensions", len(allx)), ("files_opened", 0), ("hashing", "none"),
             ("names_anonymized", "no")] + extra_listing(store)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Extensions.csv", _table(["project", "ext", "file_count", "total_bytes", "avg_bytes"], [[r[0], r[1], r[2], r[3] or 0, (r[3] or 0) // max(1, r[2])] for r in by]))
        z.writestr("Extensions_All.csv", _table(["ext", "file_count", "total_bytes", "share_of_files_pct", "share_of_bytes_pct"],
                                                [[r[0], r[1], r[2] or 0, f"{100 * r[1] / max(1, n_items):.2f}", f"{100 * (r[2] or 0) / max(1, nbytes):.2f}"] for r in allx]))
        z.writestr("Projects.csv", _table(["project", "file_count", "total_bytes", "distinct_extensions"], [[r[0], r[1], r[2] or 0, r[3]] for r in proj]))
        z.writestr("Unreadable.csv", _table(["project", "kind", "path", "reason"], stats["unreadable"]))
        z.writestr("Run_info.csv", _table(["key", "value"], _run_info(store, api, stats, started, 1, extra)))
        if stats.get("events") is not None:
            z.writestr("Run_log.csv", _table(["timestamp_utc", "elapsed_s", "level", "phase", "message"], stats["events"]))
        if stats["unknown_types"]:
            z.writestr("Unknown_Item_Types.csv", _table(["autodesk_item_type", "items"], sorted(stats["unknown_types"].items())))
        z.writestr("WHAT_IS_IN_THIS_ZIP.txt",
                   "Made by the Fusion cloud profiler (Step 1, quick count): read-only, metadata only. Per project and file type: how many items and how many bytes. No\n"
                   "item or folder names are recorded (only project names and the paths of folders that could not be read). Fusion designs count as f3d; designs have no\n"
                   "size in the cloud list, so size totals are understated (Run_info.csv items_size_unknown). Step 2 (full profile) reuses this listing for a day.\n")
    return n_items


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


class Incomplete(TransientError):
    """The listing is not finished (Autodesk throttled too long, or the network was down for some folders). Progress is saved: run again later."""


def run(api, hub_id, hub_name, out_dir, only=None, workers=3, quiet=False, log=None, step=2, fresh=False, reuse_hours=None):
    """Step 1 (quick count) or Step 2 (full profile) of a hub. Both list the hub through the same resumable crawl; Step 2 reuses a Step 1 listing
    that is less than `reuse_hours` old (no new API calls). -> (zip path, items, projects, stats)."""
    started = time.time()
    if log is not None:
        api.echo = log
    elif not quiet:
        api.echo = lambda m: print(f"  {time.strftime('%H:%M:%SZ', time.gmtime())}  {m}", flush=True)
    reuse_hours = REUSE_HOURS if reuse_hours is None else reuse_hours
    step = 1 if int(step) == 1 else 2
    api.gov.max_limit = max(1, int(workers))
    api.gov.limit = min(api.gov.limit, api.gov.max_limit)
    api.event("start", f"v{VERSION}: step {step} of hub '{hub_name}'" + (f", only {len(only)} project(s) asked for" if only else ""))
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
    sdir = state_dir_for(out_dir, hub_id, [p[0] for p in projs])
    if fresh:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)
    store = Store(os.path.join(sdir, "state.db"))
    try:
        # ---- carry on from saved progress (of any version of the tool with the same state format)
        fmt = store.meta("format")
        if fmt is not None and fmt != STATE_FORMAT:
            api.event("start", f"saved progress is in another state format ({fmt}, this version uses {STATE_FORMAT}): starting from the beginning", "WARN")
            store.close()
            import shutil
            shutil.rmtree(sdir, ignore_errors=True)
            store = Store(os.path.join(sdir, "state.db"))
            fmt = None
        resumed = fmt is not None
        store.set_meta("format", STATE_FORMAT)
        store.set_meta("hub_id", hub_id)
        api.prior = store.meta("totals", {}) if resumed else {}
        versions = store.meta("versions", [])
        if not versions:
            versions = [{"v": VERSION, "utc": _utcnow()}]
        elif versions[-1]["v"] != VERSION:
            api.event("start", f"the saved progress was made by v{versions[-1]['v']}; carrying on from it with v{VERSION} - nothing that was listed is listed again", "INFO")
            versions.append({"v": VERSION, "utc": _utcnow()})
        store.set_meta("versions", versions)
        if resumed:
            store.set_meta("restarts", store.meta("restarts", 0) + 1)
        earlier = store.events()
        for ev in list(api.events):                                          # what this run has already said is saved too
            store.add_event(ev)
        api.events[:0] = earlier[-(MAX_EVENTS // 2):]                      # the diary of the earlier part of this run comes first
        api.on_event = store.add_event
        if earlier:
            api.event("start", f"RESUMED (restart #{store.meta('restarts', 0)}): the log above is from the earlier part of this run")
        store.add_projects([(p[0], p[1]) for p in projs])
        # ---- is a finished listing still good? (decided BEFORE failed folders are retried: a folder nobody may read must not force a new listing every time)
        complete, trans, perm, pending = status_of(store)
        listed = store.meta("listed_utc", "")
        reused = False
        if complete and listed:
            age_h = (time.time() - datetime.datetime.strptime(listed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()) / 3600
            if age_h <= reuse_hours:
                reused = True
                api.event("start", f"the hub was listed {age_h:.1f} h ago (Step {store.meta('listed_by_step', 1)}): reusing that listing - no calls to Autodesk. "
                                   f"Tick 'Start from scratch' to list again")
            else:
                api.event("start", f"the saved listing is {age_h:.0f} h old (more than {reuse_hours:.0f} h): listing the hub again")
                store.clear_listing()
                store.add_projects([(p[0], p[1]) for p in projs])
                store.set_meta("listed_utc", "")
        store.set_meta("reused_listing", reused)
        if not reused:
            store.reset_errors()                                              # folders that failed last time are tried again
            if resumed and pending:
                api.event("start", f"{pending} folder(s) / project(s) were still to list: carrying on from there")
            per = crawl(api, store, hub_id, workers, log=log)
            complete, trans, perm, pending = status_of(store)
            if not complete:
                msg = (f"the listing is not finished: {trans} folder(s) / project(s) could not be listed yet (Autodesk throttled for too long, or the network was down)"
                       + (f", {pending} not reached" if pending else "") + ". Everything listed so far is saved - run it again later (10-30 minutes) and it carries on from here.")
                api.event("finish", msg, "ERROR")
                raise Incomplete(msg)
            store.set_meta("listed_utc", _utcnow())
            store.set_meta("listed_by_step", step)
        # ---- numbers for the report, from the saved listing
        stats = {"unreadable": [], "unknown_types": {}, "size_unknown": 0}
        for pid, name, reason in store.all("SELECT pid, name, reason FROM projects WHERE status='error'"):
            stats["unreadable"].append((name, "folder", "(project)", reason))
        for pname, path_, reason in store.all("SELECT p.name, f.path, f.reason FROM folders f JOIN projects p ON p.pid = f.pid WHERE f.status='error' ORDER BY p.name, f.path"):
            stats["unreadable"].append((pname, "folder", path_, reason))
        stats["size_unknown"] = store.one("SELECT COUNT(*) FROM items WHERE size_known=0")[0]
        for u, n_ in store.all("SELECT unknown, COUNT(*) FROM items WHERE unknown != '' GROUP BY unknown"):
            stats["unknown_types"][u] = n_
        n_total = store.one("SELECT COUNT(*) FROM items")[0]
        api.event("finish", f"{n_total:,} items in {len(projs)} project(s); {api.calls} API calls, {api.retried} retried, "
                            f"{len(stats['unreadable'])} folder(s) not readable, {stats['size_unknown']} item(s) without a size")
        stats["calls"], stats["retried"] = api.combined()["calls"], api.combined()["retried"]
        stats["events"], stats["events_dropped"] = list(api.events), api.events_dropped
        os.makedirs(out_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", hub_name).strip("_") or "hub"
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        if step == 1:
            path = os.path.join(out_dir, f"quick_fusion_{safe}_{stamp}.zip")
            n = export_quick(path, hub_name, store, api, stats, started)
            stats["top"] = [[e, c, b or 0] for e, c, b in store.all("SELECT ext, COUNT(*), SUM(size) FROM items GROUP BY ext ORDER BY COUNT(*) DESC, ext LIMIT 12")]
            stats["distinct_extensions"] = store.one("SELECT COUNT(DISTINCT ext) FROM items")[0]
        else:
            path = os.path.join(out_dir, f"fusion_{safe}_{stamp}.zip")
            n = export_full(path, hub_name, store, api, stats, started)
        stats["step"], stats["reused"], stats["total_bytes"] = step, reused, store.one("SELECT COALESCE(SUM(size), 0) FROM items")[0]
        stats["stats_zip"] = write_stats_zip(os.path.join(out_dir, os.path.basename(path).replace("fusion_", "fusion_run_stats_", 1)
                                                         if step == 2 else os.path.basename(path).replace("quick_fusion_", "fusion_run_stats_quick_", 1)),
                                             stats, started, len(projs), n)
        keep = (step == 1)
    except BaseException:
        keep = True                                                          # any failure: the saved progress stays
        raise
    finally:
        try:
            store.set_meta("totals", api.combined())                         # the cost so far, for the next part of this run to add to
        except Exception:
            pass
        store.close()
        if not keep:                                                         # a finished Step 2 is the end of this hub's saved progress
            import shutil
            shutil.rmtree(sdir, ignore_errors=True)
            try:
                os.rmdir(os.path.dirname(sdir))                              # the .resume-fusion folder itself, when nothing else is in it
            except OSError:
                pass
    return path, n, len(projs), stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", help="APS app client id (public client; no secret needed)")
    ap.add_argument("--token-env", help="read the access token from this environment variable instead of signing in")
    ap.add_argument("--hub", help="hub name or id to profile (omit to list the hubs)")
    ap.add_argument("--project", action="append", help="only this project (repeatable)")
    ap.add_argument("--out", default=".")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--step", type=int, choices=(1, 2), default=2, help="1 = quick count (per project and file type, no names), 2 = full profile (default); "
                    "Step 2 reuses a Step 1 listing made less than 24 h ago (FUSION_REUSE_HOURS) without asking Autodesk again")
    ap.add_argument("--fresh", action="store_true", help="ignore saved progress and a saved listing: list the hub again from the beginning")
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
        path, n, np_, stats = run(api, match[0][0], match[0][1], a.out, a.project, a.workers, step=a.step, fresh=a.fresh)
    except ApiError as e:
        raise SystemExit(str(e))
    print(f"\n{n} items in {np_} projects -> {path}")
    if a.step == 1:
        print("This is the quick count (no names). Run again with --step 2 for the full profile: it reuses this listing (no new calls to Autodesk) for 24 hours.")
    if stats["unreadable"]:
        print(f"  {len(stats['unreadable'])} folders could not be read (Unreadable.csv)")
    if stats["unknown_types"]:
        print(f"  items of unknown type (Unknown_Item_Types.csv): {sum(stats['unknown_types'].values())}")
    if stats["size_unknown"]:
        print(f"  items with no size in the cloud (counted as 0 bytes): {stats['size_unknown']}")
    print("Send this ONE file; the profile-compile skill reads it like any folder profile.")


if __name__ == "__main__":
    main()
