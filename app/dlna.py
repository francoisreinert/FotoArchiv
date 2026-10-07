"""Dia-Modus: Fotos, Videos und Diashows auf dem Fernseher abspielen (DLNA / "Auf Fernseher abspielen").

FotoArchiv stellt die Dateien über lan.py im Heimnetz bereit und sagt dem Fernseher per UPnP, was er
abspielen soll. Der Fernseher holt sich die Dateien selbst. Funktioniert mit Samsung-Fernsehern
(auch The Frame im normalen Bildschirmmodus) und den meisten anderen Smart-TVs.
"""
import html
import json
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import common
import lan
from common import connect

CONFIG = os.path.join(common.DATA_DIR, "frame", "tv.json")
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


def _cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------- Suchen ----

def _describe(location):
    with urllib.request.urlopen(location, timeout=4) as r:
        root = ET.fromstring(r.read())
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    dev = root.find(".//d:device", ns)
    base = (root.findtext("d:URLBase", namespaces=ns) or location).strip()
    control = rc = None
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        st = svc.findtext("d:serviceType", namespaces=ns) or ""
        url = urllib.parse.urljoin(base, (svc.findtext("d:controlURL", namespaces=ns) or "").strip())
        if st.startswith("urn:schemas-upnp-org:service:AVTransport"):
            control = url
        elif st.startswith("urn:schemas-upnp-org:service:RenderingControl"):
            rc = url
    if not control:
        return None
    return {"name": (dev.findtext("d:friendlyName", namespaces=ns) or "Fernseher").strip(),
            "model": (dev.findtext("d:modelName", namespaces=ns) or "").strip(),
            "maker": (dev.findtext("d:manufacturer", namespaces=ns) or "").strip(),
            "control": control, "rc": rc, "location": location, "ip": urllib.parse.urlparse(location).hostname}


def discover(timeout=4):
    msg = "\r\n".join(["M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900", 'MAN: "ssdp:discover"', "MX: 2",
                       "ST: urn:schemas-upnp-org:device:MediaRenderer:1", "", ""]).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.5)
    locations = set()
    try:
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _addr = s.recvfrom(8192)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                locations.add(m.group(1).decode())
    finally:
        s.close()
    out = []
    for loc in sorted(locations):
        try:
            d = _describe(loc)
            if d:
                out.append(d)
        except Exception:
            pass
    return out


def status():
    cfg = _cfg()
    r = cfg.get("renderer") or {}
    return {"name": r.get("name"), "ip": r.get("ip"), "show": SHOW.snapshot()}


def set_renderer(dev):
    cfg = _cfg()
    cfg["renderer"] = dev
    _save(cfg)


# ----------------------------------------------------------------- UPnP ----

def _soap(action, args, service=AVT):
    r = (_cfg().get("renderer") or {})
    if not r.get("control"):
        raise RuntimeError("Noch kein Fernseher gewählt (Bibliothek › Fernseher)")
    url = r["control"]
    if service != AVT:
        if not r.get("rc"):
            r = _describe(r["location"]) or r
            cfg = _cfg()
            cfg["renderer"] = r
            _save(cfg)
        url = r.get("rc") or url
    body = "".join("<%s>%s</%s>" % (k, html.escape(str(v), quote=False), k) for k, v in args)
    env = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s>'
           '</s:Body></s:Envelope>' % (action, service, body, action))
    req = urllib.request.Request(url, data=env.encode("utf-8"), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": '"%s#%s"' % (service, action)})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Fernseher lehnt ab (%s): %s" % (action, e.read()[:200].decode("utf-8", "replace")))


def _didl(url, title, mime, video):
    cls = "object.item.videoItem" if video else "object.item.imageItem.photo"
    pi = ("http-get:*:%s:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000" % mime if video
          else "http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=00f00000000000000000000000000000")
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1" parentID="0" restricted="1">'
            '<dc:title>%s</dc:title><upnp:class>%s</upnp:class><res protocolInfo="%s">%s</res></item></DIDL-Lite>'
            % (html.escape(title), cls, pi, html.escape(url)))


def play_url(url, title, mime, video):
    meta = _didl(url, title, mime, video)
    try:
        _soap("SetAVTransportURI", [("InstanceID", 0), ("CurrentURI", url), ("CurrentURIMetaData", meta)])
    except RuntimeError:
        # manche Geräte wollen erst gestoppt werden
        try:
            _soap("Stop", [("InstanceID", 0)])
        except RuntimeError:
            pass
        _soap("SetAVTransportURI", [("InstanceID", 0), ("CurrentURI", url), ("CurrentURIMetaData", meta)])
    try:
        _soap("Play", [("InstanceID", 0), ("Speed", 1)])
    except RuntimeError:
        # Samsung spielt neue Bilder von selbst ab und lehnt ein zusätzliches "Play" dann ab
        time.sleep(0.8)
        if transport_state() not in ("PLAYING", "TRANSITIONING"):
            raise


