"""FotoArchiv als Medienserver im Heimnetz (DLNA/UPnP MediaServer).

Am Fernseher erscheint "FotoArchiv" als Quelle. Dort lässt sich mit der Fernbedienung durch
Ereignisse, Alben, Jahre/Monate, Personen und Favoriten blättern; Fotos und Videos laufen im
eingebauten Betrachter des Fernsehers (← → blättern, ▶ Diashow).

Nur eingeschaltet, wenn gewünscht (Bibliothek › Fernseher). Private, ausgeblendete und doppelte
Fotos werden nie angeboten – der Fernseher kann kein Passwort abfragen.
"""
import html
import json
import os
import re
import socket
import struct
import threading
import time
import uuid

import common
from common import THUMBS, connect, to_abs

CONFIG = os.path.join(common.DATA_DIR, "frame", "dms.json")
SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900
DEVICE = "urn:schemas-upnp-org:device:MediaServer:1"
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
VISIBLE = ("COALESCE(i.priv_eff,0)=0 AND COALESCE(i.hidden,0)=0 AND i.dup_of IS NULL AND i.raw_of IS NULL "
           "AND i.proc_ver > 0")
PHOTO_PI = "http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=00f00000000000000000000000000000"
THUMB_PI = "http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_TN;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=00f00000000000000000000000000000"

_state = {"running": False, "stop": False}
_update_id = [int(time.time()) % 100000]


def _cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _uuid():
    cfg = _cfg()
    if not cfg.get("uuid"):
        cfg["uuid"] = str(uuid.uuid4())
        _save(cfg)
    return cfg["uuid"]


def enabled():
    return bool(_cfg().get("enabled"))


def status():
    import lan

    own = lan.lan_ip()
    seen = {ip: round(time.time() - t) for ip, t in _state.get("seen", {}).items() if ip not in (own, "127.0.0.1")}
    return {"enabled": enabled(), "running": _state["running"], "seen": seen}


def set_enabled(on):
    cfg = _cfg()
    cfg["enabled"] = bool(on)
    _save(cfg)
    if on:
        start()
    else:
        stop()


# ----------------------------------------------------------------- SSDP ----

def _location():
    import lan

    return "%s/dms/desc.xml" % lan.base_url()


def _targets():
    u = "uuid:" + _uuid()
    return [("upnp:rootdevice", u + "::upnp:rootdevice"), (u, u), (DEVICE, u + "::" + DEVICE),
            (CDS, u + "::" + CDS), (CMS, u + "::" + CMS)]


def _notify(sock, alive=True):
    for nt, usn in _targets():
        lines = ["NOTIFY * HTTP/1.1", "HOST: %s:%d" % (SSDP_ADDR, SSDP_PORT), "NT: " + nt, "USN: " + usn,
                 "NTS: ssdp:" + ("alive" if alive else "byebye")]
        if alive:
            lines += ["CACHE-CONTROL: max-age=1800", "LOCATION: " + _location(),
                      "SERVER: Windows/10 UPnP/1.0 FotoArchiv/1.0"]
        try:
            sock.sendto(("\r\n".join(lines) + "\r\n\r\n").encode(), (SSDP_ADDR, SSDP_PORT))
        except OSError:
            pass


def _ssdp_loop():
    import lan

    lan.ensure_server()
    ip = lan.lan_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", SSDP_PORT))
        listening = True
    except OSError:
        sock.bind((ip, 0))  # Port 1900 belegt: nur ankündigen (genügt den meisten Fernsehern)
        listening = False
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(ip)))
    except OSError:
        pass
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(1.0)
    _state["running"] = True
    last = 0
    try:
        while not _state["stop"]:
            if time.time() - last > 60:
                _notify(sock, True)
                last = time.time()
            if not listening:
                time.sleep(1)
                continue
            try:
                data, addr = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if not data.startswith(b"M-SEARCH"):
                continue
            m = re.search(rb"(?im)^st:\s*(\S+)", data)
            st = m.group(1).decode() if m else ""
            for nt, usn in _targets():
                if st in ("ssdp:all", nt):
                    resp = "\r\n".join(["HTTP/1.1 200 OK", "CACHE-CONTROL: max-age=1800", "EXT:",
                                        "LOCATION: " + _location(), "SERVER: Windows/10 UPnP/1.0 FotoArchiv/1.0",
                                        "ST: " + nt, "USN: " + usn, "", ""])
                    try:
                        sock.sendto(resp.encode(), addr)
                    except OSError:
                        pass
    finally:
        _notify(sock, False)
        sock.close()
        _state["running"] = False


def start():
    if _state["running"]:
        return
    _state["stop"] = False
    threading.Thread(target=_ssdp_loop, daemon=True).start()


def stop():
    _state["stop"] = True


# ---------------------------------------------------------- Beschreibung ----

