"""Archiv-Ansicht im S3-Bucket: eine index.html, mit der sich die Sicherung im Browser ansehen lässt –
Zeitleiste nach Jahr/Monat, Alben, große Ansicht, Videos, Original herunterladen. Ohne FotoArchiv nutzbar.

Ablage unter <Präfix>FotoArchiv-Ansicht/:
  index.html        Seite (alles in einer Datei; Ziel-Adresse eingebettet, keine Schlüssel)
  t/<id>.jpg        Vorschaubild (aus thumbs.db)
  p/<id>.jpg        große Ansicht für Formate, die Browser nicht zeigen (HEIC, RAW, PSD, TIFF) oder bearbeitete Fotos
  data/index.js     Jahre, Alben;  data/<Jahr>.js  Einträge des Jahres  (als <script> geladen – braucht kein CORS)
JPEG/PNG/GIF/WebP und MP4/MOV kommen direkt aus der Sicherung (<Präfix><Pfad>), ohne Kopie.
Der Bucket bleibt privat: die Seite fragt einmal nach Zugangsschlüssel + geheimem Schlüssel (bleiben nur im
Browser, localStorage) und signiert jeden Abruf selbst (Signatur V4 oder V2, WebCrypto).
Private, ausgeblendete, gelöschte, Duplikate und einsortierte Dokumente kommen nie hinein.
Fortsetzbar: Tabelle view_files merkt je Ziel Schlüssel + Stand (Verarbeitung/Drehung/Bearbeitung).
"""
import datetime
import hashlib
import json
import os
import threading

import backup
import common
import s3
from common import connect, to_abs

VIEW_DIR = "FotoArchiv-Ansicht/"
BROWSER_IMG = {"jpg", "jpeg", "png", "gif", "webp"}
BROWSER_VID = {"mp4", "m4v", "mov"}
PREVIEW_SIDE = 1920
SCHEMA = ("CREATE TABLE IF NOT EXISTS view_files (target TEXT, key TEXT, sig TEXT, PRIMARY KEY (target, key)) WITHOUT ROWID;"
          "CREATE TABLE IF NOT EXISTS view_priv (target TEXT, item INTEGER, token TEXT, PRIMARY KEY (target, item)) WITHOUT ROWID;")
JOB = backup.JOB


def _items(con):
    return con.execute(
        "SELECT id, path, kind, ext, taken, width, height, COALESCE(userrot,0), edit, proc_ver FROM items "
        "WHERE COALESCE(hidden,0) IN (0,3) AND dup_of IS NULL AND raw_of IS NULL AND COALESCE(priv_eff,0)=0 "
        "AND proc_ver > 0 ORDER BY taken, name").fetchall()


def _extra(con):
    """Zusatzangaben je Eintrag für Filter, Suche, Karte und Info: {id: (fav, camera, place, lat, lon, dauer, pano, stichworte)}"""
    return {r[0]: r[1:] for r in con.execute(
        "SELECT id, (COALESCE(fav,0)=1 OR COALESCE(rating,0)>=4), camera, place, lat, lon, duration, pano, "
        "TRIM(COALESCE(keywords,'') || ' ' || COALESCE(usertags,'') || ' ' || COALESCE(caption,'')) FROM items "
        "WHERE COALESCE(hidden,0) IN (0,3) AND dup_of IS NULL AND COALESCE(priv_eff,0)=0")}


def _cam_label(raw):
    """Kameraname wie in FotoArchiv (server.cam_label): „Apple iPhone 11“ → „iPhone 11“, ohne Firmware-Anhang."""
    import re

    s = re.sub(r"\s+", " ", (raw or "").replace("\x00", "").strip())
    s = re.sub(r"^Apple (iPhone|iPad|iPod)", r"\1", s)
    s = re.sub(r"^CASIO COMPUTER CO\.,\s*LTD\.?\s*", "CASIO ", s)
    return re.sub(r"\s+[A-Z]{0,3}\d+(\.\d+){2,}$", "", s)