def transport_state():
    try:
        x = _soap("GetTransportInfo", [("InstanceID", 0)])
        m = re.search(r"<CurrentTransportState>([^<]*)<", x)
        return m.group(1) if m else None
    except Exception:
        return None


def stop_tv():
    try:
        _soap("Stop", [("InstanceID", 0)])
    except Exception:
        pass


# -------------------------------------------------------------- Diashow ----

class Show:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop = False
        self.skip = 0
        self.paused = False
        self.i = 0
        self.total = 0
        self.title = ""
        self.message = ""
        self.token = None
        self.seamless = False
        self.follow = False
        self.timeline = []

    def snapshot(self):
        return {k: getattr(self, k) for k in ("running", "paused", "i", "total", "title", "message", "seamless", "follow")}


SHOW = Show()


def _items(ids):
    con = connect()
    try:
        rows = {}
        for k in range(0, len(ids), 500):
            ch = ids[k:k + 500]
            for r in con.execute("SELECT id, kind, ext, name, duration FROM items WHERE id IN (%s)"
                                 % ",".join("?" * len(ch)), ch):
                rows[r[0]] = r
        return [rows[i] for i in ids if i in rows]
    finally:
        con.close()


def start_show(ids, seconds=6, shuffle=False, videos=True, title="Diashow", loop=True):
    if SHOW.running:
        SHOW.stop = True
        time.sleep(1.5)
    items = [r for r in _items(ids) if videos or r[1] != "video"]
    if not items:
        raise RuntimeError("Keine Fotos/Videos ausgewählt")
    if shuffle:
        random.shuffle(items)
    token = lan.create([r[0] for r in items], kind="tv", minutes=24 * 60)
    SHOW.__init__()
    SHOW.running, SHOW.total, SHOW.title, SHOW.token = True, len(items), title, token
    threading.Thread(target=_run, args=(items, max(2, float(seconds)), loop), daemon=True).start()


def _run(items, seconds, loop):
    base = lan.base_url()
    try:
        while not SHOW.stop:
            k = 0
            while k < len(items) and not SHOW.stop:
                SHOW.i = k + 1
                iid, kind, ext, name, duration = items[k]
                video = kind == "video"
                # die nächsten zwei Fotos schon vorbereiten
                for nk in (k + 1, k + 2):
                    if nk < len(items) and items[nk][1] != "video":
                        lan.prefetch(items[nk][0])
                url = "%s/m/%s/%d.%s" % (base, SHOW.token, iid, ext if video else "jpg")
                t_sent = time.time()
                try:
                    play_url(url, name, lan.VIDEO_MIME.get(ext, "video/mp4"), video)
                    SHOW.message = ""
                except Exception as ex:
                    SHOW.message = str(ex)[:200]
                # warten: Fotos feste Zeit, Videos bis zum Ende
                wait = (duration or 30) + 3 if video else seconds
                t0 = t_sent  # Umschaltzeit des Fernsehers zählt zur Anzeigedauer
                played = False
                while not SHOW.stop and not SHOW.skip:
                    time.sleep(0.5)
                    if SHOW.paused:
                        t0 += 0.5
                        continue
                    if video and time.time() - t0 > 3:
                        st = transport_state()
                        played = played or st == "PLAYING"
                        if played and st in ("STOPPED", "NO_MEDIA_PRESENT"):
                            break
                    if time.time() - t0 >= wait:
                        break
                k = max(0, k + (SHOW.skip or 1))
                SHOW.skip = 0
            if not loop:
                break
    finally:
        lan.revoke(SHOW.token)
        if SHOW.stop:
            stop_tv()
        SHOW.running = False


def control(cmd):
    if cmd == "stop":
        SHOW.stop = True
        if SHOW.follow:
            stop_follow(stop=True)
        return
    if SHOW.seamless and (SHOW.message or "").startswith("wird vorbereitet"):
        return  # läuft noch nicht – Vor/Zurück/Pause erst, wenn die Diashow spielt
    if cmd in ("next", "prev") and SHOW.seamless:
        try:
            _seek_item(1 if cmd == "next" else -1)
        except Exception as ex:
            SHOW.message = "Springen nicht möglich: %s" % str(ex)[:80]
    elif cmd == "next":
        SHOW.skip = 1
    elif cmd == "prev":
        SHOW.skip = -1
    elif cmd == "pause":
        SHOW.paused = not SHOW.paused
        if SHOW.seamless:
            _mute(SHOW.paused)  # festgehaltenes Foto: Ton aus, sonst liefe die Musik in Schleife


