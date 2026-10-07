"""Samsung The Frame: Fotos per Heimnetz in den Kunstmodus schicken.

Einmalig muss am Fernseher "Zulassen" gedrückt werden; die Kopplung (Token) liegt danach in
data/frame/, also auf der Platte – sie gilt auch, wenn FotoArchiv an einem anderen Computer läuft.
"""
import io
import json
import os
import socket
import threading
import time
import urllib.request

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

import common
from common import connect, to_abs
from importer import Job

FRAME_DIR = os.path.join(common.DATA_DIR, "frame")
CONFIG = os.path.join(FRAME_DIR, "frame.json")
TOKEN = os.path.join(FRAME_DIR, "token.txt")
W, H = 3840, 2160
KEEP = 60  # so viele von FotoArchiv geschickte Bilder bleiben auf dem Frame, ältere werden gelöscht

JOB = Job()


def _cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(cfg):
    os.makedirs(FRAME_DIR, exist_ok=True)
    with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(CONFIG + ".tmp", CONFIG)


def device_info(ip):
    """Gerätedaten vom Fernseher (ohne Kopplung abrufbar)."""
    with urllib.request.urlopen("http://%s:8001/api/v2/" % ip, timeout=3) as r:
        d = json.loads(r.read()).get("device", {})
    return {"ip": ip, "name": d.get("name") or d.get("modelName") or ip, "model": d.get("modelName"),
            "frame": str(d.get("FrameTVSupport")).lower() == "true", "power": d.get("PowerState")}


def discover(timeout=4):
    """Samsung-Fernseher im Heimnetz suchen (SSDP)."""
    msg = "\r\n".join(["M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900", 'MAN: "ssdp:discover"', "MX: 2",
                       "ST: urn:samsung.com:device:RemoteControlReceiver:1", "", ""]).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.5)
    ips = set()
    try:
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                _data, addr = s.recvfrom(4096)
                ips.add(addr[0])
            except socket.timeout:
                pass
    finally:
        s.close()
    if not ips:
        ips = _probe_subnet()
    out = []
    for ip in sorted(ips):
        try:
            out.append(device_info(ip))
        except Exception:
            out.append({"ip": ip, "name": ip, "frame": None})
    return out


def _probe_subnet():
    """Rückfall, wenn die Firewall die Antworten auf die Suche blockiert: im eigenen Netz (/24)
    nachsehen, wer auf dem Samsung-Port 8001 antwortet. Nur ausgehende Verbindungsversuche."""
    from concurrent.futures import ThreadPoolExecutor

    import lan

    prefix = lan.lan_ip().rsplit(".", 1)[0]

    def probe(i):
        ip = "%s.%d" % (prefix, i)
        s = socket.socket()
        s.settimeout(0.6)
        try:
            return ip if s.connect_ex((ip, 8001)) == 0 else None
        finally:
            s.close()
    with ThreadPoolExecutor(64) as ex:
        return {ip for ip in ex.map(probe, range(1, 255)) if ip}


def status():
    cfg = _cfg()
    return {"ip": cfg.get("ip"), "name": cfg.get("name"), "paired": os.path.exists(TOKEN) or bool(cfg.get("connected")),
            "uploaded": len(cfg.get("uploaded", []))}


def set_device(ip, name=None):
    cfg = _cfg()
    if cfg.get("ip") != ip and os.path.exists(TOKEN):
        os.remove(TOKEN)  # anderer Fernseher -> neu koppeln
    cfg.update(ip=ip, name=name or ip)
    _save(cfg)


def _art():
    from samsungtvws import SamsungTVWS

    cfg = _cfg()
    if not cfg.get("ip"):
        raise RuntimeError("Noch kein Frame eingerichtet (Bibliothek › Samsung The Frame)")
    os.makedirs(FRAME_DIR, exist_ok=True)
    tv = SamsungTVWS(cfg["ip"], port=8002, token_file=TOKEN, timeout=60, name="FotoArchiv")
    return tv.art()


def pair():
    """Verbindung testen; beim ersten Mal erscheint am Fernseher die Frage "Zulassen?"."""
    art = _art()
    ok = art.supported()
    if ok:
        cfg = _cfg()
        cfg["connected"] = True
        _save(cfg)
    return {"ok": bool(ok), "api": art.get_api_version() if ok else None}


