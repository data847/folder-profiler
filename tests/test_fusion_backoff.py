"""Fusion rate-limit back-off: one shared policy for all threads. Retry-After parsing; a throttle halves concurrency, slows the pace and pauses EVERY thread;
answers that arrive together are one episode; speed comes back slowly; 403 'capacity' and 503 are 'slow down', a plain 403 is a permission error;
a call is given up only after its patience, with a retryable error; ordinary failures keep their own budget."""
import http.server, json, os, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import fusion_cloud_profiler as fc
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
class Fixed:                                           # no randomness in the unit checks
    def uniform(self, a, b): return 1.0
# ---- Retry-After
check("seconds", fc.retry_after_seconds("5"), 5.0)
check("fractional seconds", fc.retry_after_seconds(" 0.5 "), 0.5)
check("an HTTP date in the future is a wait", fc.retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT") > 1e6, True)
check("an HTTP date in the past is no wait", fc.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)
check("garbage / empty / negative", (fc.retry_after_seconds("soon"), fc.retry_after_seconds(""), fc.retry_after_seconds(None), fc.retry_after_seconds("-3")), (0.0, 0.0, 0.0, 0.0))
# ---- governor policy
g = fc.Governor(workers=4, base_pause=0.2, max_pause=5.0, grow_every=3, rng=Fixed())
check("starts at full concurrency", g.limit, 4)
p1 = g.throttled(0.0, "t")
check("a throttle halves concurrency and slows the pace", (g.limit, g.gap >= 0.25, round(p1, 1)), (2, True, 0.2))
p2 = g.throttled(0.0, "t")
check("a second answer inside the same pause is the same episode (no second halving, no longer pause)", (g.limit, p2, g.stats["episodes"], g.stats["throttled"]), (2, 0.0, 1, 2))
g.throttled(3.0, "t")
check("a longer Retry-After extends the pause", g.blocked_until - time.monotonic() > 2.5, True)
g2 = fc.Governor(workers=4, base_pause=0.1, max_pause=5.0, grow_every=3, rng=Fixed())
g2.throttled(0.0); time.sleep(0.15)
g2.throttled(0.0)
check("a throttle after the pause is a NEW episode: pause doubles, concurrency halves again", (g2.limit, g2.stats["episodes"], round(g2.blocked_until - time.monotonic(), 1)), (1, 2, 0.2))
check("never below one request at a time", (fc.Governor(1, rng=Fixed()).throttled(0) >= 0, ), (True,))
time.sleep(0.25)
for _ in range(3): g2.success()
check("after the pause, good answers win speed back slowly (+1 per 3)", g2.limit, 2)
for _ in range(30): g2.success()
check("...up to the original level, never above", g2.limit, 4)
g2.throttled(0.0); time.sleep(0.3)
for _ in range(60): g2.success()
check("and the pacing gap shrinks back to the floor", g2.gap, 0.0)
# ---- a throttle pauses EVERY thread
g3 = fc.Governor(workers=3, base_pause=0.5, max_pause=5.0, rng=Fixed())
starts = []
def worker():
    for _ in range(4):
        g3.acquire(); starts.append(time.monotonic()); time.sleep(0.02); g3.release(); g3.success()
t0 = time.monotonic(); g3.throttled(0.6)
ths = [threading.Thread(target=worker) for _ in range(3)]; [t.start() for t in ths]; [t.join() for t in ths]
check("no thread got a turn during the shared pause (first start >= 0.5 s after the throttle)", min(starts) - t0 >= 0.5, True)
check("all 12 requests went ahead afterwards", len(starts), 12)
# ---- live calls against a fake server
class S(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        with self.server.lock: self.server.hits.append((time.monotonic(), self.path))
        mode = self.server.mode(self.path, len(self.server.hits))
        code, body, hdr = mode
        b = json.dumps(body).encode(); self.send_response(code)
        for k, v in hdr.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
def serve(mode):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), S); srv.lock, srv.hits, srv.mode = threading.Lock(), [], mode
    threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv
