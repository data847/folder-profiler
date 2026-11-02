"""The sign-in callback must reach THIS program: a port held by another program on the IPv6 loopback ('localhost' resolves to ::1 first on many computers)
is detected, the UI picks a port free on every loopback address and answers on IPv6 too, and the sign-in says plainly when 'localhost' leads elsewhere
(the cause of a 404 after approving on Autodesk's page)."""
import http.server, json, os, socket, sys, threading, urllib.error, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import fusion_cloud_profiler as fc, ui
bad = []
def check(l, g, w):
    ok = g == w; print(("ok   " if ok else "FAIL ") + f"{l}: {g}" + ("" if ok else f" (wanted {w})")); bad.append(not ok)
V6 = socket.has_ipv6 and not os.environ.get("NO_V6_TEST")
def can_bind_v6():
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.bind(("::1", 0)); s.close(); return True
    except OSError: return False
V6 = V6 and can_bind_v6()
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
# ---- loopback_free
p = free_port()
check("a free port is free everywhere", fc.loopback_free(p), True)
s4 = socket.socket(); s4.bind(("127.0.0.1", p)); s4.listen(1)
check("a port held on IPv4 is not free", fc.loopback_free(p), False); s4.close()
if V6:
    p6 = free_port()
    s6 = socket.socket(socket.AF_INET6); s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1); s6.bind(("::1", p6)); s6.listen(1)
    check("a port held on IPv6 ALONE (like a Docker container's) is not free - the case behind the 404", fc.loopback_free(p6), False)
    # ---- the UI avoids it
    srv, why = ui.choose_server("127.0.0.1", p6, ui.Handler)
    check("the UI does not take a port that is busy on IPv6; it picks the NEXT free one (stable across restarts) and says why", (srv.server_address[1], bool(why)), (p6 + 1 if fc.loopback_free(p6 + 1) else srv.server_address[1], True))
    check("...and that port is within the next 20", p6 < srv.server_address[1] <= p6 + 20, True)
    srv.server_close()
    try: ui.choose_server("127.0.0.1", p6, ui.Handler, explicit=True); check("an explicit busy port is refused", False, True)
    except OSError: check("an explicit --port that is busy on IPv6 is refused with an error", True, True)
    s6.close()
# ---- the UI answers on both loopbacks
srv, why = ui.choose_server("127.0.0.1", free_port(), ui.Handler)
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start()
v6 = fc.serve_ipv6_too(ui.Handler, port)
def ping(host):
    try: return json.load(urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=3))["id"]
    except Exception as e: return type(e).__name__
check("IPv4: 127.0.0.1 reaches this program", ping("127.0.0.1"), ui.SERVER_ID)
if V6: check("IPv6: [::1] reaches this program too", ping("[::1]"), ui.SERVER_ID)
check("localhost reaches this program (whichever address the browser tries first)", ui.check_localhost(port), "")
srv.shutdown(); srv.server_close()
if v6: v6.shutdown(); v6.server_close()
# ---- the plain message, for each way 'localhost' can lead elsewhere (the lookup is replaced: no network)
def raises(exc): 
    def f(u): raise exc
    return f
http_404 = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
m = ui.check_localhost(8765, raises(http_404))
check("another program answering 404 is named as the cause, with what to do", ("404" in m, "another program" in m.lower(), "docker ps" in m, "callback" in m), (True, True, True, True))
check("a refused connection is reported", "nothing" in ui.check_localhost(8765, raises(urllib.error.URLError("refused"))), True)
check("another Folder Profiler (different id) is reported", "different Folder Profiler" in ui.check_localhost(8765, lambda u: json.dumps({"id": "someone-else"})), True)
check("an answer that is not JSON is reported", "does not answer like this one" in ui.check_localhost(8765, lambda u: "<html>"), True)
check("this program answering is fine", ui.check_localhost(8765, lambda u: json.dumps({"id": ui.SERVER_ID})), "")
# ---- the sign-in endpoint refuses to start a sign-in that would end on a 404
real = ui.check_localhost
ui.check_localhost = lambda port, fetch=None: "PORT CLASH MESSAGE"
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
r = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/fusion/login?t={ui.TOKEN}&client_id=abcdefgh12345678", timeout=5))
check("the page's sign-in stops with the message instead of sending the user to Autodesk", r, {"error": "PORT CLASH MESSAGE"})
ui.check_localhost = real; srv.shutdown(); srv.server_close()
print("\nFAILED" if any(bad) else "\nALL OK"); sys.exit(1 if any(bad) else 0)