def frame_image(path, kind, userrot=0, size=(W, H)):
    """Foto als 4K-JPEG für den Kunstmodus. Hochkant/Quadrat bekommen einen unscharfen Hintergrund."""
    import media

    W, H = size  # noqa: N806 – lokale Zielgröße

    if kind == "video":
        im = media.rotate_cw(media.video_frame(path)[0], userrot or 0)
    else:
        im = media.open_image(path, kind, max_side=max(W, 1920), userrot=userrot or 0)[0]
    ar = im.width / im.height
    if abs(ar - W / H) / (W / H) < 0.12:
        out = ImageOps.fit(im, (W, H), Image.LANCZOS)
    else:
        bg = ImageOps.fit(im, (W // 10, H // 10), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3))
        out = ImageEnhance.Brightness(bg.resize((W, H), Image.BILINEAR)).enhance(0.5)
        s = min(W / im.width, H / im.height)
        fg = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
        out.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def send(ids, slideshow_minutes=0):
    """Fotos auf den Frame schicken; das erste wird sofort gezeigt. Optional als Frame-Diashow."""
    JOB.reset("Frame: %d Foto%s" % (len(ids), "" if len(ids) == 1 else "s"))
    JOB.running = True
    con = connect()
    try:
        rows = {}
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            for r in con.execute("SELECT id, path, kind, userrot, name FROM items WHERE id IN (%s)"
                                 % ",".join("?" * len(ch)), ch):
                rows[r[0]] = r
        items = [rows[i] for i in ids if i in rows]
        JOB.total = len(items)
        JOB.phase = "Verbinden (beim ersten Mal am Fernseher „Zulassen“ drücken)"
        art = _art()
        if not art.supported():
            raise RuntimeError("Der Fernseher meldet keinen Kunstmodus – ist es ein The Frame und eingeschaltet?")
        cfg = _cfg()
        uploaded = cfg.get("uploaded", [])
        first = None
        JOB.phase = "Hochladen"
        for iid, rel, kind, userrot, name in items:
            if JOB.stop:
                break
            try:
                data = frame_image(to_abs(rel), kind, userrot)
                cid = art.upload(data, file_type="jpg", matte="none", portrait_matte="none")
                uploaded.append(cid)
                JOB.new += 1
                JOB.bytes += len(data)
                if first is None:
                    first = cid
                    art.select_image(cid, show=True)
                JOB.note("Gesendet: %s" % name)
            except Exception as ex:
                JOB.errors += 1
                JOB.note("Fehler bei %s: %s" % (name, ex))
            JOB.done += 1
        # Speicher am Frame nicht volllaufen lassen: nur die letzten KEEP eigenen Bilder behalten
        if len(uploaded) > KEEP:
            old, uploaded = uploaded[:-KEEP], uploaded[-KEEP:]
            try:
                art.delete_list(old)
            except Exception as ex:
                JOB.note("Alte Bilder konnten nicht gelöscht werden: %s" % ex)
        cfg["uploaded"] = uploaded
        cfg["connected"] = cfg.get("connected") or JOB.new > 0
        _save(cfg)
        if slideshow_minutes and JOB.new > 1:
            try:
                art.set_slideshow_status(duration=int(slideshow_minutes), type=False, category=2)
                JOB.note("Frame-Diashow: alle %d Min. ein neues Bild aus „Meine Fotos“" % int(slideshow_minutes))
            except Exception as ex:
                JOB.note("Diashow konnte nicht eingestellt werden: %s" % ex)
        JOB.message = "%d Foto%s auf dem Frame" % (JOB.new, "" if JOB.new == 1 else "s") if JOB.new else "Nichts gesendet"
    except Exception as ex:
        msg = str(ex)
        if "timed out" in msg.lower() or "refused" in msg.lower() or "unreachable" in msg.lower():
            msg = "Frame nicht erreichbar – ist er eingeschaltet (Kunstmodus) und im selben Netz?"
        elif "unauthorized" in msg.lower() or "ms.channel.unauthorized" in msg:
            msg = "Am Fernseher wurde die Verbindung nicht zugelassen – bitte nochmal und „Zulassen“ drücken."
        JOB.message = "Fehler: " + msg
        JOB.errors += 1
    finally:
        con.close()
        JOB.running = False
        JOB.finished = time.time()
        JOB.phase = ""


def cleanup():
    """Alle von FotoArchiv geschickten Bilder vom Frame löschen."""
    cfg = _cfg()
    ids = cfg.get("uploaded", [])
    if ids:
        _art().delete_list(ids)
    cfg["uploaded"] = []
    _save(cfg)
    return len(ids)


def start(fn, *args):
    if JOB.running:
        return False
    JOB.running = True
    threading.Thread(target=fn, args=args, daemon=True).start()
    return True