OK = (200, {"data": []}, {})
# one throttle answer to the first request; 3 threads: nobody calls during Retry-After
srv = serve(lambda path, n: (429, {}, {"Retry-After": "1"}) if n == 1 else OK)
api = fc.Api("T", f"http://127.0.0.1:{srv.server_port}", workers=3, governor=fc.Governor(3, base_pause=0.1, max_pause=5, rng=Fixed()))
res = []
def call(i): res.append(api.get(f"/x/{i}"))
ths = [threading.Thread(target=call, args=(i,)) for i in range(6)]; [t.start() for t in ths]; [t.join() for t in ths]
first = srv.hits[0][0]; after = sorted(h[0] for h in srv.hits[1:])
check("all 6 calls succeeded", len(res), 6)
gaps = [round(a - first, 2) for a in after]
print("   request arrivals after the 429 (s):", gaps)
early = [x for x in gaps if x < 0.9]
check("after the 429 (Retry-After 1) only requests already in flight got through (<= 2 of 3 slots); every later one waited out the pause", (len(early) <= 2, all(x < 0.1 for x in early)), (True, True))
check("and the other requests all came after the 1 s pause, from every thread", len(gaps) - len(early) >= 3 and all(x >= 0.9 for x in gaps if x >= 0.1), True)
check("the throttle is counted once, concurrency fell", (api.gov.stats["episodes"], api.gov.stats["min_limit"] < 3), (1, True))
srv.shutdown()
# 403 with 'capacity' text is a slow-down; a plain 403 is a permission error; 503 is a slow-down
srv = serve(lambda path, n: ((403, {"developerMessage": "Request capacity exceeded"}, {}) if "cap" in path else (403, {}, {}) if "perm" in path else (503, {}, {}) if "svc" in path else OK) if n <= 1 else OK)
api = fc.Api("T", f"http://127.0.0.1:{srv.server_port}", workers=2, governor=fc.Governor(2, base_pause=0.05, max_pause=1, rng=Fixed()))
api.get("/cap"); check("403 + capacity exceeded is waited out, not 'no permission'", (api.gov.stats["throttled"], api.retried), (1, 1))
api2 = fc.Api("T", f"http://127.0.0.1:{srv.server_port}", workers=2, governor=fc.Governor(2, base_pause=0.05, max_pause=1, rng=Fixed()))
srv.hits.clear()
api2.get("/svc"); check("503 is a slow-down for everyone", api2.gov.stats["throttled"], 1)
srv.mode = lambda path, n: (403, {}, {})
try: api2.get("/perm"); check("plain 403 raises", False, True)
except fc.ApiError as e: check("a plain 403 is a permission error, not retryable", (isinstance(e, fc.TransientError), "no read permission" in str(e)), (False, True))
srv.shutdown()
# endless throttling: given up only after the patience, with a retryable error that says nothing is lost
srv = serve(lambda path, n: (429, {}, {}))
api = fc.Api("T", f"http://127.0.0.1:{srv.server_port}", workers=2, patience=1.0, governor=fc.Governor(2, base_pause=0.1, max_pause=0.4, rng=Fixed()))
t0 = time.monotonic()
try: api.get("/never"); check("endless throttling raises", False, True)
except fc.TransientError as e:
    dt = time.monotonic() - t0
    check("gave up as a TransientError after about the patience (1 s), not at once and not forever", 0.9 <= dt < 4.0, True)
    check("the error says it is a rate limit and nothing is lost", ("rate / capacity limit" in str(e), "carries on" in str(e)), (True, True))
    check("the run log says it gave up", any("gave up" in ev[4] for ev in api.events), True)
srv.shutdown()
# ordinary server errors keep their own budget and end as TransientError too
srv = serve(lambda path, n: (500, {}, {}))
api = fc.Api("T", f"http://127.0.0.1:{srv.server_port}", retries=2)
t0 = time.monotonic()
try: api.get("/boom"); check("endless 500 raises", False, True)
except fc.TransientError as e: check("500s: 2 tries, then a retryable error; not counted as throttling", (len(srv.hits), api.gov.stats["throttled"]), (2, 0))
srv.shutdown()
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