def desc_xml():
    return ('<?xml version="1.0" encoding="utf-8"?><root xmlns="urn:schemas-upnp-org:device-1-0" '
            'xmlns:dlna="urn:schemas-dlna-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion>'
            '<device><deviceType>%s</deviceType><friendlyName>FotoArchiv</friendlyName>'
            '<manufacturer>FotoArchiv</manufacturer><modelName>FotoArchiv</modelName><modelNumber>1</modelNumber>'
            '<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC><UDN>uuid:%s</UDN><serviceList>'
            '<service><serviceType>%s</serviceType><serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>'
            '<SCPDURL>/dms/cds.xml</SCPDURL><controlURL>/dms/control/cds</controlURL><eventSubURL>/dms/event/cds</eventSubURL></service>'
            '<service><serviceType>%s</serviceType><serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>'
            '<SCPDURL>/dms/cms.xml</SCPDURL><controlURL>/dms/control/cms</controlURL><eventSubURL>/dms/event/cms</eventSubURL></service>'
            '</serviceList></device></root>' % (DEVICE, _uuid(), CDS, CMS))


def _scpd(actions, variables):
    acts = "".join("<action><name>%s</name><argumentList>%s</argumentList></action>" % (
        name, "".join("<argument><name>%s</name><direction>%s</direction><relatedStateVariable>%s</relatedStateVariable></argument>"
                      % a for a in args)) for name, args in actions)
    vs = "".join('<stateVariable sendEvents="%s"><name>%s</name><dataType>%s</dataType></stateVariable>' % v
                 for v in variables)
    return ('<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion>'
            '<major>1</major><minor>0</minor></specVersion><actionList>%s</actionList><serviceStateTable>%s'
            '</serviceStateTable></scpd>' % (acts, vs))


def cds_xml():
    return _scpd([
        ("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                    ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                    ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                    ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                    ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
        ("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities")]),
        ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities")]),
        ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID")]),
    ], [("no", "A_ARG_TYPE_ObjectID", "string"), ("no", "A_ARG_TYPE_BrowseFlag", "string"),
        ("no", "A_ARG_TYPE_Filter", "string"), ("no", "A_ARG_TYPE_Index", "ui4"), ("no", "A_ARG_TYPE_Count", "ui4"),
        ("no", "A_ARG_TYPE_SortCriteria", "string"), ("no", "A_ARG_TYPE_Result", "string"),
        ("no", "A_ARG_TYPE_UpdateID", "ui4"), ("no", "SearchCapabilities", "string"),
        ("no", "SortCapabilities", "string"), ("yes", "SystemUpdateID", "ui4")])


def cms_xml():
    return _scpd([
        ("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")]),
        ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
    ], [("yes", "SourceProtocolInfo", "string"), ("yes", "SinkProtocolInfo", "string"),
        ("yes", "CurrentConnectionIDs", "string")])


# --------------------------------------------------------------- Inhalte ----

def _items_sql(where, args, order="i.taken"):
    return ("SELECT i.id, i.name, i.kind, i.ext, i.taken, i.width, i.height, i.duration, i.size FROM items i WHERE "
            + VISIBLE + " AND " + where + " ORDER BY " + order, args)


def _children(con, oid):
    """Liefert (container-Liste, item-SQL) für ein Objekt. container: (id, titel, anzahl_oder_None)."""
    if oid == "0":
        return [("ev", "Ereignisse", None), ("al", "Alben", None), ("yr", "Jahre", None),
                ("pe", "Personen", None), ("fav", "Favoriten", None), ("new", "Neueste 500", None)], None
    if oid == "ev":
        return [("ev:%d" % r[0], r[1], None) for r in con.execute(
            "SELECT id, name FROM events WHERE COALESCE(private,0)=0 ORDER BY start DESC")], None
    if oid.startswith("ev:"):
        e = con.execute("SELECT start, end FROM events WHERE id=? AND COALESCE(private,0)=0", (int(oid[3:]),)).fetchone()
        return [], _items_sql("i.taken >= ? AND i.taken <= ?", (e[0][:10], e[1][:10] + " 23:59:59")) if e else None
    if oid == "al" or oid.startswith("al:"):
        import privacy

        hidden = privacy.private_albums(con)
        parent = None if oid == "al" else int(oid[3:])
        subs = [("al:%d" % r[0], r[1], None) for r in con.execute(
            "SELECT id, name FROM albums WHERE parent IS ? ORDER BY name COLLATE NOCASE", (parent,)) if r[0] not in hidden]
        if parent is None or parent in hidden:
            return subs, None
        return subs, _items_sql("i.id IN (SELECT item_id FROM album_items WHERE album_id=?)", (parent,))
    if oid == "yr":
        return [("yr:%s" % r[0], r[0], r[1]) for r in con.execute(
            "SELECT substr(i.taken,1,4) y, COUNT(*) FROM items i WHERE " + VISIBLE + " AND i.taken IS NOT NULL "
            "GROUP BY y ORDER BY y DESC")], None
    if oid.startswith("yr:"):
        months = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                  "November", "Dezember"]
        return [("mo:%s" % r[0], "%s %s" % (months[int(r[0][5:7]) - 1], r[0][:4]), r[1]) for r in con.execute(
            "SELECT substr(i.taken,1,7) m, COUNT(*) FROM items i WHERE " + VISIBLE + " AND substr(i.taken,1,4)=? "
            "GROUP BY m ORDER BY m", (oid[3:],))], None
    if oid.startswith("mo:"):
        return [], _items_sql("substr(i.taken,1,7)=?", (oid[3:],))
    if oid == "pe":
        return [("pe:%d" % r[0], r[1], None) for r in con.execute(
            "SELECT id, name FROM persons WHERE COALESCE(hidden,0)=0 ORDER BY name COLLATE NOCASE")], None
    if oid.startswith("pe:"):
        return [], _items_sql("i.id IN (SELECT item_id FROM faces WHERE person_id=?)", (int(oid[3:]),))
    if oid == "fav":
        return [], _items_sql("(i.fav=1 OR i.rating>=4)", ())
    if oid == "new":
        return [], _items_sql("i.taken IS NOT NULL", (), "i.taken DESC LIMIT 500")
    return [], None


