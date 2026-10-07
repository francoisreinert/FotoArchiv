"""Freigabe im Heimnetz: ausgewählte Fotos/Videos für Handy (QR-Code) und Fernseher (DLNA) bereitstellen.

Läuft getrennt vom eigentlichen FotoArchiv (das nur auf diesem Computer erreichbar ist) und liefert
ausschließlich Dateien aus, für die gerade eine Freigabe besteht – über zufällige, ablaufende Links.
"""
import io
import os
import secrets
import socket
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common
from common import connect, to_abs

PORTS = range(8780, 8800)
_shares = {}   # token -> {"ids": [...], "until": ts, "kind": "phone"|"tv"}
_lock = threading.Lock()
_server = None
_port = None

VIDEO_MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "qt": "video/quicktime",
              "mts": "video/vnd.dlna.mpeg-tts", "m2ts": "video/vnd.dlna.mpeg-tts", "avi": "video/x-msvideo",
              "mkv": "video/x-matroska", "3gp": "video/3gpp", "wmv": "video/x-ms-wmv", "mpg": "video/mpeg"}


def lan_ip():
    """IP-Adresse dieses Computers im Heimnetz (es wird nichts gesendet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 9))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def base_url():
    ensure_server()
    return "http://%s:%d" % (lan_ip(), _port)


def create(ids, kind="phone", minutes=15):
    token = secrets.token_urlsafe(16)
    with _lock:
        now = time.time()
        for t in [t for t, s in _shares.items() if s["until"] < now]:
            _shares.pop(t, None)
        _shares[token] = {"ids": list(ids), "until": now + minutes * 60, "kind": kind}
    return token


def create_file(path, minutes=360):
    """Eine einzelne Datei freigeben (z. B. ein Diashow-Video vom Schreibtisch)."""
    token = secrets.token_urlsafe(16)
    with _lock:
        _shares[token] = {"ids": [], "file": path, "until": time.time() + minutes * 60, "kind": "file"}
    return token


def add_ids(token, ids):
    """Weitere Fotos zu einer bestehenden Freigabe hinzufügen (Mitlauf-Modus im Betrachter)."""
    with _lock:
        s = _shares.get(token)
        if s is not None:
            s["ids"].extend(i for i in ids if i not in s["ids"])
            s["until"] = max(s["until"], time.time() + 6 * 3600)


def extend(token, minutes):
    with _lock:
        if token in _shares:
            _shares[token]["until"] = time.time() + minutes * 60


def revoke(token):
    with _lock:
        _shares.pop(token, None)


def _get(token):
    with _lock:
        s = _shares.get(token)
        if s and s["until"] >= time.time():
            return s
    return None


def ensure_server():
    global _server, _port
    if _server:
        return
    for port in PORTS:
        try:
            srv = _Server(("0.0.0.0", port), _Handler)
        except OSError:
            continue
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _server, _port = srv, port
        return
    raise RuntimeError("Kein freier Port für die Freigabe im Heimnetz")


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# --------------------------------------------------------------- Inhalte ----

_jpeg_cache = {}
_cache_lock = threading.Lock()


def prefetch(iid):
    """Bild für den Fernseher schon im Hintergrund vorbereiten (schneller Wechsel)."""
    def run():
        try:
            row = _item(iid)
            if row and row[2] != "video":
                image_bytes(row, "tv")
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def _item(iid):
    con = connect()
    try:
        return con.execute("SELECT id, path, kind, ext, name, userrot, taken FROM items WHERE id=?", (iid,)).fetchone()
    finally:
        con.close()


def image_bytes(row, size):
    """JPEG in passender Größe (richtig gedreht, HEIC/RAW umgewandelt)."""
    key = (row[0], size)
    if key in _jpeg_cache:
        return _jpeg_cache[key]
    import media
    from PIL import Image

    if size == "tv":
        import frame

        # 16:9 mit unscharfem Rand, Full HD: reicht für die Diashow und ist doppelt so schnell wie 4K
        data = frame.frame_image(to_abs(row[1]), row[2], row[5], size=(1920, 1080))
    else:
        import common

        im = media.open_image(to_abs(row[1]), row[2], max_side=2560, userrot=row[5] or 0,
                              edit=common.edit_for(to_abs(row[1])))[0]
        im.thumbnail((2560, 2560), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        data = buf.getvalue()
    with _cache_lock:
        if len(_jpeg_cache) > 40:
            _jpeg_cache.pop(next(iter(_jpeg_cache)))
    _jpeg_cache[key] = data
    return data


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FotoArchiv – %(n)d Fotos</title>
<style>body{margin:0;font:15px system-ui,-apple-system,sans-serif;background:#111;color:#eee}
header{padding:14px 16px;position:sticky;top:0;background:#1a1d20;border-bottom:1px solid #333}
h1{font-size:17px;margin:0 0 4px}p{margin:4px 0;color:#aaa;font-size:13px}
a.btn{display:inline-block;margin-top:8px;padding:9px 14px;background:#2f7fd8;color:#fff;border-radius:9px;text-decoration:none}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:4px;padding:4px}
.g a{display:block;position:relative}.g img{width:100%%;aspect-ratio:1;object-fit:cover;display:block}
.g span{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.6);padding:0 6px;border-radius:4px;font-size:12px}
</style></head><body><header><h1>%(n)d Fotos von FotoArchiv</h1>
<p>iPhone: Foto antippen, dann gedrückt halten › „Zu Fotos hinzufügen“. Android: gedrückt halten › „Bild herunterladen“.</p>
<p>Link gültig bis %(until)s Uhr.</p><a class="btn" href="zip">Alle als ZIP laden</a></header>
<div class="g">%(items)s</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._route(head=True)

    def do_GET(self):
        self._route(head=False)

    def _dms(self, method):
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            if not parts or parts[0] != "dms":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                return self.send_error(404)
            import dms

            dms.handle(self, method, parts, method == "HEAD")
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as ex:
            try:
                self.send_error(500, str(ex)[:100])
            except Exception:
                pass

    def do_POST(self):
        self._dms("POST")

    def do_SUBSCRIBE(self):
        self._dms("SUBSCRIBE")

    def do_UNSUBSCRIBE(self):
        self._dms("SUBSCRIBE")

    def _send(self, data, ctype, head=False, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _file(self, path, ctype, head=False):
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass

    def _route(self, head):
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if parts and parts[0] == "dms":
            return self._dms("HEAD" if head else "GET")
        try:
            if len(parts) < 2 or parts[0] not in ("p", "m", "f"):
                return self.send_error(404)
            share = _get(parts[1])
            if not share:
                return self.send_error(410, "Link abgelaufen")
            ids = share["ids"]
            if parts[0] == "f" and share.get("file"):
                ext = os.path.splitext(share["file"])[1].lower().lstrip(".")
                return self._file(share["file"], VIDEO_MIME.get(ext, "video/mp4"), head)
            if parts[0] == "p":
                return self._phone(share, ids, parts[2:], head)
            # /m/<token>/<id>.<ext>  – Medien für den Fernseher
            iid = int(parts[2].split(".")[0])
            if iid not in ids:
                return self.send_error(404)
            row = _item(iid)
            if row[2] == "video":
                return self._file(to_abs(row[1]), VIDEO_MIME.get(row[3], "video/mp4"), head)
            self._send(image_bytes(row, "tv"), "image/jpeg", head, extra=[
                ("transferMode.dlna.org", "Interactive"),
                ("contentFeatures.dlna.org", "DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=00f00000000000000000000000000000")])
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as ex:
            try:
                self.send_error(500, str(ex)[:100])
            except Exception:
                pass

    def _phone(self, share, ids, rest, head):
        if not rest:
            items = []
            for n, iid in enumerate(ids):
                row = _item(iid)
                if not row:
                    continue
                if row[2] == "video":
                    items.append('<a href="v/%d" download="%s"><img src="t/%d" loading="lazy"><span>▶ Video</span></a>'
                                 % (n, row[4], n))
                else:
                    items.append('<a href="i/%d"><img src="t/%d" loading="lazy"></a>' % (n, n))
            html = PAGE % {"n": len(ids), "items": "".join(items),
                           "until": time.strftime("%H:%M", time.localtime(share["until"]))}
            return self._send(html.encode("utf-8"), "text/html; charset=utf-8", head)
        if rest[0] == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                for iid in ids:
                    row = _item(iid)
                    if not row:
                        continue
                    if row[2] == "video":
                        z.write(to_abs(row[1]), row[4])
                    else:
                        z.writestr(os.path.splitext(row[4])[0] + ".jpg", image_bytes(row, "phone"))
            return self._send(buf.getvalue(), "application/zip", head,
                              extra=[("Content-Disposition", 'attachment; filename="FotoArchiv.zip"')])
        n = int(rest[1])
        if n >= len(ids):
            return self.send_error(404)
        row = _item(ids[n])
        if rest[0] == "t":
            from common import THUMBS

            return self._send(THUMBS.get(row[0]) or b"", "image/jpeg", head)
        if rest[0] == "v" and row[2] == "video":
            return self._file(to_abs(row[1]), VIDEO_MIME.get(row[3], "video/mp4"), head)
        if rest[0] == "i":
            return self._send(image_bytes(row, "phone"), "image/jpeg", head,
                              extra=[("Content-Disposition", 'inline; filename="%s.jpg"'
                                      % os.path.splitext(row[4])[0].encode("ascii", "replace").decode())])
        self.send_error(404)


def qr_svg(url):
    import segno

    return segno.make(url, error="m").svg_inline(scale=6, border=2, dark="#000", light="#fff")