CACHE = os.path.join(common.DATA_DIR, "_tvcache")


class _RenderJob:
    """Fortschritt der Video-Erzeugung, gekoppelt an Stopp der Diashow."""

    def __init__(self):
        self.done = self.total = self.errors = 0
        self.log = []

    @property
    def stop(self):
        return SHOW.stop

    def note(self, msg):
        self.log.append(msg)


def start_seamless(ids, seconds=6, kenburns=False, music=(), shuffle=False, title="Diashow", videos=True):
    """Nahtlose Diashow: als ein Video erzeugen und schon währenddessen abspielen –
    so blendet der Fernseher seine Player-Leiste nur einmal ein. Mit Überblendung und Musik."""
    if SHOW.running:
        SHOW.stop = True
        time.sleep(1.5)
    con = connect()
    try:
        import share

        items = share._items(con, list(ids))
    finally:
        con.close()
    if not videos:
        items = [r for r in items if r[2] != "video"]
    if not items:
        raise RuntimeError("Keine Fotos/Videos ausgewählt")
    if shuffle:
        random.shuffle(items)
    SHOW.__init__()
    SHOW.running, SHOW.total, SHOW.title = True, len(items), title
    SHOW.seamless = True
    threading.Thread(target=_run_seamless, args=(items, seconds, kenburns, list(music)), daemon=True).start()


def _run_seamless(items, seconds, kenburns, music):
    import hashlib

    import share

    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.sha1(json.dumps([[r[0] for r in items], seconds, kenburns, sorted(music), share.PROC_SHOW],
                                  sort_keys=True).encode()).hexdigest()[:16]
    path = os.path.join(CACHE, "show_%s.mp4" % key)
    token = None
    try:
        if not os.path.exists(path):
            # alte Diashows aufräumen (die letzten 5 bleiben für schnellen Neustart)
            olds = sorted((os.path.join(CACHE, f) for f in os.listdir(CACHE)), key=os.path.getmtime)
            for f in olds[:-5]:
                try:
                    os.remove(f)
                except OSError:
                    pass
            job = _RenderJob()
            tmp = path + ".tmp.mp4"
            SHOW.message = "wird vorbereitet …"

            def progress():
                while not done:
                    SHOW.i = job.done
                    SHOW.message = "wird vorbereitet: %d von %d" % (job.done, job.total or len(items))
                    time.sleep(0.5)
            done = False
            threading.Thread(target=progress, daemon=True).start()
            try:
                share.render_show(items, tmp, seconds, kenburns, music, True, job)
            finally:
                done = True
            if SHOW.stop:
                os.remove(tmp)
                return
            os.replace(tmp, path)
            with open(path + ".json", "w", encoding="utf-8") as f:
                json.dump(job.timeline, f)
        os.utime(path)
        try:
            with open(path + ".json", encoding="utf-8") as f:
                SHOW.timeline = json.load(f)
        except (OSError, ValueError):
            SHOW.timeline = []
        token = lan.create_file(path, minutes=12 * 60)
        play_url("%s/f/%s/diashow.mp4" % (lan.base_url(), token), SHOW.title, "video/mp4", True)
        SHOW.message = ""
        SHOW.paused = False
        SHOW.i = 1
        played = False
        t0 = time.time()
        while not SHOW.stop:
            time.sleep(0.5 if SHOW.paused else 1)
            if SHOW.paused:
                try:
                    _hold()
                except Exception:
                    pass
                continue
            st = transport_state()
            if st in ("PLAYING", "PAUSED_PLAYBACK"):
                played = True
                if st == "PLAYING" and not SHOW.paused:
                    SHOW.i = _position_index(len(items), seconds)
            if played and st in ("STOPPED", "NO_MEDIA_PRESENT"):
                break
            if not played and time.time() - t0 > 40:
                SHOW.message = "Der Fernseher startet die Wiedergabe nicht."
                break
    except Exception as ex:
        SHOW.message = str(ex)[:200]
    finally:
        if token:
            lan.revoke(token)
        if SHOW.paused:
            _mute(False)
        if SHOW.stop:
            stop_tv()
        SHOW.running = False