def _parent(oid):
    if oid in ("ev", "al", "yr", "pe", "fav", "new"):
        return "0"
    if oid.startswith("mo:"):
        return "yr:" + oid[3:7]
    if ":" in oid:
        return oid.split(":")[0]
    return "-1"


def _dur(s):
    s = int(s or 0)
    return "%d:%02d:%02d.000" % (s // 3600, s % 3600 // 60, s % 60)


def _item_didl(r, parent, base):
    iid, name, kind, ext, taken, w, h, duration, size = r
    import lan

    date = (taken or "").replace(" ", "T")
    thumb = "%s/dms/t/%d.jpg" % (base, iid)
    if kind == "video":
        mime = lan.VIDEO_MIME.get(ext, "video/mp4")
        res = ('<res protocolInfo="http-get:*:%s:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000" '
               'size="%d" duration="%s">%s/dms/v/%d.%s</res>' % (mime, size or 0, _dur(duration), base, iid, ext))
        cls = "object.item.videoItem"
    else:
        res = '<res protocolInfo="%s" resolution="%dx%d">%s/dms/i/%d.jpg</res>' % (PHOTO_PI, w or 0, h or 0, base, iid)
        cls = "object.item.imageItem.photo"
    title = os.path.splitext(name)[0]
    if taken:
        title = taken[8:10] + "." + taken[5:7] + "." + taken[:4] + " " + taken[11:16]
    return ('<item id="i:%d" parentID="%s" restricted="1"><dc:title>%s</dc:title><upnp:class>%s</upnp:class>'
            '<dc:date>%s</dc:date>%s<res protocolInfo="%s">%s</res>'
            '<upnp:albumArtURI dlna:profileID="JPEG_TN">%s</upnp:albumArtURI></item>'
            % (iid, html.escape(parent), html.escape(title), cls, date, res, THUMB_PI, thumb, thumb))


def _container_didl(cid, title, count, parent):
    return ('<container id="%s" parentID="%s" restricted="1"%s><dc:title>%s</dc:title>'
            '<upnp:class>object.container.storageFolder</upnp:class></container>'
            % (html.escape(cid), html.escape(parent), ' childCount="%d"' % count if count is not None else "",
               html.escape(title)))


def browse(oid, flag, start, count):
    import lan

    base = lan.base_url()
    con = connect()
    try:
        if flag == "BrowseMetadata":
            if oid.startswith("i:"):
                rows = con.execute(*_items_sql("i.id=?", (int(oid[2:]),))).fetchall()
                parts = [_item_didl(r, "0", base) for r in rows]
            else:
                parts = [_container_didl(oid, "FotoArchiv" if oid == "0" else oid, None, _parent(oid))]
            total = len(parts)
        else:
            conts, items_sql = _children(con, oid)
            total = len(conts)
            parts = []
            for c in conts[start:start + count] if count else conts[start:]:
                parts.append(_container_didl(c[0], c[1], c[2], oid))
            if items_sql:
                sql, args = items_sql
                n_items = con.execute("SELECT COUNT(*) FROM (%s)" % sql, args).fetchone()[0]
                skip = max(0, start - len(conts))
                need = (count - len(parts)) if count else n_items
                if need > 0:
                    rows = con.execute("SELECT * FROM (%s) LIMIT ? OFFSET ?" % sql, list(args) + [need, skip]).fetchall()
                    parts += [_item_didl(r, oid, base) for r in rows]
                total += n_items
    finally:
        con.close()
    didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">%s</DIDL-Lite>'
            % "".join(parts))
    return didl, len(parts), total


