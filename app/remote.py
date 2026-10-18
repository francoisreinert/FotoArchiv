"""FotoArchiv von anderen Rechnern im Heimnetz bedienen (opt-in, mit Passwort).

Der normale Server bleibt auf 127.0.0.1. Ist „Im Heimnetz bedienen“ an, lauscht ein zweiter Server auf allen
Netzwerkkarten (Port PORTS) mit demselben Handler, verlangt aber für Anfragen, die nicht von diesem Rechner kommen,
eine Anmeldung (Cookie fa_remote). Nur private Adressen (Heimnetz) – Anfragen aus dem Internet werden abgewiesen.
Einstellungen und Prüfwert in data/remote.json (nicht in config.json: die geht an die Oberfläche und in die Sicherung).
"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time

import common

FILE = os.path.join(common.DATA_DIR, "remote.json")
COOKIE = "fa_remote"
PORTS = range(8775, 8780)
ITERATIONS = 200_000
SESSION_DAYS = 30
LOCAL_ONLY = ("/api/remote/", "/api/quit", "/api/library")  # nur am Rechner selbst
_lock = threading.Lock()
_fails = {}  # ip -> [Zeitpunkte fehlgeschlagener Anmeldungen]
_server = None
_port = None


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(d):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, FILE)


def _hash(pw, salt, it):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), it).hex()


def set_password(pw):
    with _lock:
        d = load()
        d["salt"] = secrets.token_hex(16)
        d["iter"] = ITERATIONS
        d["hash"] = _hash(pw, d["salt"], ITERATIONS)
        d["sessions"] = {}  # neues Passwort meldet alle ab
        save(d)


def set_on(on):
    with _lock:
        d = load()
        d["on"] = bool(on)
        save(d)


def enabled():
    d = load()
    return bool(d.get("on") and d.get("hash"))


def is_local(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_loopback


def is_home(ip):
    """Heimnetz: private Adressen (192.168.x, 10.x, 172.16–31.x, fe80::, fd00::) – nie Internet."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_private or a.is_link_local


def login(ip, pw):
    """Gibt (token, None) oder (None, Fehlertext) zurück. Nach 5 Fehlversuchen in 10 min: Pause."""
    now = time.time()
    with _lock:
        f = [t for t in _fails.get(ip, []) if now - t < 600]
        _fails[ip] = f
        if len(f) >= 5:
            return None, "Zu viele Fehlversuche – bitte in einigen Minuten erneut versuchen"
        d = load()
        if not d.get("hash") or not hmac.compare_digest(_hash(pw or "", d["salt"], d.get("iter", ITERATIONS)), d["hash"]):
            f.append(now)
            time.sleep(1)
            return None, "Passwort falsch"
        tok = secrets.token_urlsafe(32)
        s = {k: v for k, v in (d.get("sessions") or {}).items() if v > now}
        s[tok] = now + SESSION_DAYS * 86400
        d["sessions"] = s
        save(d)
        _fails.pop(ip, None)
        return tok, None


def valid(tok):
    if not tok:
        return False
    exp = (load().get("sessions") or {}).get(tok)
    return bool(exp and exp > time.time())


def logout(tok=None):
    """Ein Gerät (tok) oder alle abmelden."""
    with _lock:
        d = load()
        s = d.get("sessions") or {}
        if tok:
            s.pop(tok, None)
        else:
            s = {}
        d["sessions"] = s
        save(d)


def lan_url():
    if not _port:
        return None
    try:
        import lan

        return "http://%s:%d" % (lan.lan_ip(), _port)
    except Exception:
        return None


def status():
    d = load()
    now = time.time()
    return {"on": bool(d.get("on")), "password": bool(d.get("hash")), "running": bool(_server), "url": lan_url(),
            "devices": sum(1 for v in (d.get("sessions") or {}).values() if v > now)}


def start(handler_cls, server_cls):
    """Zweiten Server auf allen Netzwerkkarten starten (falls eingeschaltet)."""
    global _server, _port
    if _server or not enabled():
        return bool(_server)
    for port in PORTS:
        try:
            srv = server_cls(("0.0.0.0", port), handler_cls)
            break
        except OSError:
            continue
    else:
        return False
    srv.daemon_threads = True
    srv.remote = True  # Handler.route ruft dann gate() auf
    _server, _port = srv, port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def stop():
    global _server, _port
    srv, _server, _port = _server, None, None
    if srv:
        threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()


LOGIN_HTML = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>FotoArchiv – Anmelden</title>
<style>body{font-family:system-ui,sans-serif;background:#16181c;color:#e8e8e8;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0}form{background:#22252b;padding:28px;border-radius:12px;width:min(320px,90vw)}
h1{font-size:20px;margin:0 0 6px}p{color:#9aa0a6;font-size:14px;margin:0 0 16px}input,button{width:100%;box-sizing:border-box;
padding:10px;border-radius:8px;border:1px solid #3a3f47;background:#16181c;color:inherit;font-size:16px;margin-bottom:10px}
button{background:#3b82f6;border:0;cursor:pointer}#e{color:#f87171;min-height:1.2em;font-size:14px}</style></head>
<body><form id="f"><h1>FotoArchiv</h1><p>Zugang im Heimnetz – Passwort eingeben.</p>
<input type="password" id="pw" autocomplete="current-password" autofocus placeholder="Passwort">
<button>Anmelden</button><div id="e"></div></form>
<script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();
const r=await fetch("/api/remote-login",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({password:document.getElementById("pw").value})});const d=await r.json().catch(()=>({}));
if(r.ok)location.href="/";else document.getElementById("e").textContent=d.error||"Fehler";};</script></body></html>"""


def _cookie(h):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def gate(h, method, path):
    """Vor jeder Anfrage über den Heimnetz-Server: True = weiter, False = schon beantwortet."""
    ip = h.client_address[0]
    if is_local(ip):
        return True
    if not is_home(ip) or not enabled():
        h.send_error(403)
        return False
    tok = _cookie(h)
    if path == "/api/remote-login" and method == "POST":
        t, err = login(ip, h.body().get("password"))
        if not t:
            h.send_json({"error": err}, 403)
            return False
        h._extra_headers.append(("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                                 % (COOKIE, t, SESSION_DAYS * 86400)))
        h.send_json({"ok": True})
        return False
    if path == "/api/remote-logout" and method == "POST":
        logout(tok)
        h._extra_headers.append(("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % COOKIE))
        h.send_json({"ok": True})
        return False
    if not valid(tok):
        if path.startswith("/api/"):
            h.send_json({"error": "Bitte anmelden"}, 401)
        elif method == "GET" and path in ("/", "/index.html", "/login"):
            h.send_bytes(LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8", cache=0)
        else:
            h.send_error(401)
        return False
    if path.startswith(LOCAL_ONLY):
        h.send_json({"error": "Nur am FotoArchiv-Rechner selbst möglich"}, 403)
        return False
    h._remote = True
    return True