def _position():
    """Aktuelle Abspielposition in Sekunden (oder None)."""
    try:
        x = _soap("GetPositionInfo", [("InstanceID", 0)])
        m = re.search(r"<RelTime>(\d+):(\d+):(\d+(?:\.\d+)?)", x)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


def _position_index(n, seconds):
    """Welches Foto gerade läuft (für die Anzeige unten rechts)."""
    t = _position()
    if t is None:
        return SHOW.i
    if SHOW.timeline:
        return max(1, sum(1 for s in SHOW.timeline if s <= t + 0.3))
    return min(n, int(t // seconds) + 1)


def _seek_item(delta):
    """Nahtlose Diashow: zum vorigen/nächsten Foto springen."""
    tl = SHOW.timeline
    if not tl:
        return
    # in der Pause meldet der Samsung keine verlässliche Position -> eigene Foto-Nummer verwenden
    t = None if SHOW.paused else _position()
    if t is None:
        cur = max(0, min(len(tl) - 1, SHOW.i - 1))
        t = tl[cur]
    else:
        cur = max(0, sum(1 for s in tl if s <= t + 0.3) - 1)
    if delta < 0 and t - tl[cur] > 2:
        target = cur  # erst an den Anfang des aktuellen Fotos
    else:
        target = min(len(tl) - 1, max(0, cur + delta))
    secs = tl[target] + 0.05
    stamp = "%d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, int(secs % 60 + 0.999))
    _soap("Seek", [("InstanceID", 0), ("Unit", "REL_TIME"), ("Target", stamp)])
    SHOW.i = target + 1


RC = "urn:schemas-upnp-org:service:RenderingControl:1"


def _mute(on):
    try:
        _soap("SetMute", [("InstanceID", 0), ("Channel", "Master"), ("DesiredMute", 1 if on else 0)], service=RC)
    except Exception:
        pass


def _hold():
    """Pause in der nahtlosen Diashow: der Samsung reagiert nach einem echten Pause-Befehl nicht mehr
    zuverlässig. Deshalb läuft das Video weiter, und kurz bevor das nächste Foto käme, springt
    FotoArchiv an den Anfang des aktuellen Fotos zurück (Ton ist dabei stumm)."""
    tl = SHOW.timeline
    if not tl:
        return
    cur = max(0, min(len(tl) - 1, SHOW.i - 1))
    t = _position()
    if t is None:
        return
    seg_end = tl[cur + 1] if cur + 1 < len(tl) else None
    limit = (seg_end - 1.0 - 1.5) if seg_end is not None else tl[cur] + 30   # vor der Überblendung
    if t < tl[cur] - 0.5 or t > max(tl[cur] + 0.8, limit):
        secs = tl[cur] + 0.05
        _soap("Seek", [("InstanceID", 0), ("Unit", "REL_TIME"),
                       ("Target", "%d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, int(secs % 60 + 0.999)))])


def show_one(iid, prefetch=()):
    """Mitlauf-Modus: das im Betrachter gezeigte Foto/Video sofort auf den Fernseher schicken."""
    if not (SHOW.running and SHOW.follow):
        if SHOW.running:
            SHOW.stop = True
            time.sleep(1.5)
        SHOW.__init__()
        SHOW.running, SHOW.follow, SHOW.total = True, True, 1
        SHOW.token = lan.create([], kind="tv", minutes=6 * 60)
    rows = _items([iid])
    if not rows:
        raise RuntimeError("Foto nicht gefunden")
    iid, kind, ext, name, duration = rows[0]
    lan.add_ids(SHOW.token, [iid] + list(prefetch))
    for p in prefetch:
        lan.prefetch(p)
    video = kind == "video"
    url = "%s/m/%s/%d.%s" % (lan.base_url(), SHOW.token, iid, ext if video else "jpg")
    SHOW.title, SHOW.i, SHOW.message = name, 1, ""
    try:
        play_url(url, name, lan.VIDEO_MIME.get(ext, "video/mp4"), video)
    except Exception as ex:
        SHOW.message = str(ex)[:200]
        raise


def stop_follow(stop=False):
    if SHOW.follow:
        if SHOW.token:
            lan.revoke(SHOW.token)
        if stop:
            stop_tv()
        SHOW.running = False
        SHOW.follow = False


def play_file(path, title):
    """Eine Datei (z. B. die gerenderte Diashow mit Musik) auf dem Fernseher abspielen."""
    token = lan.create_file(path, minutes=6 * 60)
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "mp4"
    play_url("%s/f/%s/video.%s" % (lan.base_url(), token, ext), title, lan.VIDEO_MIME.get(ext, "video/mp4"), True)