def _soap_resp(service, action, values):
    body = "".join("<%s>%s</%s>" % (k, html.escape(str(v), quote=False), k) for k, v in values)
    return ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%sResponse xmlns:u="%s">%s'
            '</u:%sResponse></s:Body></s:Envelope>' % (action, service, body, action)).encode("utf-8")


def _arg(xml, name):
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (name, name), xml, re.S)
    return html.unescape(m.group(1)) if m else ""


def control(service_name, soap_action, body):
    action = (soap_action or "").strip('"').split("#")[-1]
    xml = body.decode("utf-8", "replace")
    if service_name == "cds":
        if action == "Browse":
            oid = _arg(xml, "ObjectID") or "0"
            flag = _arg(xml, "BrowseFlag") or "BrowseDirectChildren"
            start = int(_arg(xml, "StartingIndex") or 0)
            count = int(_arg(xml, "RequestedCount") or 0)
            didl, n, total = browse(oid, flag, start, count)
            return _soap_resp(CDS, "Browse", [("Result", didl), ("NumberReturned", n), ("TotalMatches", total),
                                              ("UpdateID", _update_id[0])])
        if action == "GetSystemUpdateID":
            return _soap_resp(CDS, action, [("Id", _update_id[0])])
        if action == "GetSearchCapabilities":
            return _soap_resp(CDS, action, [("SearchCaps", "")])
        if action == "GetSortCapabilities":
            return _soap_resp(CDS, action, [("SortCaps", "")])
    if service_name == "cms":
        if action == "GetProtocolInfo":
            return _soap_resp(CMS, action, [("Source", "http-get:*:image/jpeg:*,http-get:*:video/mp4:*,"
                                                       "http-get:*:video/quicktime:*,http-get:*:video/vnd.dlna.mpeg-tts:*"),
                                            ("Sink", "")])
        if action == "GetCurrentConnectionIDs":
            return _soap_resp(CMS, action, [("ConnectionIDs", "0")])
    return None


def media_row(iid):
    """Nur sichtbare Fotos ausliefern (nicht privat/ausgeblendet)."""
    con = connect()
    try:
        return con.execute("SELECT i.id, i.path, i.kind, i.ext, i.name, i.userrot, i.taken FROM items i WHERE i.id=? AND "
                           "COALESCE(i.priv_eff,0)=0 AND COALESCE(i.hidden,0)=0", (iid,)).fetchone()
    finally:
        con.close()


def thumb(iid):
    return THUMBS.get(iid)


def handle(h, method, parts, head):
    """HTTP-Anfragen unter /dms/ (aus lan.py). True = beantwortet."""
    import lan

    if not enabled():
        h.send_error(404)
        return True
    p = parts[1:] if len(parts) > 1 else []
    _state.setdefault("seen", {})[h.client_address[0]] = time.time()
    if method == "SUBSCRIBE":
        h.send_response(200)
        h.send_header("SID", "uuid:" + str(uuid.uuid4()))
        h.send_header("TIMEOUT", "Second-1800")
        h.send_header("Content-Length", "0")
        h.end_headers()
        return True
    if method == "POST" and len(p) == 2 and p[0] == "control":
        n = int(h.headers.get("Content-Length") or 0)
        out = control(p[1], h.headers.get("SOAPACTION"), h.rfile.read(n) if n else b"")
        if out is None:
            h.send_error(401)
        else:
            h._send(out, 'text/xml; charset="utf-8"', head)
        return True
    if p == ["desc.xml"]:
        h._send(desc_xml().encode("utf-8"), 'text/xml; charset="utf-8"', head)
        return True
    if p == ["cds.xml"]:
        h._send(cds_xml().encode("utf-8"), 'text/xml; charset="utf-8"', head)
        return True
    if p == ["cms.xml"]:
        h._send(cms_xml().encode("utf-8"), 'text/xml; charset="utf-8"', head)
        return True
    if len(p) == 2 and p[0] in ("i", "t", "v"):
        iid = int(p[1].split(".")[0])
        row = media_row(iid)
        if not row:
            h.send_error(404)
            return True
        if p[0] == "t":
            h._send(thumb(iid) or b"", "image/jpeg", head)
        elif p[0] == "v" and row[2] == "video":
            h._file(to_abs(row[1]), lan.VIDEO_MIME.get(row[3], "video/mp4"), head)
        else:
            h._send(lan.image_bytes(row, "phone"), "image/jpeg", head, extra=[
                ("transferMode.dlna.org", "Interactive"), ("contentFeatures.dlna.org", PHOTO_PI.split(":", 3)[3])])
        return True
    h.send_error(404)
    return True