def _put_js(c, key, text):
    """Datendatei gepackt ablegen (Content-Encoding: gzip – der Browser entpackt selbst)."""
    import gzip

    c.put(key, gzip.compress(text.encode("utf-8"), 6), {"Content-Type": "application/javascript; charset=utf-8",
                                                         "Content-Encoding": "gzip", "Cache-Control": "no-cache"})


def _sig(proc_ver, userrot, edit):
    return "%s|%s|%s" % (proc_ver, userrot, hashlib.md5((edit or "").encode()).hexdigest()[:10])


def _view_mode(kind, ext, userrot, edit):
    """o = Original direkt, p = eigene große Ansicht, t = nur Vorschaubild (Video, das Browser nicht spielen)."""
    if kind == "video":
        return "o" if ext in BROWSER_VID else "t"
    if ext in BROWSER_IMG and not userrot and not edit:
        return "o"
    return "p"


def _make_preview(args):
    iid, path, kind, userrot, edit = args
    import media

    try:
        return iid, media.make_preview(path, kind, PREVIEW_SIDE, userrot, json.loads(edit) if edit else None)
    except Exception:
        return iid, None


def _put(c, key, data, ctype, cache="max-age=31536000"):
    c.put(key, data, {"Content-Type": ctype, "Cache-Control": cache})


