"""FotoArchiv – lokaler Server. Start über die Start-Dateien im FotoArchiv-Ordner.

Läuft nur auf diesem Computer (127.0.0.1) und öffnet die Oberfläche im Browser.
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import privacy  # noqa: E402
from common import FACE_KEY_OFFSET, PREVIEWS, THUMBS, connect, to_abs  # noqa: E402

PORT = 8765
_local = threading.local()
_tree_cache = {}


def db():
    c = getattr(_local, "con", None)
    if c is None:
        c = _local.con = connect()
    return c


# ---------------------------------------------------------------- Filter ----

def fts_query(text):
    toks = re.findall(r"[\w\-]+", text or "", re.UNICODE)
    return " ".join('"%s"*' % t.replace('"', "") for t in toks if t)


def locked(h):
    """Sind private Fotos für diese Anfrage gesperrt?"""
    if getattr(h, "_locked", None) is None:
        h._locked = privacy.is_locked(db(), h._token)
    return h._locked


def is_blocked(h, iid):
    if not locked(h):
        return False
    row = db().execute("SELECT priv_eff FROM items WHERE id=?", (int(iid),)).fetchone()
    return bool(row and row[0])


def build_where(p):
    where, args = [], []
    cfg = common.load_config()
    if p.get("_hide_private"):
        where.append("COALESCE(i.priv_eff, 0) = 0")
    # Ausgeblendete Fotos nur in der Ansicht "Ausgeblendet", gelöschte nur im Papierkorb
    where.append("COALESCE(i.hidden, 0) = %d" % {"1": 1, "2": 2}.get(p.get("hidden"), 0))
    if cfg.get("hide_duplicates", True) and p.get("dups") != "1":
        where.append("i.dup_of IS NULL")
    if cfg.get("hide_raw_with_jpeg", True) and p.get("dups") != "1":
        where.append("i.raw_of IS NULL")
    q = (p.get("q") or "").strip()
    if q:
        fq = fts_query(q)
        if fq:
            where.append("i.id IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
            args.append(fq)
    for pid in filter(None, (p.get("persons") or "").split(",")):
        where.append("i.id IN (SELECT item_id FROM faces WHERE person_id=?)")
        args.append(int(pid))
    if p.get("from"):
        where.append("i.taken >= ?")
        args.append(p["from"])
    if p.get("to"):
        t = p["to"]
        t = t + "-12-31" if len(t) == 4 else (t + "-31" if len(t) == 7 else t)
        where.append("i.taken <= ?")
        args.append(t + " 23:59:59")
    kind = p.get("kind")
    if kind == "photo":
        where.append("i.kind IN ('photo','raw')")
    elif kind in ("video", "raw"):
        where.append("i.kind = ?")
        args.append(kind)
    if p.get("fav") == "1":
        where.append("(i.fav = 1 OR i.rating >= 4)")
    if p.get("nodate") == "1":
        where.append("(i.taken IS NULL OR i.taken_src IN ('file','undated'))")
    if p.get("album"):
        where.append("i.id IN (SELECT item_id FROM album_items WHERE album_id IN (%s))" % ",".join(
            str(int(a)) for a in album_tree_ids(int(p["album"]))))
    if p.get("event"):
        ev = db().execute("SELECT start, end FROM events WHERE id=?", (int(p["event"]),)).fetchone()
        if ev:
            where.append("i.taken >= ? AND i.taken <= ?")
            args += [ev[0][:10], ev[1][:10] + " 23:59:59"]
    if p.get("month"):
        where.append("substr(i.taken, 1, 7) = ?")
        args.append(p["month"])
    if p.get("geo") == "1":
        where.append("i.lat IS NOT NULL")
    folder = p.get("folder")
    if folder is not None and folder != "":
        if p.get("recursive") == "1":
            where.append("(i.folder = ? OR i.folder LIKE ?)")
            args += [folder, folder.replace("%", "\\%") + "/%"]
        else:
            where.append("i.folder = ?")
            args.append(folder)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def album_tree_ids(aid):
    ids, todo = [], [aid]
    while todo:
        a = todo.pop()
        ids.append(a)
        todo += [r[0] for r in db().execute("SELECT id FROM albums WHERE parent=?", (a,))]
    return ids


def events_by_month():
    out = {}
    for eid, name, start, end in db().execute("SELECT id, name, start, end FROM events ORDER BY start"):
        y, m = int(start[:4]), int(start[5:7])
        ey, em = int(end[:4]), int(end[5:7])
        while (y, m) <= (ey, em):
            out.setdefault("%04d-%02d" % (y, m), []).append({"id": eid, "name": name})
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def query_items(p):
    where, args = build_where(p)
    asc = p.get("sort") == "asc"
    if p.get("folder") and p.get("recursive") != "1":
        order = "i.name COLLATE NOCASE"
    else:
        order = "i.taken IS NULL, i.taken %s, i.id" % ("ASC" if asc else "DESC")
    rows = db().execute("SELECT i.id, i.taken, i.kind, COALESCE(i.priv_eff, 0) FROM items i%s ORDER BY %s"
                        % (where, order), args).fetchall()
    ids, groups, videos, private = [], [], [], []
    last = None
    for iid, taken, kind, priv in rows:
        if priv:
            private.append(len(ids))
        key = taken[:7] if taken else "ohne"
        if p.get("folder") and p.get("recursive") != "1":
            key = "ordner"
        if key != last:
            groups.append([key, 0])
            last = key
        groups[-1][1] += 1
        ids.append(iid)
        if kind == "video":
            videos.append(len(ids) - 1)
    out = {"ids": ids, "groups": groups, "videos": videos, "total": len(ids), "events": events_by_month()}
    # gesperrt: Platzhalter statt Vorschau; entsperrt: Liste nur fürs Nicht-Zwischenspeichern
    out["locked" if p.get("_locked") else "private"] = private
    return out


# --------------------------------------------------------------- Ordner ----

def folder_tree():
    key = db().execute("SELECT COUNT(*), MAX(id) FROM items").fetchone() + (privacy.VERSION[0],)
    if _tree_cache.get("key") == key:
        return _tree_cache["tree"]
    counts = {}
    covers = {}
    for folder, n, cover in db().execute(
            "SELECT folder, COUNT(*), MAX(CASE WHEN COALESCE(priv_eff,0)=0 THEN id END) FROM items "
            "WHERE dup_of IS NULL AND raw_of IS NULL AND COALESCE(hidden,0)=0 GROUP BY folder"):
        counts[folder] = n
        covers[folder] = cover
    tree = {}
    for folder, n in counts.items():
        parts = folder.split("/") if folder else []
        for d in range(len(parts) + 1):
            path = "/".join(parts[:d])
            node = tree.setdefault(path, {"path": path, "name": parts[d - 1] if d else "", "direct": 0, "total": 0,
                                          "children": set(), "cover": covers[folder]})
            node["total"] += n
            if d < len(parts):
                node["children"].add("/".join(parts[:d + 1]))
        tree[folder]["direct"] += n
        tree[folder]["cover"] = covers[folder]
    _tree_cache.update(key=key, tree=tree)
    return tree


# --------------------------------------------------------------- Bilder ----

def thumb_bytes(iid):
    data = THUMBS.get(iid)
    if data:
        return data
    row = db().execute("SELECT path, kind, userrot, edit FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        return None
    import media

    try:
        if row[1] == "video":
            im = media.rotate_cw(media.video_frame(to_abs(row[0]))[0], row[2] or 0)
        else:
            im = media.open_image(to_abs(row[0]), row[1], max_side=800, userrot=row[2] or 0, edit=row[3])[0]
        data = media.make_thumb(im)
        THUMBS.put(iid, data)
        return data
    except Exception:
        return None


def preview_bytes(iid):
    data = PREVIEWS.get(iid)
    if data:
        return data
    row = db().execute("SELECT path, kind, userrot, edit FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        return None
    import media

    try:
        data = media.make_preview(to_abs(row[0]), row[1], userrot=row[2] or 0, edit=row[3])
    except Exception:
        return thumb_bytes(iid)
    PREVIEWS.put(iid, data)
    return data


# ------------------------------------------------------------ Gesichter ----

_recompute_lock = threading.Lock()
RECOMPUTE = {"running": False, "result": None}


def recompute_faces_bg():
    import indexer

    if indexer.PROGRESS.running or RECOMPUTE["running"]:
        return False

    def run():
        import faces

        with _recompute_lock:
            RECOMPUTE["running"] = True
            con = connect()
            try:
                RECOMPUTE["result"] = faces.recompute(con)
                indexer.rebuild_fts(con)
            finally:
                con.close()
                RECOMPUTE["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def refresh_fts_for_faces(face_ids):
    import indexer

    if not face_ids:
        return
    ph = ",".join("?" * len(face_ids))
    items = [r[0] for r in db().execute("SELECT DISTINCT item_id FROM faces WHERE id IN (%s)" % ph, face_ids)]
    indexer.rebuild_fts(db(), items)


def add_rejected(con, face_id, pid):
    row = con.execute("SELECT rejected FROM faces WHERE id=?", (face_id,)).fetchone()
    rej = set(filter(None, (row[0] or "").split(","))) if row else set()
    rej.add(str(pid))
    con.execute("UPDATE faces SET rejected=? WHERE id=?", (",".join(sorted(rej)), face_id))


def person_by_name(name, create=True):
    name = name.strip()
    row = db().execute("SELECT id FROM persons WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row[0]
    if not create:
        return None
    cur = db().execute("INSERT INTO persons(name) VALUES(?)", (name,))
    db().commit()
    return cur.lastrowid


def face_rows(sql, args):
    return [{"id": r[0], "item": r[1], "score": r[2], "person": r[3], "source": r[4]}
            for r in db().execute(sql, args)]


# ------------------------------------------------------------ Handler ----

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # Antworten
    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in getattr(self, "_extra_headers", []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, ctype, cache=86400):
        if data is None:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=%d" % cache if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype=None):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self.send_error(404)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=3600")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
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

    def body(self):
        return self._body

    def _read_body(self):
        # Immer komplett lesen, sonst bleibt der Rest in der Verbindung und stört die nächste Anfrage
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            self._body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except ValueError:
            self._body = {}

    # Routing
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        self._read_body()
        self._locked = None
        self._extra_headers = []
        self._token = None
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == privacy.COOKIE:
                self._token = v
        u = urllib.parse.urlparse(self.path)
        p = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path
        try:
            for m, pattern, fn in ROUTES:
                if m != method:
                    continue
                mt = re.fullmatch(pattern, path)
                if mt:
                    return fn(self, p, *mt.groups())
            if method == "GET":
                return self.static(path)
            self.send_error(404)
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            import traceback

            traceback.print_exc()
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        full = os.path.normpath(os.path.join(common.STATIC_DIR, path.lstrip("/")))
        if not full.startswith(common.STATIC_DIR) or not os.path.isfile(full):
            return self.send_error(404)
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_bytes(data, ctype, cache=0)


# ------------------------------------------------------------- Routen ----

def r_ping(h, p):
    h.send_json({"app": "FotoArchiv", "base": common.BASE_DIR})


def r_status(h, p):
    import indexer

    c = db()
    st = {
        "progress": indexer.PROGRESS.snapshot(),
        "recompute": RECOMPUTE["running"],
        "items": c.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "pending": c.execute("SELECT COUNT(*) FROM items WHERE proc_ver = 0").fetchone()[0],
        "errors": c.execute("SELECT COUNT(*) FROM items WHERE error IS NOT NULL").fetchone()[0],
        "faces": c.execute("SELECT COUNT(*) FROM faces").fetchone()[0],
        "persons": c.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
        "last_index": (c.execute("SELECT value FROM meta WHERE key='last_index'").fetchone() or [None])[0],
        "root": common.LIB_ROOT,
        "root_ok": os.path.isdir(common.LIB_ROOT),
        "root_custom": bool(common.load_config().get("library_root")),
        "config": common.load_config(),
        "all_folders": common.top_level_folders(),
        "included": common.included_folders(),
        "private": {"password": privacy.has_password(c), "unlocked": not locked(h)},
    }
    h.send_json(st)


def r_config(h, p):
    b = h.body()
    cfg = common.load_config()
    for k in ("folders", "hide_duplicates", "hide_raw_with_jpeg", "workers"):
        if k in b:
            cfg[k] = b[k]
    common.save_config(cfg)
    h.send_json({"ok": True})


def r_index_start(h, p):
    import indexer

    if RECOMPUTE["running"]:
        return h.send_json({"ok": False, "error": "Personenerkennung läuft gerade"})
    h.send_json({"ok": indexer.start_background()})


def r_index_stop(h, p):
    import indexer

    indexer.PROGRESS.stop = True
    h.send_json({"ok": True})


def r_query(h, p):
    p = dict(p)
    if locked(h):
        p["_locked"] = 1
        # bei Suche/Personenfilter private Fotos ganz weglassen, damit Treffer nichts verraten
        if p.get("q") or p.get("persons"):
            p["_hide_private"] = 1
    h.send_json(query_items(p))


def r_years(h, p):
    rows = db().execute("SELECT substr(taken,1,4) y, COUNT(*) FROM items WHERE taken IS NOT NULL AND dup_of IS NULL "
                        "AND raw_of IS NULL AND COALESCE(hidden,0)=0 GROUP BY y ORDER BY y DESC").fetchall()
    h.send_json(rows)


def r_map(h, p):
    p = dict(p, geo="1")
    if locked(h):
        p["_hide_private"] = 1
    where, args = build_where(p)
    rows = db().execute("SELECT i.id, round(i.lat, 5), round(i.lon, 5) FROM items i%s" % where, args).fetchall()
    h.send_json(rows)


def r_item(h, p, iid):
    c = db()
    cols = ["id", "path", "folder", "name", "ext", "kind", "size", "taken", "taken_src", "width", "height", "duration",
            "lat", "lon", "place", "camera", "rating", "fav", "keywords", "caption", "dup_of", "raw_of", "error",
            "hidden", "usertags", "userrot", "trashed", "trash_from", "edit"]
    row = c.execute("SELECT %s FROM items WHERE id=?" % ",".join(cols), (int(iid),)).fetchone()
    if not row:
        return h.send_error(404)
    if is_blocked(h, iid):
        return h.send_json({"id": int(iid), "locked": True, "kind": row[cols.index("kind")]})
    d = dict(zip(cols, row))
    d["private"], d["priv_eff"] = c.execute("SELECT COALESCE(private,0), COALESCE(priv_eff,0) FROM items WHERE id=?",
                                            (int(iid),)).fetchone()
    d["faces"] = [dict(zip(("id", "x", "y", "w", "h", "person", "name", "source", "sugg", "sugg_name", "score"), r))
                  for r in c.execute(
            "SELECT f.id, f.x, f.y, f.w, f.h, f.person_id, p.name, f.source, f.sugg_person, s.name, f.sugg_score "
            "FROM faces f LEFT JOIN persons p ON p.id=f.person_id LEFT JOIN persons s ON s.id=f.sugg_person "
            "WHERE f.item_id=? AND f.source != 'ignored' ORDER BY f.x", (int(iid),))]
    d["copies"] = [r[0] for r in c.execute("SELECT path FROM items WHERE (dup_of=? OR raw_of=?) AND COALESCE(hidden,0) != 2",
                                           (int(iid), int(iid)))]
    rotfix = c.execute("SELECT rotfix FROM items WHERE id=?", (int(iid),)).fetchone()[0]
    d["browser_ok"] = (d["ext"] in common.BROWSER_IMAGE_EXT and (d["size"] or 0) < 40_000_000 and not rotfix
                       and not d["edit"])
    d["edit"] = json.loads(d["edit"]) if d["edit"] else None
    d["albums"] = [{"id": r[0], "name": r[1]} for r in c.execute(
        "SELECT a.id, a.name FROM album_items ai JOIN albums a ON a.id=ai.album_id WHERE ai.item_id=?", (int(iid),))]
    d["events"] = [{"id": r[0], "name": r[1]} for r in c.execute(
        "SELECT id, name FROM events WHERE ? BETWEEN substr(start,1,10) AND substr(end,1,10)", ((d["taken"] or "")[:10],))]
    h.send_json(d)


def r_fav(h, p, iid):
    b = h.body()
    db().execute("UPDATE items SET fav=? WHERE id=?", (1 if b.get("fav") else 0, int(iid)))
    db().commit()
    h.send_json({"ok": True})


def r_rotate(h, p, iid):
    """Foto um 90° drehen (nur in FotoArchiv, die Datei bleibt unverändert)."""
    rot = rotate_item(int(iid), h.body().get("dir", "cw") == "cw")
    if rot is None:
        return h.send_error(404)
    h.send_json({"ok": True, "rot": rot})


def rotate_item(iid, cw):
    c = db()
    row = c.execute("SELECT userrot, width, height, edit FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        return None
    e = json.loads(row[3]) if row[3] else None
    if e:
        # Zuschnitt dreht sichtbar mit. Bei gespiegeltem Foto dreht die Grundlage andersherum
        # (Spiegeln kehrt die Drehrichtung um), damit das Ergebnis wie erwartet aussieht.
        if e.get("crop"):
            x, y, w, h = e["crop"]
            e["crop"] = [1 - y - h, x, h, w] if cw else [y, 1 - x - w, h, w]
        if e.get("flip"):
            cw = not cw
        c.execute("UPDATE items SET edit=? WHERE id=?", (json.dumps(e), iid))
    rot = ((row[0] or 0) + (90 if cw else 270)) % 360
    c.execute("UPDATE items SET userrot=?, rotfix=?, width=?, height=? WHERE id=?",
              (rot, 1 if rot else 0, row[2], row[1], iid))
    faces = c.execute("SELECT id, x, y, w, h FROM faces WHERE item_id=? AND x IS NOT NULL", (iid,)).fetchall()
    for fid, x, y, w, hh in faces:
        nx, ny, nw, nh = (1 - y - hh, x, hh, w) if cw else (y, 1 - x - w, hh, w)
        c.execute("UPDATE faces SET x=?, y=?, w=?, h=? WHERE id=?", (nx, ny, nw, nh, fid))
    c.commit()
    THUMBS.delete([iid] + [FACE_KEY_OFFSET + f[0] for f in faces])
    PREVIEWS.delete([iid])
    thumb_bytes(iid)
    return rot


def _private_cache(iid, cache):
    row = db().execute("SELECT priv_eff FROM items WHERE id=?", (int(iid),)).fetchone()
    return 0 if row and row[0] else cache  # private Bilder nie im Browser zwischenspeichern


def r_thumb(h, p, iid):
    if is_blocked(h, iid):
        return h.send_bytes(privacy.lock_image(), "image/jpeg", cache=0)
    h.send_bytes(thumb_bytes(int(iid)), "image/jpeg", cache=_private_cache(iid, 600))


def r_preview(h, p, iid):
    if is_blocked(h, iid):
        return h.send_bytes(privacy.lock_image(), "image/jpeg", cache=0)
    h.send_bytes(preview_bytes(int(iid)), "image/jpeg", cache=_private_cache(iid, 86400))


def r_face_img(h, p, fid):
    if locked(h):
        row = db().execute("SELECT i.priv_eff FROM faces f JOIN items i ON i.id=f.item_id WHERE f.id=?",
                           (int(fid),)).fetchone()
        if row and row[0]:
            return h.send_bytes(privacy.lock_image(), "image/jpeg", cache=0)
    data = THUMBS.get(FACE_KEY_OFFSET + int(fid))
    if data is None:
        row = db().execute("SELECT i.path, i.kind, f.x, f.y, f.w, f.h, i.userrot FROM faces f JOIN items i "
                           "ON i.id=f.item_id WHERE f.id=?", (int(fid),)).fetchone()
        if row and row[2] is not None:
            import media

            try:
                im = media.open_image(to_abs(row[0]), row[1], max_side=1280, userrot=row[6] or 0)[0]
                data = media._crop(im, row[2] * im.width, row[3] * im.height, row[4] * im.width, row[5] * im.height)
                THUMBS.put(FACE_KEY_OFFSET + int(fid), data)
            except Exception:
                data = None
    h.send_bytes(data, "image/jpeg")


VIDEO_TYPES = {"mov": "video/mp4", "mp4": "video/mp4", "m4v": "video/mp4", "qt": "video/mp4", "webm": "video/webm"}


def r_original(h, p, iid):
    row = db().execute("SELECT path, ext FROM items WHERE id=?", (int(iid),)).fetchone()
    if not row:
        return h.send_error(404)
    if is_blocked(h, iid):
        return h.send_error(403)
    h.send_file(to_abs(row[0]), VIDEO_TYPES.get(row[1]))


def r_open(h, p, iid, mode):
    row = db().execute("SELECT path FROM items WHERE id=?", (int(iid),)).fetchone()
    if not row:
        return h.send_error(404)
    if is_blocked(h, iid):
        return h.send_error(403)
    path = to_abs(row[0])
    if sys.platform == "win32":
        if mode == "reveal":
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if mode == "reveal" else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if mode == "reveal" else path])
    h.send_json({"ok": True})


def r_folders(h, p):
    tree = folder_tree()
    path = p.get("path", "")
    node = tree.get(path)
    if node is None:
        return h.send_json({"path": path, "children": [], "direct": 0})
    kids = sorted((tree[c] for c in node["children"]), key=lambda n: n["name"].lower(),
                  reverse=bool(re.fullmatch(r"\d{4}", (tree[next(iter(node["children"]))]["name"] if node["children"] else ""))))
    h.send_json({"path": path, "name": node["name"], "direct": node["direct"], "total": node["total"],
                 "children": [{"path": k["path"], "name": k["name"], "total": k["total"], "cover": k["cover"]}
                              for k in kids]})


def r_persons(h, p):
    # Titelbild fehlt oder zeigt auf ein Gesicht, das beim Neu-Verarbeiten ersetzt wurde: neu wählen
    if db().execute("SELECT 1 FROM persons p LEFT JOIN faces f ON f.id = p.cover_face AND f.person_id = p.id "
                    "WHERE f.id IS NULL LIMIT 1").fetchone():
        import faces

        c = db()
        faces._covers(c)
        c.commit()
    rows = db().execute("""
        SELECT p.id, p.name, p.cover_face, p.hidden,
          (SELECT COUNT(DISTINCT f.item_id) FROM faces f JOIN items i ON i.id = f.item_id
             WHERE f.person_id = p.id AND i.dup_of IS NULL AND i.raw_of IS NULL AND COALESCE(i.hidden,0) != 2),
          (SELECT COUNT(*) FROM faces WHERE person_id = p.id AND source IN ('mylio','manual')),
          (SELECT COUNT(*) FROM faces WHERE person_id = p.id AND source = 'auto'),
          (SELECT COUNT(*) FROM faces f JOIN items i ON i.id = f.item_id
             WHERE f.sugg_person = p.id AND f.person_id IS NULL AND i.dup_of IS NULL AND COALESCE(i.hidden,0) != 2)
        FROM persons p ORDER BY 5 DESC""").fetchall()
    out = [dict(zip(("id", "name", "cover", "hidden", "items", "confirmed", "auto", "suggestions"), r)) for r in rows]
    if locked(h):
        for d in out:
            if d["cover"] and db().execute("SELECT i.priv_eff FROM faces f JOIN items i ON i.id=f.item_id "
                                           "WHERE f.id=?", (d["cover"],)).fetchone()[0]:
                alt = db().execute("SELECT f.id FROM faces f JOIN items i ON i.id=f.item_id WHERE f.person_id=? "
                                   "AND f.x IS NOT NULL AND COALESCE(i.priv_eff,0)=0 AND f.source IN ('mylio','manual') "
                                   "ORDER BY MIN(f.px, 250) * COALESCE(f.score, 0.5) DESC LIMIT 1", (d["id"],)).fetchone()
                d["cover"] = alt[0] if alt else None
    h.send_json(out)


def r_person_create(h, p):
    b = h.body()
    h.send_json({"id": person_by_name(b["name"])})


def r_person_update(h, p, pid):
    b = h.body()
    pid = int(pid)
    c = db()
    if "name" in b:
        other = person_by_name(b["name"], create=False)
        if other and other != pid:
            return h.send_json({"error": "exists", "other": other}, 409)
        c.execute("UPDATE persons SET name=? WHERE id=?", (b["name"].strip(), pid))
    if "hidden" in b:
        c.execute("UPDATE persons SET hidden=? WHERE id=?", (1 if b["hidden"] else 0, pid))
    if "cover" in b:
        c.execute("UPDATE persons SET cover_face=? WHERE id=?", (int(b["cover"]), pid))
    c.commit()
    items = [r[0] for r in c.execute("SELECT DISTINCT item_id FROM faces WHERE person_id=?", (pid,))]
    import indexer

    indexer.rebuild_fts(c, items)
    h.send_json({"ok": True})


def r_person_merge(h, p, pid):
    into = int(h.body()["into"])
    pid = int(pid)
    c = db()
    items = [r[0] for r in c.execute("SELECT DISTINCT item_id FROM faces WHERE person_id=?", (pid,))]
    c.execute("UPDATE faces SET person_id=? WHERE person_id=?", (into, pid))
    c.execute("UPDATE faces SET sugg_person=? WHERE sugg_person=?", (into, pid))
    c.execute("DELETE FROM persons WHERE id=?", (pid,))
    c.commit()
    import indexer

    indexer.rebuild_fts(c, items)
    h.send_json({"ok": True})


def r_person_delete(h, p, pid):
    pid = int(pid)
    c = db()
    items = [r[0] for r in c.execute("SELECT DISTINCT item_id FROM faces WHERE person_id=?", (pid,))]
    c.execute("UPDATE faces SET person_id=NULL, source='det' WHERE person_id=? AND x IS NOT NULL", (pid,))
    c.execute("DELETE FROM faces WHERE person_id=? AND x IS NULL", (pid,))
    c.execute("UPDATE faces SET sugg_person=NULL, sugg_score=NULL WHERE sugg_person=?", (pid,))
    c.execute("DELETE FROM persons WHERE id=?", (pid,))
    c.commit()
    import indexer

    indexer.rebuild_fts(c, items)
    h.send_json({"ok": True})


def r_person_faces(h, p, pid):
    kind = p.get("type", "sugg")
    off, lim = int(p.get("offset", 0)), int(p.get("limit", 200))
    pid = int(pid)
    priv = (" AND COALESCE(i.priv_eff,0)=0" if locked(h) else "") + " AND COALESCE(i.hidden,0) != 2 "
    if kind == "sugg":
        sql = ("SELECT f.id, f.item_id, f.sugg_score, f.sugg_person, 'sugg' FROM faces f JOIN items i ON i.id=f.item_id "
               "WHERE f.sugg_person=? AND f.person_id IS NULL AND i.dup_of IS NULL" + priv +
               "ORDER BY f.sugg_score DESC LIMIT ? OFFSET ?")
    elif kind == "auto":
        sql = ("SELECT f.id, f.item_id, f.sugg_score, f.person_id, f.source FROM faces f JOIN items i ON i.id=f.item_id "
               "WHERE f.person_id=? AND f.source='auto' AND i.dup_of IS NULL" + priv +
               "ORDER BY f.sugg_score ASC LIMIT ? OFFSET ?")
    else:
        sql = ("SELECT f.id, f.item_id, f.score, f.person_id, f.source FROM faces f JOIN items i ON i.id=f.item_id "
               "WHERE f.person_id=? AND f.source IN ('mylio','manual') AND f.x IS NOT NULL" + priv +
               "ORDER BY i.taken DESC LIMIT ? OFFSET ?")
    h.send_json(face_rows(sql, (pid, lim, off)))


def r_faces_assign(h, p):
    b = h.body()
    ids = [int(x) for x in b["faces"]]
    pid = b.get("person") or person_by_name(b["name"])
    c = db()
    c.executemany("UPDATE faces SET person_id=?, source='manual', sugg_person=NULL, sugg_score=NULL, cluster=NULL "
                  "WHERE id=?", [(pid, i) for i in ids])
    c.commit()
    refresh_fts_for_faces(ids)
    h.send_json({"ok": True, "person": pid})


def r_faces_reject(h, p):
    """Vorschlag oder automatische Zuordnung ablehnen bzw. Zuordnung entfernen."""
    ids = [int(x) for x in h.body()["faces"]]
    c = db()
    for fid in ids:
        row = c.execute("SELECT person_id, sugg_person, x FROM faces WHERE id=?", (fid,)).fetchone()
        if not row:
            continue
        pid = row[0] or row[1]
        if pid:
            add_rejected(c, fid, pid)
        if row[2] is None:
            c.execute("DELETE FROM faces WHERE id=?", (fid,))
        else:
            c.execute("UPDATE faces SET person_id=NULL, source='det', sugg_person=NULL, sugg_score=NULL WHERE id=?",
                      (fid,))
    c.commit()
    refresh_fts_for_faces(ids)
    h.send_json({"ok": True})


def r_faces_ignore(h, p):
    ids = [int(x) for x in h.body()["faces"]]
    c = db()
    c.executemany("UPDATE faces SET person_id=NULL, source='ignored', sugg_person=NULL, cluster=NULL WHERE id=?",
                  [(i,) for i in ids])
    c.commit()
    refresh_fts_for_faces(ids)
    h.send_json({"ok": True})


def r_clusters(h, p):
    off, lim = int(p.get("offset", 0)), int(p.get("limit", 60))
    priv = (" AND COALESCE(i.priv_eff,0)=0" if locked(h) else "") + " AND COALESCE(i.hidden,0) != 2"
    rows = db().execute("SELECT f.cluster, COUNT(*) n FROM faces f JOIN items i ON i.id=f.item_id WHERE f.cluster IS NOT NULL "
                        "AND f.person_id IS NULL AND f.source='det'" + priv +
                        " GROUP BY f.cluster HAVING n >= 3 ORDER BY n DESC LIMIT ? OFFSET ?", (lim, off)).fetchall()
    out = []
    for cl, n in rows:
        faces = [r[0] for r in db().execute("SELECT f.id FROM faces f JOIN items i ON i.id=f.item_id WHERE f.cluster=? "
                                            "AND f.person_id IS NULL AND f.source='det'" + priv +
                                            " ORDER BY f.score DESC LIMIT 8", (cl,))]
        out.append({"cluster": cl, "count": n, "faces": faces})
    h.send_json(out)


def r_cluster(h, p, cl):
    priv = (" AND COALESCE(i.priv_eff,0)=0" if locked(h) else "") + " AND COALESCE(i.hidden,0) != 2"
    h.send_json(face_rows("SELECT f.id, f.item_id, f.score, f.person_id, f.source FROM faces f JOIN items i "
                          "ON i.id=f.item_id WHERE f.cluster=? AND f.person_id IS NULL AND f.source='det'" + priv +
                          " ORDER BY f.score DESC LIMIT 1000", (int(cl),)))


def r_calendar(h, p):
    """Jahre × Monate wie bei Mylio: Anzahl, Titelbild und Ereignis pro Monat."""
    c = db()
    people = {r[0]: r[1] for r in c.execute(
        "SELECT item_id, COUNT(DISTINCT person_id) FROM faces INDEXED BY faces_cov_person2 "
        "WHERE person_id IS NOT NULL GROUP BY item_id")}
    best = {}
    hide = locked(h)
    for iid, taken, kind, fav, rating, camera, ext, priv in c.execute(
            "SELECT id, taken, kind, fav, rating, camera, ext, COALESCE(priv_eff,0) FROM items "
            "WHERE taken IS NOT NULL AND dup_of IS NULL "
            "AND raw_of IS NULL AND COALESCE(hidden,0)=0 AND (taken_src IS NULL OR taken_src NOT IN ('file','undated'))"):
        key = taken[:7]
        # Titelbild: Favoriten/Bewertung und Fotos mit Personen bevorzugen, Screenshots/Belege
        # (PNG, ohne Kamera) möglichst nicht, sonst zufällig aber stabil
        score = ((fav or 0) * 6 + (rating or 0) * 1.5 + min(people.get(iid, 0), 3) * 2 + (kind != "video") * 2
                 + (camera is not None) * 3 - (ext == "png") * 4 + ((iid * 2654435761) % 1000) / 1000)
        if hide and priv:
            score = -1e9  # nie als Titelbild, solange gesperrt
        b = best.get(key)
        if b is None:
            best[key] = [1, iid, score]
        else:
            b[0] += 1
            if score > b[2]:
                b[1], b[2] = iid, score
    ev = events_by_month()
    h.send_json({k: {"count": v[0], "cover": v[1] if v[2] > -1e8 else None, "events": ev.get(k, [])}
                 for k, v in best.items()})


def r_albums(h, p):
    c = db()
    albums = []
    hide = locked(h)
    priv_albums = privacy.private_albums(c)
    for aid, name, parent, desc, own_cover, priv in c.execute(
            "SELECT id, name, parent, description, cover, COALESCE(private,0) FROM albums ORDER BY name COLLATE NOCASE"):
        n, cover, open_cover = c.execute(
            "SELECT COUNT(*), MAX(ai.item_id), MAX(CASE WHEN COALESCE(i.priv_eff,0)=0 THEN ai.item_id END) "
            "FROM album_items ai JOIN items i ON i.id=ai.item_id "
            "WHERE ai.album_id=? AND i.dup_of IS NULL AND COALESCE(i.hidden,0)=0", (aid,)).fetchone()
        cover = own_cover or cover
        if hide:
            if aid in priv_albums:
                cover = None
            elif cover and c.execute("SELECT priv_eff FROM items WHERE id=?", (cover,)).fetchone()[0]:
                cover = open_cover
        albums.append({"id": aid, "name": name, "parent": parent, "description": desc, "count": n, "cover": cover,
                       "private": bool(priv), "private_eff": aid in priv_albums})
    # Zahlen der Unteralben mitzählen
    kids = {}
    for a in albums:
        kids.setdefault(a["parent"], []).append(a)

    def total(a):
        return a["count"] + sum(total(k) for k in kids.get(a["id"], []))
    for a in albums:
        a["total"] = total(a)
        if not a["cover"] and not (hide and a["private_eff"]):
            sub = next((k["cover"] for k in kids.get(a["id"], []) if k["cover"]), None)
            a["cover"] = sub
    events = []
    for eid, name, start, end, priv in c.execute(
            "SELECT id, name, start, end, COALESCE(private,0) FROM events ORDER BY start DESC").fetchall():
        rows = c.execute("SELECT id, COALESCE(priv_eff,0) FROM items WHERE taken >= ? AND taken <= ? AND dup_of IS NULL "
                         "AND raw_of IS NULL AND COALESCE(hidden,0)=0 ORDER BY fav DESC, rating DESC, taken",
                         (start[:10], end[:10] + " 23:59:59")).fetchall()
        open_rows = [r for r in rows if not r[1]] if hide else rows
        cover = open_rows[len(open_rows) // 3][0] if open_rows and not (hide and priv) else None
        events.append({"id": eid, "name": name, "start": start, "end": end, "count": len(rows), "cover": cover,
                       "private": bool(priv)})
    hidden = c.execute("SELECT COUNT(*) FROM items WHERE hidden=1").fetchone()[0]
    h.send_json({"albums": albums, "events": events, "hidden": hidden})


# ------------------------------------------------- Bearbeiten (Mehrfachauswahl) ----

def _ids(b):
    return [int(x) for x in b.get("ids", [])]


def _chunks(ids, n=500):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _refresh(ids, private=True):
    import indexer

    for ch in _chunks(ids, 2000):
        indexer.rebuild_fts(db(), ch)
    if private:
        privacy.refresh(db())


def _visible_ids(h, ids):
    if not locked(h) or not ids:
        return ids
    blocked = set()
    for ch in _chunks(ids):
        blocked |= {r[0] for r in db().execute("SELECT id FROM items WHERE priv_eff=1 AND id IN (%s)"
                                               % ",".join("?" * len(ch)), ch)}
    return [i for i in ids if i not in blocked]


def r_items_fav(h, p):
    b = h.body()
    db().executemany("UPDATE items SET fav=? WHERE id=?", [(1 if b.get("fav") else 0, i) for i in _ids(b)])
    db().commit()
    h.send_json({"ok": True})


def r_items_hide(h, p):
    b = h.body()
    db().executemany("UPDATE items SET hidden=? WHERE id=? AND COALESCE(hidden,0) != 2",
                     [(1 if b.get("hidden") else 0, i) for i in _ids(b)])
    db().commit()
    _tree_cache.clear()
    h.send_json({"ok": True})


def r_items_rotate(h, p):
    b = h.body()
    for i in _ids(b):
        rotate_item(i, b.get("dir", "cw") == "cw")
    h.send_json({"ok": True})


def r_items_date(h, p):
    """Aufnahmedatum setzen. date = 'JJJJ-MM-TT' (Uhrzeit bleibt) oder 'JJJJ-MM-TT HH:MM'."""
    b = h.body()
    ids = _ids(b)
    c = db()
    d = (b.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", d):
        return h.send_json({"error": "Datum bitte als JJJJ-MM-TT"}, 400)
    for i in ids:
        old = (c.execute("SELECT taken FROM items WHERE id=?", (i,)).fetchone() or [None])[0]
        t = d + ":00" if len(d) == 16 else d + " " + (old[11:] if old and len(old) >= 19 else "12:00:00")
        c.execute("UPDATE items SET taken=?, usertaken=?, taken_src='manual' WHERE id=?", (t, t, i))
    c.commit()
    _refresh(ids)
    h.send_json({"ok": True})


def r_items_tags(h, p):
    b = h.body()
    ids = _ids(b)
    add = [t.strip() for t in b.get("add", []) if t.strip()]
    rem = {t.strip().lower() for t in b.get("remove", [])}
    c = db()
    for i in ids:
        cur = (c.execute("SELECT usertags FROM items WHERE id=?", (i,)).fetchone() or [None])[0]
        tags = [t for t in (cur or "").split(", ") if t and t.lower() not in rem]
        for t in add:
            if t.lower() not in {x.lower() for x in tags}:
                tags.append(t)
        c.execute("UPDATE items SET usertags=? WHERE id=?", (", ".join(tags) or None, i))
    c.commit()
    _refresh(ids)
    h.send_json({"ok": True})


def r_tags(h, p):
    counts = {}
    for (t,) in db().execute("SELECT usertags FROM items WHERE usertags IS NOT NULL"):
        for x in t.split(", "):
            counts[x] = counts.get(x, 0) + 1
    h.send_json(sorted(counts.items(), key=lambda kv: kv[0].lower()))


def r_items_person(h, p):
    """Person zu Fotos hinzufügen (ohne Gesichtsrahmen) oder entfernen."""
    b = h.body()
    ids = _ids(b)
    c = db()
    pid = b.get("person") or person_by_name(b["name"])
    if b.get("remove"):
        for ch in _chunks(ids):
            ph = ",".join("?" * len(ch))
            c.execute("DELETE FROM faces WHERE person_id=? AND x IS NULL AND item_id IN (%s)" % ph, [pid] + ch)
            c.execute("UPDATE faces SET person_id=NULL, source='det' WHERE person_id=? AND item_id IN (%s)" % ph,
                      [pid] + ch)
    else:
        have = set()
        for ch in _chunks(ids):
            ph = ",".join("?" * len(ch))
            have |= {r[0] for r in c.execute("SELECT DISTINCT item_id FROM faces WHERE person_id=? AND item_id IN (%s)"
                                             % ph, [pid] + ch)}
        c.executemany("INSERT INTO faces(item_id, person_id, source) VALUES(?, ?, 'manual')",
                      [(i, pid) for i in ids if i not in have])
    c.commit()
    _refresh(ids)
    h.send_json({"ok": True, "person": pid})


def r_album_create(h, p):
    b = h.body()
    name = (b.get("name") or "").strip()
    if not name:
        return h.send_json({"error": "Name fehlt"}, 400)
    c = db()
    aid = c.execute("INSERT INTO albums(name, parent, source) VALUES(?, ?, 'user')",
                    (name, b.get("parent"))).lastrowid
    ids = _ids(b)
    c.executemany("INSERT OR IGNORE INTO album_items(album_id, item_id) VALUES(?,?)", [(aid, i) for i in ids])
    c.commit()
    _refresh(ids)
    h.send_json({"ok": True, "id": aid})


def r_album_update(h, p, aid):
    b = h.body()
    aid = int(aid)
    c = db()
    if "name" in b and b["name"].strip():
        c.execute("UPDATE albums SET name=? WHERE id=?", (b["name"].strip(), aid))
    if "parent" in b:
        par = b["parent"]
        # nicht in sich selbst oder eigene Unteralben verschieben
        if par is not None and int(par) in album_tree_ids(aid):
            return h.send_json({"error": "Ein Album kann nicht in sich selbst liegen"}, 400)
        c.execute("UPDATE albums SET parent=? WHERE id=?", (par, aid))
    if "cover" in b:
        c.execute("UPDATE albums SET cover=? WHERE id=?", (b["cover"], aid))
    if "description" in b:
        c.execute("UPDATE albums SET description=? WHERE id=?", (b["description"] or None, aid))
    if "private" in b:
        if not b["private"] and locked(h):
            return h.send_json({"error": "Zum Aufheben bitte erst entsperren"}, 403)
        if b["private"] and not privacy.has_password(c):
            return h.send_json({"error": "Bitte zuerst ein Passwort für Privates festlegen"}, 400)
        c.execute("UPDATE albums SET private=? WHERE id=?", (1 if b["private"] else 0, aid))
    c.commit()
    items = [r[0] for r in c.execute("SELECT item_id FROM album_items WHERE album_id=?", (aid,))]
    _refresh(items)
    h.send_json({"ok": True})


def r_album_delete(h, p, aid):
    """Album löschen (die Fotos bleiben natürlich erhalten). Unteralben rücken eine Ebene hoch."""
    aid = int(aid)
    c = db()
    row = c.execute("SELECT name, parent, source FROM albums WHERE id=?", (aid,)).fetchone()
    if not row:
        return h.send_error(404)
    items = [r[0] for r in c.execute("SELECT item_id FROM album_items WHERE album_id=?", (aid,))]
    if h.body().get("items"):
        _trash_items(h, items)
    c.execute("UPDATE albums SET parent=? WHERE parent=?", (row[1], aid))
    c.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
    c.execute("DELETE FROM albums WHERE id=?", (aid,))
    if row[2] == "mylio":
        _remember_deleted(c, "a:" + row[0])
    c.commit()
    _refresh(items)
    h.send_json({"ok": True})


def _remember_deleted(c, key):
    # damit ein erneuter Mylio-Import Gelöschtes nicht wiederherstellt
    cur = c.execute("SELECT value FROM meta WHERE key='mylio_deleted'").fetchone()
    lst = json.loads(cur[0]) if cur else []
    if key not in lst:
        lst.append(key)
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('mylio_deleted', ?)", (json.dumps(lst),))


def r_album_add(h, p, aid):
    ids = _ids(h.body())
    c = db()
    c.executemany("INSERT OR IGNORE INTO album_items(album_id, item_id) VALUES(?,?)", [(int(aid), i) for i in ids])
    c.executemany("DELETE FROM album_removed WHERE album_id=? AND item_id=?", [(int(aid), i) for i in ids])
    c.commit()
    _refresh(ids)
    h.send_json({"ok": True})


def r_album_remove(h, p, aid):
    ids = _ids(h.body())
    c = db()
    c.executemany("DELETE FROM album_items WHERE album_id=? AND item_id=?", [(int(aid), i) for i in ids])
    c.executemany("INSERT OR IGNORE INTO album_removed(album_id, item_id) VALUES(?,?)", [(int(aid), i) for i in ids])
    c.commit()
    _refresh(ids)
    h.send_json({"ok": True})


def _event_items(start, end):
    return [r[0] for r in db().execute("SELECT id FROM items WHERE taken >= ? AND taken <= ?",
                                       (start[:10], end[:10] + " 23:59:59"))]


def r_event_create(h, p):
    """Ereignis anlegen: mit Zeitraum (start/end) oder aus markierten Fotos (Zeitraum = ältestes bis neuestes)."""
    b = h.body()
    name = (b.get("name") or "").strip()
    c = db()
    start, end = b.get("start"), b.get("end")
    ids = _ids(b)
    if ids and not start:
        ph = ",".join("?" * len(ids[:900]))
        start, end = c.execute("SELECT MIN(taken), MAX(taken) FROM items WHERE taken IS NOT NULL AND id IN (%s)" % ph,
                               ids[:900]).fetchone()
    if not name or not start:
        return h.send_json({"error": "Name und Zeitraum (bzw. Fotos mit Datum) nötig"}, 400)
    end = end or start
    eid = c.execute("INSERT INTO events(name, start, end, source) VALUES(?,?,?,'user')",
                    (name, start[:10] + " 00:00:00", end[:10] + " 23:59:59")).lastrowid
    c.commit()
    _refresh(_event_items(start, end))
    h.send_json({"ok": True, "id": eid})


def r_event_update(h, p, eid):
    b = h.body()
    c = db()
    row = c.execute("SELECT name, start, end FROM events WHERE id=?", (int(eid),)).fetchone()
    if not row:
        return h.send_error(404)
    if "private" in b:
        if not b["private"] and locked(h):
            return h.send_json({"error": "Zum Aufheben bitte erst entsperren"}, 403)
        if b["private"] and not privacy.has_password(c):
            return h.send_json({"error": "Bitte zuerst ein Passwort für Privates festlegen"}, 400)
        c.execute("UPDATE events SET private=? WHERE id=?", (1 if b["private"] else 0, int(eid)))
    name = (b.get("name") or row[0]).strip()
    start = (b.get("start") or row[1])[:10] + " 00:00:00"
    end = (b.get("end") or row[2])[:10] + " 23:59:59"
    if end < start:
        start, end = end[:10] + " 00:00:00", start[:10] + " 23:59:59"
    c.execute("UPDATE events SET name=?, start=?, end=? WHERE id=?", (name, start, end, int(eid)))
    c.commit()
    _refresh(sorted(set(_event_items(row[1], row[2]) + _event_items(start, end))))
    h.send_json({"ok": True})


def r_event_delete(h, p, eid):
    c = db()
    row = c.execute("SELECT name, start, end, source FROM events WHERE id=?", (int(eid),)).fetchone()
    if not row:
        return h.send_error(404)
    if h.body().get("items"):
        _trash_items(h, [r[0] for r in c.execute(
            "SELECT id FROM items WHERE taken >= ? AND taken <= ? AND dup_of IS NULL AND raw_of IS NULL "
            "AND COALESCE(hidden,0)=0", (row[1][:10], row[2][:10] + " 23:59:59"))])
    c.execute("DELETE FROM events WHERE id=?", (int(eid),))
    if row[3] == "mylio":
        _remember_deleted(c, "e:" + row[0])
    c.commit()
    _refresh(_event_items(row[1], row[2]))
    h.send_json({"ok": True})


# ------------------------------------------------------------ Papierkorb ----

def _trash_items(h, ids):
    import trash

    ids = trash.with_copies(db(), _visible_ids(h, ids))
    n, errors = trash.move_to_trash(db(), ids)
    _tree_cache.clear()
    return n, errors, ids


def _after_restore():
    import indexer

    _tree_cache.clear()
    if not indexer.PROGRESS.running:  # Duplikate/RAW-Paare neu bestimmen (sonst beim nächsten Einlesen)
        indexer.mark_duplicates(db())
        indexer.PROGRESS.phase = ""


def r_items_delete(h, p):
    n, errors, ids = _trash_items(h, _ids(h.body()))
    h.send_json({"ok": True, "count": n, "errors": errors[:20], "ids": ids})


def r_folder_delete(h, p):
    import trash

    folder = (h.body().get("path") or "").strip("/")
    if not folder:
        return h.send_json({"error": "Die oberste Ebene kann nicht gelöscht werden"}, 400)
    like = folder.replace("%", "\\%").replace("_", "\\_") + "/%"
    if locked(h) and db().execute("SELECT 1 FROM items WHERE (folder=? OR folder LIKE ? ESCAPE '\\') AND priv_eff=1 "
                                  "LIMIT 1", (folder, like)).fetchone():
        return h.send_json({"error": "Der Ordner enthält private Fotos – bitte erst entsperren"}, 403)
    n, errors, others = trash.trash_folder(db(), folder)
    _tree_cache.clear()
    h.send_json({"ok": True, "count": n, "errors": errors[:20], "others": others[:20], "others_total": len(others)})


def r_trash_status(h, p):
    import trash

    h.send_json(trash.stats(db()))


def r_trash_restore(h, p):
    import trash

    n, errors = trash.restore(db(), _visible_ids(h, _ids(h.body())))
    _after_restore()
    h.send_json({"ok": True, "count": n, "errors": errors[:20]})


def r_trash_purge(h, p):
    import trash

    b = h.body()
    if locked(h):
        return h.send_json({"error": "Bitte zuerst Privates entsperren"}, 403)
    n, errors = trash.purge(db(), None if b.get("all") else _ids(b))
    _tree_cache.clear()
    h.send_json({"ok": True, "count": n, "errors": errors[:20]})


# ------------------------------------------------------------- Duplikate ----

def _dup_filter(h, p):
    import dups

    kind, folder = p.get("kind") or "", p.get("folder") or ""
    hide = locked(h)
    return [g for g in dups.groups(db()) if (not kind or g[3] == kind or (kind == "photo" and g[3] == "raw"))
            and (not folder or folder in g[5]) and not (hide and g[4])]


def r_dups(h, p):
    import dups

    off, lim = int(p.get("offset", 0)), min(int(p.get("limit", 30)), 200)
    prefer = p.get("prefer") or None
    all_groups = _dup_filter(h, p)
    page = all_groups[off:off + lim]
    info = dups.details(db(), [i for g in page for i in g[1]]) if page else {}
    out = []
    for g in page:
        members = [info[i] for i in g[1] if i in info]
        if len(members) < 2:
            continue
        best, rest = dups.suggest(members, prefer)
        out.append({"id": g[0], "keep": best["id"], "reason": dups.reason(best, rest[0], prefer),
                    "members": sorted(members, key=lambda m: m["id"] != best["id"])})
    folders = {}
    for g in dups.groups(db()):
        for f in g[5]:
            folders[f] = folders.get(f, 0) + 1
    files = sum(len(g[1]) - 1 for g in all_groups)
    h.send_json({"total": len(all_groups), "files": files, "groups": out,
                 "folders": sorted(folders.items(), key=lambda kv: -kv[1])})


def r_dups_resolve(h, p):
    import dups

    n, errors, keeps = 0, [], []
    for g in h.body().get("groups", []):
        ids = _visible_ids(h, [int(g["keep"])] + [int(x) for x in g.get("remove", [])])
        if len(ids) < 2 or ids[0] != int(g["keep"]):
            continue
        c, e = dups.resolve(db(), ids[0], ids[1:])
        n += c
        errors += e
        keeps.append(ids[0])
    _refresh(keeps)
    _tree_cache.clear()
    h.send_json({"ok": True, "count": n, "errors": errors[:20]})


def r_dups_keep(h, p):
    import dups

    dups.keep_all(db(), _visible_ids(h, _ids(h.body())))
    _tree_cache.clear()
    h.send_json({"ok": True})


def r_dups_all(h, p):
    import dups

    b = h.body()

    def after(con):
        import indexer

        privacy.refresh(con)
        indexer.rebuild_fts(con)
        con.close()
        _tree_cache.clear()
    ok = dups.run_all(connect, b.get("prefer") or None, b.get("kind") or "", b.get("folder") or "", locked(h), after)
    h.send_json({"ok": ok})


def r_dups_job(h, p):
    import dups

    h.send_json(dups.JOB)


def r_dups_stop(h, p):
    import dups

    dups.JOB["stop"] = True
    h.send_json({"ok": True})


# ------------------------------------------------------------- Übersicht ----

def r_stats(h, p):
    import trash

    c = db()
    vis = "dup_of IS NULL AND raw_of IS NULL AND COALESCE(hidden,0)=0" + (" AND COALESCE(priv_eff,0)=0" if locked(h) else "")
    kinds = {}
    first = last = None
    for kind, n, size, lo, hi, fav, geo in c.execute(
            "SELECT kind, COUNT(*), COALESCE(SUM(size),0), MIN(taken), MAX(CASE WHEN taken <= date('now','+1 day') "
            "THEN taken END), SUM(fav=1 OR rating>=4), SUM(lat IS NOT NULL) FROM items WHERE %s GROUP BY kind" % vis):
        kinds[kind] = {"count": n, "size": size, "fav": fav or 0, "geo": geo or 0}
        first = min(filter(None, (first, lo)), default=None)
        last = max(filter(None, (last, hi)), default=None)
    years = {}
    for y, kind, n in c.execute("SELECT substr(taken,1,4) y, kind, COUNT(*) FROM items WHERE taken IS NOT NULL "
                                "AND taken <= date('now','+1 day') AND %s GROUP BY y, kind" % vis):
        d = years.setdefault(y, {"year": y, "photo": 0, "video": 0})
        d["video" if kind == "video" else "photo"] += n
    places = c.execute("SELECT place, COUNT(*) n FROM items WHERE place IS NOT NULL AND %s GROUP BY place "
                       "ORDER BY n DESC LIMIT 8" % vis).fetchall()
    dups = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM items WHERE dup_of IS NOT NULL "
                     "AND COALESCE(hidden,0) != 2").fetchone()
    h.send_json({
        "photo": kinds.get("photo", {}).get("count", 0), "raw": kinds.get("raw", {}).get("count", 0),
        "video": kinds.get("video", {}).get("count", 0),
        "size": sum(k["size"] for k in kinds.values()),
        "video_size": kinds.get("video", {}).get("size", 0),
        "fav": sum(k["fav"] for k in kinds.values()), "geo": sum(k["geo"] for k in kinds.values()),
        "first": first, "last": last,
        "years": sorted(years.values(), key=lambda d: d["year"]),
        "places": places,
        "persons": c.execute("SELECT COUNT(*) FROM persons WHERE COALESCE(hidden,0)=0").fetchone()[0],
        "faces": c.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0],
        "albums": c.execute("SELECT COUNT(*) FROM albums").fetchone()[0],
        "events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "folders": c.execute("SELECT COUNT(DISTINCT folder) FROM items WHERE COALESCE(hidden,0) != 2").fetchone()[0],
        "dups": dups[0], "dups_size": dups[1],
        "hidden": c.execute("SELECT COUNT(*) FROM items WHERE hidden=1").fetchone()[0],
        "trash": trash.stats(c),
        "last_index": (c.execute("SELECT value FROM meta WHERE key='last_index'").fetchone() or [None])[0],
    })


# ---------------------------------------------------------------- Import ----

def r_import_status(h, p):
    import importer

    h.send_json({"job": importer.JOB.snapshot(), "accounts": importer.accounts(),
                 "sources": [{"source": r[0], "imported": r[1], "existing": r[2], "last": r[3]} for r in db().execute(
                     "SELECT source, SUM(status='imported'), SUM(status='exists'), MAX(at) FROM imports GROUP BY source")]})


def r_import_folder(h, p):
    import importer

    b = h.body()
    path = (b.get("path") or "").strip().strip('"')
    if not path or not os.path.exists(path):
        return h.send_json({"error": "Ordner oder Datei nicht gefunden"}, 400)
    if os.path.abspath(path).startswith(os.path.abspath(os.path.join(common.LIB_ROOT, importer.TARGET_ROOT))):
        return h.send_json({"error": "Dieser Ordner gehört schon zur Bibliothek"}, 400)
    ok = importer.start(importer.import_folder, path, b.get("label") or None, b.get("albums"), bool(b.get("dry")))
    h.send_json({"ok": ok} if ok else {"error": "Es läuft schon ein Import"})


def r_import_stop(h, p):
    import importer

    importer.JOB.stop = True
    h.send_json({"ok": True})


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".aif", ".aiff"}


def r_fs(h, p):
    """Ordner-Auswahl für den Import: Laufwerke/Ordner/ZIP-Dateien auflisten."""
    path = p.get("path") or ""
    roots = []
    if sys.platform == "win32":
        import ctypes
        import string

        # Laufwerksliste direkt von Windows – os.path.exists würde bei leeren Kartenlesern hängen
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        roots = [{"name": d + ":\\", "path": d + ":\\"} for i, d in enumerate(string.ascii_uppercase) if mask >> i & 1]
    else:
        roots = [{"name": "Volumes", "path": "/Volumes"}] if os.path.isdir("/Volumes") else [{"name": "/", "path": "/"}]
    home = os.path.expanduser("~")
    for name in ("Downloads", "Desktop", "Pictures", "Bilder", "Schreibtisch"):
        d = os.path.join(home, name)
        if os.path.isdir(d):
            roots.append({"name": name, "path": d})
    if not path:
        return h.send_json({"path": "", "parent": None, "entries": [], "roots": roots})
    entries = []
    try:
        for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if e.name.startswith(".") or e.name.startswith("$"):
                continue
            try:
                if e.is_dir():
                    entries.append({"name": e.name, "path": e.path, "type": "dir"})
                elif e.name.lower().endswith(".zip") and not p.get("audio"):
                    entries.append({"name": e.name, "path": e.path, "type": "zip", "size": e.stat().st_size})
                elif p.get("audio") and os.path.splitext(e.name)[1].lower() in AUDIO_EXTS:
                    entries.append({"name": e.name, "path": e.path, "type": "audio", "size": e.stat().st_size})
            except OSError:
                pass
    except OSError as ex:
        return h.send_json({"error": str(ex)}, 400)
    parent = os.path.dirname(path.rstrip("\\/"))
    h.send_json({"path": path, "parent": parent if parent and parent != path else None, "entries": entries[:500],
                 "roots": roots})


def r_icloud_login(h, p):
    import importer

    b = h.body()
    if not b.get("apple_id") or not b.get("password"):
        return h.send_json({"status": "error", "error": "Apple-ID und Passwort angeben"})
    try:
        h.send_json(importer.login(b["apple_id"].strip(), b["password"], (b.get("label") or "").strip()))
    except Exception as ex:
        h.send_json({"status": "error", "error": "Anmeldung nicht möglich: %s" % ex})


def r_icloud_code(h, p):
    import importer

    b = h.body()
    try:
        h.send_json(importer.submit_code(b.get("id", ""), b.get("code", "")))
    except Exception as ex:
        h.send_json({"status": "error", "error": str(ex)})


def r_icloud_resend(h, p):
    import importer

    b = h.body()
    try:
        h.send_json(importer.resend_code(b.get("id", ""), b.get("phone")))
    except Exception as ex:
        h.send_json({"status": "error", "error": str(ex)})


def r_icloud_run(h, p):
    import importer

    b = h.body()
    ok = importer.start(importer.run_icloud, b.get("id"), b.get("since") or None)
    h.send_json({"ok": ok} if ok else {"error": "Es läuft schon ein Import"})


def r_icloud_remove(h, p):
    import importer

    importer.remove_account(h.body().get("id", ""))
    h.send_json({"ok": True})


# ------------------------------------------------- Diashow / Teilen ----

def r_screen(h, p, iid):
    """Bild für Bildschirm/Diashow: Original wenn der Browser es kann, sonst berechnete Ansicht."""
    row = db().execute("SELECT path, ext, size, rotfix, kind FROM items WHERE id=?", (int(iid),)).fetchone()
    if not row:
        return h.send_error(404)
    if is_blocked(h, iid):
        return h.send_bytes(privacy.lock_image(), "image/jpeg", cache=0)
    if row[4] == "photo" and row[1] in common.BROWSER_IMAGE_EXT and (row[2] or 0) < 25_000_000 and not row[3]:
        return h.send_file(to_abs(row[0]))
    h.send_bytes(preview_bytes(int(iid)), "image/jpeg")


def r_music(h, p):
    import share
    import video

    music = share.music_list()
    if p.get("dur"):
        for m in music:
            m["duration"] = video.audio_duration(m["path"])
    h.send_json({"music": music, "dirs": share.music_dirs()})


def r_music_import(h, p):
    """Audiodatei vom Rechner in den Musik-Ordner übernehmen (Videoschnitt)."""
    import video

    try:
        dest = video.import_music(h.body().get("path") or "")
    except (RuntimeError, OSError) as ex:
        return h.send_json({"error": str(ex)}, 400)
    h.send_json({"ok": True, "path": dest, "name": os.path.splitext(os.path.basename(dest))[0],
                 "duration": video.audio_duration(dest)})


def r_music_file(h, p):
    import share

    f = p.get("f", "")
    if not share.music_allowed(f):
        return h.send_error(403)
    h.send_file(f)


def r_share_status(h, p):
    import share

    d = share.JOB.snapshot()
    d["result"] = getattr(share.JOB, "result", None)
    d["platform"] = sys.platform
    d["dest"] = share.default_dest()
    h.send_json(d)


def r_export(h, p):
    import share

    b = h.body()
    ids = _visible_ids(h, _ids(b))
    if not ids:
        return h.send_json({"error": "Keine (freigegebenen) Fotos ausgewählt"}, 400)
    ok = share.start(share.export, ids, b.get("size", "small"), bool(b.get("zip")), b.get("name"),
                     None, bool(b.get("apple")) and sys.platform == "darwin")
    h.send_json({"ok": ok} if ok else {"error": "Es läuft schon ein Export"})


def r_slideshow_video(h, p):
    import share

    b = h.body()
    ids = _visible_ids(h, _ids(b))
    if not ids:
        return h.send_json({"error": "Keine (freigegebenen) Fotos ausgewählt"}, 400)
    ok = share.start(share.slideshow_video, ids, float(b.get("seconds", 5)), bool(b.get("kenburns", True)),
                     b.get("music") or [], b.get("name"), bool(b.get("shuffle")), bool(b.get("tv")))
    h.send_json({"ok": ok} if ok else {"error": "Es läuft schon ein Export"})


def r_share_stop(h, p):
    import share

    share.JOB.stop = True
    h.send_json({"ok": True})


# ------------------------------------------------------------- Privat ----

def r_private_status(h, p):
    h.send_json({"password": privacy.has_password(db()), "unlocked": not locked(h)})


def r_private_password(h, p):
    b = h.body()
    new = b.get("new") or ""
    if len(new) < 4:
        return h.send_json({"error": "Das Passwort braucht mindestens 4 Zeichen"}, 400)
    if not privacy.set_password(db(), new, b.get("old")):
        return h.send_json({"error": "Das bisherige Passwort stimmt nicht"}, 403)
    privacy.lock_all()
    token = privacy.unlock(db(), new)
    h._extra_headers.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (privacy.COOKIE, token)))
    h.send_json({"ok": True})


def r_private_unlock(h, p):
    token = privacy.unlock(db(), h.body().get("password") or "")
    if not token:
        return h.send_json({"error": "Passwort falsch"}, 403)
    h._extra_headers.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (privacy.COOKIE, token)))
    h.send_json({"ok": True})


def r_private_lock(h, p):
    privacy.lock_all()
    h._extra_headers.append(("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % privacy.COOKIE))
    h.send_json({"ok": True})


def r_items_private(h, p):
    b = h.body()
    ids = _ids(b)
    c = db()
    if b.get("private") and not privacy.has_password(c):
        return h.send_json({"error": "Bitte zuerst ein Passwort für Privates festlegen"}, 400)
    if not b.get("private") and locked(h):
        return h.send_json({"error": "Zum Aufheben bitte erst entsperren"}, 403)
    c.executemany("UPDATE items SET private=? WHERE id=?", [(1 if b.get("private") else 0, i) for i in ids])
    c.commit()
    privacy.refresh(c)
    h.send_json({"ok": True})


# ------------------------------------------------- Handy, Frame, Fernseher ----

def r_phone(h, p):
    import lan

    ids = _visible_ids(h, _ids(h.body()))
    if not ids:
        return h.send_json({"error": "Keine (freigegebenen) Fotos ausgewählt"}, 400)
    try:
        token = lan.create(ids, "phone", 15)
        url = "%s/p/%s/" % (lan.base_url(), token)
    except Exception as ex:
        return h.send_json({"error": str(ex)}, 500)
    h.send_json({"url": url, "svg": lan.qr_svg(url), "count": len(ids), "minutes": 15})


def r_tv_status(h, p):
    import dlna
    import frame

    d = {"frame": frame.status(), "tv": dlna.status(), "frame_job": frame.JOB.snapshot()}
    h.send_json(d)


def r_tv_mirror(h, p):
    """Windows-Fenster „Verbinden/Übertragen“ öffnen (wie Win+K) – dort den Fernseher anklicken."""
    h.body()
    if sys.platform != "win32":
        return h.send_json({"error": "Am Mac: Kontrollzentrum › Bildschirmsynchronisierung (nur AirPlay-fähige Fernseher)"}, 400)
    try:
        os.startfile("ms-settings-connectabledevices:devicediscovery")
    except OSError as ex:
        return h.send_json({"error": str(ex)[:200]}, 400)
    h.send_json({"ok": True})


def r_dms(h, p):
    """Medienserver fürs Heimnetz (Fernbedienung des Fernsehers) ein-/ausschalten."""
    import dms

    if h.command == "POST":
        dms.set_enabled(bool(h.body().get("enabled")))
    h.send_json(dms.status())


def r_tv_discover(h, p):
    import dlna
    import frame

    found = {}
    threads = [threading.Thread(target=lambda: found.__setitem__("tv", dlna.discover())),
               threading.Thread(target=lambda: found.__setitem__("frame", frame.discover()))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    renderers = found.get("tv", [])
    # Samsung-Fernseher, die nur über den Rückfall gefunden wurden: Abspieldienst direkt abfragen
    for f in found.get("frame", []):
        if not any(r.get("ip") == f["ip"] for r in renderers):
            try:
                d = dlna._describe("http://%s:9197/dmr" % f["ip"])
                if d:
                    renderers.append(d)
            except Exception:
                pass
    h.send_json({"renderers": renderers, "frames": found.get("frame", [])})


def r_tv_manual(h, p):
    """Fernseher per IP-Adresse einrichten (falls die Suche nichts findet)."""
    import dlna
    import frame

    ip = (h.body().get("ip") or "").strip()
    out = {"ip": ip}
    try:
        info = frame.device_info(ip)
        out["frame"] = info if info.get("frame") else None
        out["name"] = info.get("name")
    except Exception:
        out["frame"] = None
    for loc in ("http://%s:9197/dmr" % ip, "http://%s:7676/smp_15_" % ip):
        try:
            d = dlna._describe(loc)
            if d:
                out["renderer"] = d
                break
        except Exception:
            continue
    if not out.get("frame") and not out.get("renderer"):
        return h.send_json({"error": "Unter %s antwortet kein Fernseher – ist er eingeschaltet?" % ip}, 404)
    h.send_json(out)


def r_tv_select(h, p):
    import dlna
    import frame

    b = h.body()
    if b.get("renderer"):
        dlna.set_renderer(b["renderer"])
    if b.get("frame"):
        frame.set_device(b["frame"]["ip"], b["frame"].get("name"))
    h.send_json({"ok": True})


def r_frame_pair(h, p):
    import frame

    try:
        h.send_json(frame.pair())
    except Exception as ex:
        h.send_json({"ok": False, "error": "Keine Verbindung: %s" % ex})


def r_frame_send(h, p):
    import frame

    b = h.body()
    ids = _visible_ids(h, _ids(b))
    if not ids:
        return h.send_json({"error": "Keine (freigegebenen) Fotos ausgewählt"}, 400)
    ok = frame.start(frame.send, ids, int(b.get("slideshow") or 0))
    h.send_json({"ok": ok} if ok else {"error": "Es wird gerade schon etwas an den Frame gesendet"})


def r_frame_cleanup(h, p):
    import frame

    try:
        h.send_json({"ok": True, "deleted": frame.cleanup()})
    except Exception as ex:
        h.send_json({"error": str(ex)}, 500)


def r_tv_show(h, p):
    import dlna

    b = h.body()
    ids = _visible_ids(h, _ids(b))
    if not ids:
        return h.send_json({"error": "Keine (freigegebenen) Fotos ausgewählt"}, 400)
    try:
        if b.get("mode", "direct") == "seamless" and len(ids) > 1:
            dlna.start_seamless(ids, float(b.get("seconds", 6)), bool(b.get("kenburns")), b.get("music") or [],
                                bool(b.get("shuffle")), b.get("title") or "Diashow", b.get("videos", True))
        else:
            dlna.start_show(ids, b.get("seconds", 6), bool(b.get("shuffle")), b.get("videos", True),
                            b.get("title") or "Diashow", loop=b.get("loop", len(ids) > 1))
    except Exception as ex:
        return h.send_json({"error": str(ex)}, 400)
    h.send_json({"ok": True})


def r_tv_one(h, p):
    """Mitlauf-Modus im Betrachter: aktuelles Foto auf den Fernseher, Nachbarn vorbereiten."""
    import dlna

    b = h.body()
    iid = int(b.get("id", 0))
    if is_blocked(h, iid):
        return h.send_json({"error": "Privat – erst entsperren"}, 403)
    pre = _visible_ids(h, [int(x) for x in b.get("prefetch", [])][:4])
    try:
        dlna.show_one(iid, pre)
    except Exception as ex:
        return h.send_json({"error": str(ex)[:200]}, 400)
    h.send_json({"ok": True})


def r_tv_follow_end(h, p):
    import dlna

    dlna.stop_follow(stop=bool(h.body().get("stop")))
    h.send_json({"ok": True})


def r_tv_control(h, p):
    import dlna

    dlna.control(h.body().get("cmd", ""))
    h.send_json({"ok": True})


# ------------------------------------------------- Videos für den Browser ----

def r_video_prepare(h, p, iid):
    import videoconv

    if is_blocked(h, iid):
        return h.send_error(403)
    h.send_json(videoconv.prepare(int(iid)))


def r_video_status(h, p, iid):
    import videoconv

    h.send_json(videoconv.status(int(iid)))


def r_video(h, p, iid):
    import videoconv

    if is_blocked(h, iid):
        return h.send_error(403)
    path = videoconv.cached_path(int(iid))
    if not os.path.exists(path):
        return h.send_error(404)
    h.send_file(path, "video/mp4")


def r_recompute(h, p):
    h.send_json({"ok": recompute_faces_bg()})


def r_library(h, p):
    """Fotoordner festlegen (nur solange der Katalog leer ist), danach Neustart."""
    path = (h.body().get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return h.send_json({"error": "Ordner nicht gefunden"}, 400)
    if os.path.abspath(path) == os.path.abspath(common.LIB_ROOT):
        return h.send_json({"ok": True, "restart": False})
    if db().execute("SELECT 1 FROM items LIMIT 1").fetchone():
        return h.send_json({"error": "Der Katalog enthält schon Fotos aus dem bisherigen Ordner. Zum Wechseln "
                                     "FotoArchiv beenden und den Ordner „data“ im FotoArchiv-Ordner löschen "
                                     "(alles Eigene geht dabei verloren)."}, 409)
    cfg = common.load_config()
    cfg["library_root"] = common.root_setting(path)
    cfg["folders"] = None
    common.save_config(cfg)
    h.send_json({"ok": True, "restart": True})
    threading.Thread(target=_restart, daemon=True).start()


def _restart():
    """Sich selbst neu starten (eigenes Fenster), damit der neue Fotoordner überall gilt."""
    time.sleep(0.5)
    args = [sys.executable, os.path.abspath(__file__), "--no-browser", "--restart"]
    if sys.platform == "win32":
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=common.BASE_DIR)
    else:
        subprocess.Popen(args, start_new_session=True, cwd=common.BASE_DIR)
    os._exit(0)


def r_edit_src(h, p, iid):
    """Unbearbeitetes Foto (richtig gedreht, max. 1600 px) als Arbeitsbild für den Editor im Browser."""
    import io

    import media

    if is_blocked(h, iid):
        return h.send_error(403)
    row = db().execute("SELECT path, kind, userrot FROM items WHERE id=?", (int(iid),)).fetchone()
    if not row or row[1] == "video":
        return h.send_error(404)
    im = media.open_image(to_abs(row[0]), row[1], max_side=1600, userrot=row[2] or 0)[0].convert("RGB")
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    h.send_bytes(buf.getvalue(), "image/jpeg", cache=0)


def r_edit_save(h, p, iid):
    """Bearbeitung speichern (edit = null setzt aufs Original zurück)."""
    import edit as editmod

    iid = int(iid)
    if is_blocked(h, iid):
        return h.send_json({"error": "Privat – bitte entsperren"}, 403)
    e = editmod.clean(h.body().get("edit"))
    c = db()
    c.execute("UPDATE items SET edit=? WHERE id=?", (json.dumps(e) if e else None, iid))
    c.commit()
    THUMBS.delete([iid])
    PREVIEWS.delete([iid])
    try:
        import lan

        lan._jpeg_cache.clear()
    except Exception:
        pass
    thumb_bytes(iid)
    h.send_json({"ok": True, "edit": e})


# ---------------------------------------------------------- Videoschnitt ----

def _vproject(pid):
    row = db().execute("SELECT id, name, data, created, updated FROM vprojects WHERE id=?", (int(pid),)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "data": json.loads(row[2] or "{}"), "created": row[3], "updated": row[4]}


def r_vprojects(h, p):
    import video

    out = []
    for pid, name, data, upd in db().execute("SELECT id, name, data, updated FROM vprojects ORDER BY updated DESC"):
        d = json.loads(data or "{}")
        d.setdefault("clips", [])
        d.setdefault("music", [])
        try:
            total = video.timeline(video.clean(d, db()))[1]
        except Exception:
            total = 0
        out.append({"id": pid, "name": name, "updated": upd, "clips": len(d["clips"]), "seconds": total,
                    "cover": next((c["item"] for c in d["clips"]), None)})
    h.send_json({"projects": out, "jobs": video.jobs()})


def r_vproject(h, p, pid):
    pr = _vproject(pid)
    if not pr:
        return h.send_error(404)
    import video

    pr["data"] = video.clean(pr["data"], db())
    pr["items"] = {}
    for c in pr["data"]["clips"]:
        if c["item"] not in pr["items"]:
            r = db().execute("SELECT id, name, ext, kind, duration, width, height, userrot, COALESCE(priv_eff,0) "
                             "FROM items WHERE id=?", (c["item"],)).fetchone()
            if r:
                pr["items"][r[0]] = dict(zip(("id", "name", "ext", "kind", "duration", "width", "height", "userrot",
                                              "private"), r))
    pr["jobs"] = video.jobs(int(pid), 20)
    h.send_json(pr)


def r_vproject_create(h, p):
    import video

    b = h.body()
    c = db()
    now = video._now()
    clips = [x for x in (video.new_clip(c, i) for i in _visible_ids(h, _ids(b))) if x]
    pid = c.execute("INSERT INTO vprojects(name, data, created, updated) VALUES(?,?,?,?)",
                    ((b.get("name") or "Neues Video").strip()[:120], json.dumps({"clips": clips, "music": []}), now,
                     now)).lastrowid
    c.commit()
    h.send_json({"ok": True, "id": pid})


def r_vproject_save(h, p, pid):
    import video

    b = h.body()
    c = db()
    if not _vproject(pid):
        return h.send_error(404)
    if "data" in b:
        c.execute("UPDATE vprojects SET data=?, updated=? WHERE id=?",
                  (json.dumps(video.clean(b["data"], c)), video._now(), int(pid)))
    if (b.get("name") or "").strip():
        c.execute("UPDATE vprojects SET name=?, updated=? WHERE id=?", (b["name"].strip()[:120], video._now(), int(pid)))
    c.commit()
    h.send_json({"ok": True})


def r_vproject_add(h, p, pid):
    import video

    pr = _vproject(pid)
    if not pr:
        return h.send_error(404)
    c = db()
    d = pr["data"]
    d.setdefault("clips", [])
    added = [x for x in (video.new_clip(c, i) for i in _visible_ids(h, _ids(h.body()))) if x]
    d["clips"] += added
    c.execute("UPDATE vprojects SET data=?, updated=? WHERE id=?", (json.dumps(video.clean(d, c)), video._now(), int(pid)))
    c.commit()
    h.send_json({"ok": True, "added": len(added)})


def r_vproject_delete(h, p, pid):
    db().execute("DELETE FROM vprojects WHERE id=?", (int(pid),))
    db().commit()
    h.send_json({"ok": True})


def r_vproject_render(h, p, pid):
    import video

    try:
        jid = video.enqueue(int(pid))
    except RuntimeError as ex:
        return h.send_json({"error": str(ex)}, 400)
    h.send_json({"ok": True, "job": jid})


def r_vproject_preview(h, p, pid):
    """Kurzen Ausschnitt ab t (Sequenzzeit) klein rendern – mit Übergängen und Ton."""
    import video

    b = h.body()
    d = video.clean(b.get("data") if b.get("data") else (_vproject(pid) or {}).get("data"), db())
    try:
        name, t0 = video.preview(d, float(b.get("t") or 0), float(b.get("seconds") or 8))
    except RuntimeError as ex:
        return h.send_json({"error": str(ex)}, 400)
    h.send_json({"ok": True, "url": "/vpreview/" + name, "t0": t0})


def r_vpreview(h, p, name):
    import video

    path = video.preview_path(name)
    if not path:
        return h.send_error(404)
    h.send_file(path, "video/mp4")


def r_vjobs(h, p):
    import video

    h.send_json(video.jobs(int(p["project"]) if p.get("project") else None))


def r_vjob_cancel(h, p, jid):
    import video

    video.cancel(int(jid))
    h.send_json({"ok": True})


def _vjob_out(jid):
    row = db().execute("SELECT out, status FROM vrenders WHERE id=?", (int(jid),)).fetchone()
    return row[0] if row and row[1] == "fertig" and row[0] and os.path.exists(row[0]) else None


def r_vjob_file(h, p, jid):
    out = _vjob_out(jid)
    if not out:
        return h.send_error(404)
    h.send_file(out, "video/mp4")


def r_vjob_reveal(h, p, jid):
    import share

    out = _vjob_out(jid)
    if not out:
        return h.send_json({"error": "Datei nicht mehr da"}, 404)
    share.reveal(os.path.dirname(out))
    h.send_json({"ok": True})


def r_quit(h, p):
    h.send_json({"ok": True})
    threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()


ROUTES = [
    ("GET", r"/api/ping", r_ping),
    ("GET", r"/api/status", r_status),
    ("POST", r"/api/config", r_config),
    ("POST", r"/api/index/start", r_index_start),
    ("POST", r"/api/index/stop", r_index_stop),
    ("GET", r"/api/query", r_query),
    ("GET", r"/api/years", r_years),
    ("GET", r"/api/calendar", r_calendar),
    ("GET", r"/api/albums", r_albums),
    ("GET", r"/api/tags", r_tags),
    ("POST", r"/api/video/(\d+)/prepare", r_video_prepare),
    ("GET", r"/api/video/(\d+)/status", r_video_status),
    ("GET", r"/video/(\d+)", r_video),
    ("POST", r"/api/phone", r_phone),
    ("GET", r"/api/tv/status", r_tv_status),
    ("GET", r"/api/dms", r_dms),
    ("POST", r"/api/tv/mirror", r_tv_mirror),
    ("POST", r"/api/dms", r_dms),
    ("POST", r"/api/tv/discover", r_tv_discover),
    ("POST", r"/api/tv/select", r_tv_select),
    ("POST", r"/api/tv/manual", r_tv_manual),
    ("POST", r"/api/tv/show", r_tv_show),
    ("POST", r"/api/tv/control", r_tv_control),
    ("POST", r"/api/tv/one", r_tv_one),
    ("POST", r"/api/tv/follow_end", r_tv_follow_end),
    ("POST", r"/api/frame/pair", r_frame_pair),
    ("POST", r"/api/frame/send", r_frame_send),
    ("POST", r"/api/frame/cleanup", r_frame_cleanup),
    ("GET", r"/api/private/status", r_private_status),
    ("POST", r"/api/private/password", r_private_password),
    ("POST", r"/api/private/unlock", r_private_unlock),
    ("POST", r"/api/private/lock", r_private_lock),
    ("POST", r"/api/items/private", r_items_private),
    ("GET", r"/screen/(\d+)", r_screen),
    ("GET", r"/api/music", r_music),
    ("GET", r"/music", r_music_file),
    ("GET", r"/api/share/status", r_share_status),
    ("POST", r"/api/export", r_export),
    ("POST", r"/api/slideshow/video", r_slideshow_video),
    ("POST", r"/api/share/stop", r_share_stop),
    ("GET", r"/api/import/status", r_import_status),
    ("POST", r"/api/import/folder", r_import_folder),
    ("POST", r"/api/import/stop", r_import_stop),
    ("GET", r"/api/fs", r_fs),
    ("POST", r"/api/icloud/login", r_icloud_login),
    ("POST", r"/api/icloud/code", r_icloud_code),
    ("POST", r"/api/icloud/run", r_icloud_run),
    ("POST", r"/api/icloud/resend", r_icloud_resend),
    ("POST", r"/api/icloud/remove", r_icloud_remove),
    ("POST", r"/api/items/fav", r_items_fav),
    ("POST", r"/api/items/hide", r_items_hide),
    ("POST", r"/api/items/delete", r_items_delete),
    ("POST", r"/api/folders/delete", r_folder_delete),
    ("GET", r"/api/trash", r_trash_status),
    ("POST", r"/api/trash/restore", r_trash_restore),
    ("POST", r"/api/trash/purge", r_trash_purge),
    ("GET", r"/api/stats", r_stats),
    ("GET", r"/api/dups", r_dups),
    ("POST", r"/api/dups/resolve", r_dups_resolve),
    ("POST", r"/api/dups/keep", r_dups_keep),
    ("POST", r"/api/dups/all", r_dups_all),
    ("GET", r"/api/dups/job", r_dups_job),
    ("POST", r"/api/dups/stop", r_dups_stop),
    ("POST", r"/api/items/rotate", r_items_rotate),
    ("POST", r"/api/items/date", r_items_date),
    ("POST", r"/api/items/tags", r_items_tags),
    ("POST", r"/api/items/person", r_items_person),
    ("POST", r"/api/albums", r_album_create),
    ("POST", r"/api/albums/(\d+)", r_album_update),
    ("POST", r"/api/albums/(\d+)/delete", r_album_delete),
    ("POST", r"/api/albums/(\d+)/add", r_album_add),
    ("POST", r"/api/albums/(\d+)/remove", r_album_remove),
    ("POST", r"/api/events", r_event_create),
    ("POST", r"/api/events/(\d+)", r_event_update),
    ("POST", r"/api/events/(\d+)/delete", r_event_delete),
    ("GET", r"/api/map", r_map),
    ("GET", r"/api/item/(\d+)", r_item),
    ("POST", r"/api/item/(\d+)/fav", r_fav),
    ("POST", r"/api/item/(\d+)/rotate", r_rotate),
    ("POST", r"/api/item/(\d+)/(open|reveal)", r_open),
    ("GET", r"/thumb/(\d+)", r_thumb),
    ("GET", r"/preview/(\d+)", r_preview),
    ("GET", r"/original/(\d+)", r_original),
    ("GET", r"/face/(\d+)", r_face_img),
    ("GET", r"/api/folders", r_folders),
    ("GET", r"/api/persons", r_persons),
    ("POST", r"/api/persons", r_person_create),
    ("POST", r"/api/persons/(\d+)", r_person_update),
    ("POST", r"/api/persons/(\d+)/merge", r_person_merge),
    ("POST", r"/api/persons/(\d+)/delete", r_person_delete),
    ("GET", r"/api/persons/(\d+)/faces", r_person_faces),
    ("POST", r"/api/faces/assign", r_faces_assign),
    ("POST", r"/api/faces/reject", r_faces_reject),
    ("POST", r"/api/faces/ignore", r_faces_ignore),
    ("GET", r"/api/clusters", r_clusters),
    ("GET", r"/api/clusters/(\d+)", r_cluster),
    ("POST", r"/api/faces/recompute", r_recompute),
    ("POST", r"/api/quit", r_quit),
    ("GET", r"/edit-src/(\d+)", r_edit_src),
    ("POST", r"/api/item/(\d+)/edit", r_edit_save),
    ("POST", r"/api/library", r_library),
    ("GET", r"/api/vprojects", r_vprojects),
    ("POST", r"/api/vprojects", r_vproject_create),
    ("GET", r"/api/vprojects/(\d+)", r_vproject),
    ("POST", r"/api/vprojects/(\d+)", r_vproject_save),
    ("POST", r"/api/vprojects/(\d+)/add", r_vproject_add),
    ("POST", r"/api/vprojects/(\d+)/delete", r_vproject_delete),
    ("POST", r"/api/vprojects/(\d+)/render", r_vproject_render),
    ("GET", r"/api/vjobs", r_vjobs),
    ("POST", r"/api/vprojects/(\d+)/preview", r_vproject_preview),
    ("GET", r"/vpreview/(v[0-9a-f]+\.mp4)", r_vpreview),
    ("POST", r"/api/music/import", r_music_import),
    ("POST", r"/api/vjobs/(\d+)/cancel", r_vjob_cancel),
    ("POST", r"/api/vjobs/(\d+)/reveal", r_vjob_reveal),
    ("GET", r"/vjob/(\d+)\.mp4", r_vjob_file),
]


class ExclusiveServer(ThreadingHTTPServer):
    """Port exklusiv belegen. Unter Windows erlaubt SO_REUSEADDR sonst einen zweiten Server auf
    demselben Port, und Anfragen landen zufällig bei einem alten (evtl. hängenden) Prozess."""
    allow_reuse_address = False

    def server_bind(self):
        import socket

        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
            return json.loads(r.read()).get("app") == "FotoArchiv"
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    if "--setup-only" in args:
        print("Einrichtung fertig.")
        return
    if "--index" in args:
        import indexer

        indexer.run()
        print(indexer.PROGRESS.snapshot())
        return
    connect().close()
    port = PORT
    if "--restart" in args:  # der alte Prozess gibt den Port gleich frei
        for _ in range(40):
            if not already_running(PORT):
                break
            time.sleep(0.25)
    for port in range(PORT, PORT + 20):
        if already_running(port):
            print("FotoArchiv läuft bereits: http://127.0.0.1:%d" % port)
            webbrowser.open("http://127.0.0.1:%d" % port)
            return
        try:
            srv = ExclusiveServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        print("Kein freier Port gefunden.")
        return
    srv.daemon_threads = True
    url = "http://127.0.0.1:%d" % port
    print("=" * 60)
    print(" FotoArchiv läuft:  " + url)
    print(" Bibliothek:        " + common.LIB_ROOT)
    print(" Zum Beenden dieses Fenster schließen (oder Strg+C).")
    print("=" * 60)
    if "--no-browser" not in args:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def resume():
        import indexer

        try:
            if indexer.needs_resume():
                print(" Einlesen war noch nicht fertig – wird fortgesetzt.")
                indexer.start_background()
        except Exception as ex:
            print(" Fortsetzen nicht möglich:", ex)
        try:
            import dms

            if dms.enabled():
                dms.start()
                print(" Medienserver für den Fernseher ist eingeschaltet.")
        except Exception as ex:
            print(" Medienserver nicht gestartet:", ex)
        try:
            import videoconv

            videoconv.remove_partial()
        except Exception:
            pass
        try:
            import video

            video.recover()
        except Exception:
            pass
        try:
            import importer

            what = importer.resume_pending()
            if what:
                print(" Unterbrochener Import wird fortgesetzt:", what)
        except Exception as ex:
            print(" Import-Fortsetzung nicht möglich:", ex)
    threading.Timer(5, resume).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