def run(tid, cfg=None):
    """Ansicht für ein Ziel aktualisieren (läuft im Hintergrund über backup.start)."""
    import multiprocessing as mp

    t = backup.target(tid)
    c = backup.client(t)
    pfx = backup.prefix_of(t)
    base = pfx + VIEW_DIR
    con = connect()
    con.executescript(SCHEMA)
    done = {k: s for k, s in con.execute("SELECT key, sig FROM view_files WHERE target=?", (tid,))}
    rows = _items(con)
    lock = threading.Lock()

    def mark(key, sig):
        with lock:
            done[key] = sig
            con.execute("INSERT OR REPLACE INTO view_files VALUES (?,?,?)", (tid, key, sig))

    # 1) Vorschaubilder (aus thumbs.db) – viele kleine Dateien, mehrere Threads
    JOB.phase = "Archiv-Ansicht: Vorschaubilder"
    todo = [r for r in rows if done.get(base + "t/%d.jpg" % r[0]) != _sig(r[9], r[7], r[8])]
    JOB.files_total, JOB.files_done, JOB.bytes_total, JOB.bytes_done = len(todo), 0, len(todo) * 34000, 0
    queue = list(reversed(todo))
    qlock = threading.Lock()

    def worker():
        wc = backup.client(t)
        while not JOB.stop:
            with qlock:
                if not queue:
                    return
                r = queue.pop()
            blob = common.THUMBS.get(r[0])
            try:
                if blob:
                    _put(wc, base + "t/%d.jpg" % r[0], blob, "image/jpeg")
                    mark(base + "t/%d.jpg" % r[0], _sig(r[9], r[7], r[8]))
            except s3.S3Error as ex:
                JOB.err("Vorschau %d: %s" % (r[0], backup.explain(ex)))
            with JOB.lock:
                JOB.files_done += 1
                JOB.bytes_done += 34000
                if JOB.files_done % 500 == 0:
                    with lock:
                        con.commit()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(2, int((cfg or {}).get("workers") or 4) * 2))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    con.commit()
    if JOB.stop:
        return
    # 2) große Ansichten für HEIC/RAW/PSD/TIFF und bearbeitete/gedrehte Fotos – rechenintensiv, Prozess-Pool
    JOB.phase = "Archiv-Ansicht: große Ansichten (HEIC, RAW …)"
    need = [r for r in rows if _view_mode(r[2], r[3], r[7], r[8]) == "p"
            and done.get(base + "p/%d.jpg" % r[0]) != _sig(r[9], r[7], r[8])]
    JOB.files_total, JOB.files_done, JOB.bytes_total, JOB.bytes_done = len(need), 0, len(need) * 400000, 0
    if need:
        info = {r[0]: r for r in need}
        n = max(1, min(6, (os.cpu_count() or 2) - 2))
        with mp.get_context("spawn").Pool(n) as pool:
            args = [(r[0], to_abs(r[1]), r[2], r[7], r[8]) for r in need]
            for iid, data in pool.imap_unordered(_make_preview, args, chunksize=4):
                r = info[iid]
                try:
                    if data:
                        _put(c, base + "p/%d.jpg" % iid, data, "image/jpeg")
                        mark(base + "p/%d.jpg" % iid, _sig(r[9], r[7], r[8]))
                except s3.S3Error as ex:
                    JOB.err("Ansicht %d: %s" % (iid, backup.explain(ex)))
                JOB.files_done += 1
                JOB.bytes_done += 400000
                if JOB.files_done % 100 == 0:
                    con.commit()
                if JOB.stop:
                    pool.terminate()
                    break
        con.commit()
        if JOB.stop:
            return
    # 3) Daten (eine gepackte Datei – Filter, Suche, Kalender, Karte rechnet die Seite selbst), Seite
    JOB.phase = "Archiv-Ansicht: Daten und Seite"
    data = build_data(con, rows, {k for k in done if k.startswith(base + "p/")}, base)
    priv = _private(con, c, tid, base, done, mark)
    if JOB.stop:
        return
    out = dict(data, priv=priv) if priv else data
    _put_js(c, base + "data/all.js", "FA.all(%s);" % json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    _remember(con, tid, data)
    pano_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pano.js")
    if os.path.exists(pano_js):
        with open(pano_js, "rb") as f:
            _put(c, base + "pano.js", f.read(), "application/javascript; charset=utf-8", "no-cache")
    _put(c, base + "index.html", page(t).encode(), "text/html; charset=utf-8", "no-cache")
    con.close()
    JOB.message = "Archiv-Ansicht aktuell: %d Fotos und Videos." % data["total"]


# ------------------------------------------------------------ private Fotos ----
# Nur mit dem Schlüssel aus dem Privat-Passwort (privacy.VIEW_KEYS, beim Entsperren abgeleitet): Liste, Vorschaubilder und
# große Ansichten verschlüsselt (aesctr: AES-256-CTR + HMAC) unter Zufallsnamen x/<token>t.bin / x/<token>p.bin.
# Die Seite entschlüsselt nach Eingabe desselben Passworts (WebCrypto). Ohne Schlüssel bleibt der letzte Stand.

def _priv_items(con):
    return con.execute(
        "SELECT id, path, kind, ext, taken, width, height, COALESCE(userrot,0), edit, proc_ver FROM items "
        "WHERE COALESCE(hidden,0) IN (0,3) AND dup_of IS NULL AND raw_of IS NULL AND COALESCE(priv_eff,0)=1 "
        "AND proc_ver > 0 ORDER BY taken, name").fetchall()


def _priv_cache(tid):
    return os.path.join(common.DATA_DIR, "view_priv_%s.json" % tid)


def _private_payload(con, prows, tokens):
    """Klartext der privaten Liste (wird verschlüsselt): Einträge mit Ordner/Kamera/Ort als Text."""
    extra = {r[0]: r[1:] for r in con.execute(
        "SELECT id, (COALESCE(fav,0)=1 OR COALESCE(rating,0)>=4), camera, place, lat, lon, duration, pano, "
        "TRIM(COALESCE(keywords,'') || ' ' || COALESCE(usertags,'') || ' ' || COALESCE(caption,'')) FROM items "
        "WHERE COALESCE(priv_eff,0)=1")}
    recs, kw, pano = [], {}, {}
    ids = set()
    for iid, path, kind, ext, taken, w, h, userrot, edit, _pv in prows:
        fav, cam, place, lat, lon, dur, pinfo, words = extra.get(iid, (0, None, None, None, None, None, "", ""))
        folder, name = path.rsplit("/", 1) if "/" in path else ("", path)
        r = [iid, "v" if kind == "video" else "p", (taken or "")[:16], "x", folder, name, w or 0, h or 0,
             (1 if fav else 0) | (2 if pinfo else 0), _cam_label(cam) if cam else "", place or "", int(dur or 0)]
        if lat is not None and lon is not None and (abs(lat) > 1e-4 or abs(lon) > 1e-4):
            r += [round(lat, 5), round(lon, 5)]
        recs.append(r)
        ids.add(iid)
        if words:
            kw[iid] = words
        if pinfo:
            try:
                pano[iid] = json.loads(pinfo)
            except ValueError:
                pano[iid] = {}
    import privacy

    priv_alb = privacy.private_albums(con)
    albums, alb = [], {}
    for aid, name, parent, desc, own_cover in con.execute("SELECT id, name, parent, description, cover FROM albums"):
        members = [r[0] for r in con.execute("SELECT ai.item_id FROM album_items ai JOIN items i ON i.id=ai.item_id "
                                             "WHERE ai.album_id=? AND i.dup_of IS NULL AND COALESCE(i.hidden,0) IN (0,3) "
                                             "ORDER BY i.taken DESC", (aid,))]
        if aid in priv_alb:
            albums.append([aid, name, parent, members, own_cover or (max(members) if members else None), desc or ""])
        else:
            mine = [m for m in members if m in ids]
            if mine:
                alb[aid] = mine
    pers, pnames = {}, {}
    for pid, pname, item in con.execute("SELECT p.id, p.name, f.item_id FROM faces f JOIN persons p ON p.id=f.person_id "
                                        "WHERE COALESCE(p.hidden,0)=0 AND p.name<>''"):
        if item in ids:
            pers.setdefault(pid, set()).add(item)
            pnames[pid] = pname
    return {"recs": recs, "tok": {str(k): v for k, v in tokens.items() if k in ids}, "kw": kw, "pano": pano,
            "albums": albums, "alb": alb, "pers": {k: sorted(v) for k, v in pers.items()}, "pnames": pnames}


def _private(con, c, tid, base, done, mark):
    import base64
    import multiprocessing as mp
    import secrets

    import aesctr
    import privacy

    keys = privacy.VIEW_KEYS
    if not keys:  # nicht entsperrt: letzten verschlüsselten Stand weiterverwenden
        try:
            with open(_priv_cache(tid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    kfp = hashlib.sha1(keys[1]).hexdigest()[:8]
    prows = _priv_items(con)
    tokens = dict(con.execute("SELECT item, token FROM view_priv WHERE target=?", (tid,)).fetchall())
    for r in prows:
        if r[0] not in tokens:
            tokens[r[0]] = secrets.token_hex(12)
            con.execute("INSERT INTO view_priv VALUES (?,?,?)", (tid, r[0], tokens[r[0]]))
    con.commit()
    sig = lambda r: _sig(r[9], r[7], r[8]) + "|" + kfp  # noqa: E731
    enc = lambda data: aesctr.seal(keys, data)  # noqa: E731
    JOB.phase = "Archiv-Ansicht: private Fotos verschlüsseln"
    todo_t = [r for r in prows if done.get(base + "x/%st.bin" % tokens[r[0]]) != sig(r)]
    todo_p = [r for r in prows if r[2] == "photo" and done.get(base + "x/%sp.bin" % tokens[r[0]]) != sig(r)]
    JOB.files_total, JOB.files_done = len(todo_t) + len(todo_p), 0
    JOB.bytes_total, JOB.bytes_done = len(todo_t) * 34000 + len(todo_p) * 400000, 0
    for r in todo_t:
        if JOB.stop:
            return None
        blob = common.THUMBS.get(r[0])
        if blob:
            try:
                _put(c, base + "x/%st.bin" % tokens[r[0]], enc(blob), "application/octet-stream")
                mark(base + "x/%st.bin" % tokens[r[0]], sig(r))
            except s3.S3Error as ex:
                JOB.err("Privat %d: %s" % (r[0], backup.explain(ex)))
        JOB.files_done += 1
        JOB.bytes_done += 34000
    con.commit()
    if todo_p:
        info = {r[0]: r for r in todo_p}
        n = max(1, min(6, (os.cpu_count() or 2) - 2))
        with mp.get_context("spawn").Pool(n) as pool:
            for iid, data in pool.imap_unordered(_make_preview, [(r[0], to_abs(r[1]), r[2], r[7], r[8]) for r in todo_p],
                                                 chunksize=2):
                r = info[iid]
                try:
                    if data:
                        _put(c, base + "x/%sp.bin" % tokens[iid], enc(data), "application/octet-stream")
                        mark(base + "x/%sp.bin" % tokens[iid], sig(r))
                except s3.S3Error as ex:
                    JOB.err("Privat %d: %s" % (iid, backup.explain(ex)))
                JOB.files_done += 1
                JOB.bytes_done += 400000
                if JOB.stop:
                    pool.terminate()
                    return None
        con.commit()
    payload = json.dumps(_private_payload(con, prows, tokens), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    import gzip

    out = {"salt": privacy.VIEW_SALT, "iter": aesctr.ITER, "n": len(prows),
           "blob": base64.b64encode(enc(gzip.compress(payload, 6))).decode()}
    with open(_priv_cache(tid), "w", encoding="utf-8") as f:
        json.dump(out, f)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("view_pfp_" + tid, hashlib.sha1(payload).hexdigest()))
    con.commit()
    return out


def private_changed(tid):
    """Nur wenn entsperrt: hat sich an den privaten Fotos etwas geändert?"""
    import privacy

    if not privacy.VIEW_KEYS:
        return False
    con = connect()
    try:
        con.executescript(SCHEMA)
        tokens = dict(con.execute("SELECT item, token FROM view_priv WHERE target=?", (tid,)).fetchall())
        prows = _priv_items(con)
        if any(r[0] not in tokens for r in prows):
            return True
        payload = json.dumps(_private_payload(con, prows, tokens), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        old = (con.execute("SELECT value FROM meta WHERE key=?", ("view_pfp_" + tid,)).fetchone() or [None])[0]
        return hashlib.sha1(payload).hexdigest() != old
    finally:
        con.close()


def _fingerprint(data):
    d = dict(data)
    d.pop("generated", None)
    return hashlib.sha1(json.dumps(d, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _remember(con, tid, data):
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("view_fp_" + tid, _fingerprint(data)))
    con.commit()


def changed(tid):
    """Hat sich seit der letzten Archiv-Ansicht etwas geändert (Stichwörter, Alben, Personen, Favoriten, Datum, neue Fotos …)?
    Rechnet die Daten neu (~3 s) und vergleicht den Fingerabdruck – lädt nichts hoch."""
    t = backup.target(tid)
    base = backup.prefix_of(t) + VIEW_DIR
    con = connect()
    try:
        con.executescript(SCHEMA)
        have_p = {k for (k,) in con.execute("SELECT key FROM view_files WHERE target=? AND key LIKE ?", (tid, base + "p/%"))}
        fp = _fingerprint(build_data(con, _items(con), have_p, base))
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        old = (con.execute("SELECT value FROM meta WHERE key=?", ("view_fp_" + tid,)).fetchone() or [None])[0]
        return fp != old
    finally:
        con.close()


def build_data(con, rows, have_p, base):
    """Alle Angaben für die Seite: Einträge + Nachschlagelisten (Kameras, Orte, Ordner, Alben, Ereignisse, Personen)."""
    extra = _extra(con)
    cams, places, folders = {}, {}, {}
    idx = lambda d, v: d.setdefault(v, len(d)) if v else -1  # noqa: E731
    recs, ids, kw, pano = [], set(), {}, {}
    for iid, path, kind, ext, taken, w, h, userrot, edit, _pv in rows:
        mode = _view_mode(kind, ext, userrot, edit)
        if mode == "p" and base + "p/%d.jpg" % iid not in have_p:
            mode = "t"
        if mode == "o" and ext not in BROWSER_IMG | BROWSER_VID:
            mode = "t"
        if userrot and kind == "photo" and mode == "o":
            mode = "t"
        fav, cam, place, lat, lon, dur, pinfo, words = extra.get(iid, (0, None, None, None, None, None, "", ""))
        folder, name = path.rsplit("/", 1) if "/" in path else ("", path)
        flags = (1 if fav else 0) | (2 if pinfo else 0)
        r = [iid, "v" if kind == "video" else "p", (taken or "")[:16], mode, idx(folders, folder), name, w or 0, h or 0,
             flags, idx(cams, _cam_label(cam) if cam else None), idx(places, place), int(dur or 0)]
        if lat is not None and lon is not None and (abs(lat) > 1e-4 or abs(lon) > 1e-4):
            r += [round(lat, 5), round(lon, 5)]
        recs.append(r)
        ids.add(iid)
        if words:
            kw[iid] = words
        if pinfo:
            try:
                pano[iid] = json.loads(pinfo)
            except ValueError:
                pano[iid] = {}
    import privacy

    priv = privacy.private_albums(con) if hasattr(privacy, "private_albums") else set()
    # Alben wie in FotoArchiv (server.r_albums): Baum über parent, Titelbild = gewähltes oder zuletzt hinzugefügtes
    albums = []
    for aid, name, parent, desc, own_cover in con.execute(
            "SELECT id, name, parent, description, cover FROM albums ORDER BY name COLLATE NOCASE"):
        if aid in priv:
            continue
        members = [r[0] for r in con.execute("SELECT ai.item_id FROM album_items ai JOIN items i ON i.id = ai.item_id "
                                             "WHERE ai.album_id=? ORDER BY i.taken DESC", (aid,)) if r[0] in ids]
        cover = own_cover if own_cover in ids else (max(members) if members else None)
        albums.append([aid, name, parent, members, cover, desc or ""])
    # Ereignisse: Titelbild wie in FotoArchiv (Favoriten zuerst, dann nach Datum, das Bild bei einem Drittel)
    events = []
    for eid, name, start, end in con.execute("SELECT id, name, start, end FROM events WHERE COALESCE(private,0)=0 "
                                             "ORDER BY start DESC"):
        rows_e = [r[0] for r in con.execute(
            "SELECT id FROM items WHERE taken >= ? AND taken <= ? AND dup_of IS NULL AND raw_of IS NULL "
            "AND COALESCE(hidden,0)=0 ORDER BY fav DESC, rating DESC, taken", ((start or "")[:10], (end or start or "")[:10] + " 23:59:59"))
            if r[0] in ids]
        events.append([eid, name, start, end, rows_e[len(rows_e) // 3] if rows_e else None])
    persons = []
    for pid, name in con.execute("SELECT id, name FROM persons WHERE COALESCE(hidden,0)=0 AND name IS NOT NULL "
                                 "AND name<>'' ORDER BY name COLLATE NOCASE"):
        members = sorted({r[0] for r in con.execute("SELECT item_id FROM faces WHERE person_id=?", (pid,)) if r[0] in ids})
        if members:
            persons.append([pid, name, members])
    return {"generated": datetime.datetime.now().isoformat(timespec="minutes"), "total": len(recs),
            "cams": list(cams), "places": list(places), "folders": list(folders), "recs": recs, "albums": albums,
            "events": events, "persons": persons, "kw": kw, "pano": pano}


def config(t):
    c = backup.client(t)
    return {"scheme": c.scheme, "host": c.host, "base_path": c.base_path, "bucket": c.bucket,
            "path_style": c.path_style, "region": c.region, "sig": c.sig, "prefix": backup.prefix_of(t),
            "view": backup.prefix_of(t) + VIEW_DIR, "name": t.get("name") or t["bucket"]}


def page(t):
    return TEMPLATE.replace("/*CONFIG*/{}", json.dumps(config(t), ensure_ascii=False))


def link(tid, days=7):
    """Signierter Link zur Seite (Signatur V4: höchstens 7 Tage; V2: frei wählbar)."""
    t = backup.target(tid)
    c = backup.client(t)
    return c.presign(backup.prefix_of(t) + VIEW_DIR + "index.html", days * 86400 if c.sig == "v4" else 365 * 86400)


def start(tid):
    d = backup.load()
    return backup.start("view", run, tid, d["backup"])


TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "archview.html"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "archview.html")) else ""
