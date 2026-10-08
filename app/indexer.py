"""Durchsucht die Bibliothek und hält den Katalog aktuell.

Ablauf: 1. Dateien auflisten (schnell), 2. neue/geänderte Dateien parallel
verarbeiten, 3. Orte, Duplikate, Suchindex und Gesichtsvorschläge aktualisieren.
Kann jederzeit abgebrochen und später fortgesetzt werden.
"""
import datetime
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import threading
import time

import common
from common import FACE_KEY_OFFSET, THUMBS, connect, kind_for_ext, to_abs, to_rel

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]
SEASONS = {12: "Winter", 1: "Winter", 2: "Winter", 3: "Frühling", 4: "Frühling", 5: "Frühling",
           6: "Sommer", 7: "Sommer", 8: "Sommer", 9: "Herbst", 10: "Herbst", 11: "Herbst"}
KIND_WORDS = {"photo": "Foto", "raw": "RAW Foto", "video": "Video Film"}


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.stop = False
        self.phase = ""
        self.done = 0
        self.total = 0
        self.errors = 0
        self.started = None
        self.message = ""
        self.finished = None
        self.phase_started = time.time()
        self.rerun = False

    def snapshot(self):
        with self.lock:
            d = {k: getattr(self, k) for k in ("running", "phase", "done", "total", "errors", "message", "finished")}
            if self.running and self.started and self.done and self.total:
                el = time.time() - self.phase_started
                d["eta"] = el / self.done * (self.total - self.done)
            return d

    def set_phase(self, phase, total=0):
        with self.lock:
            self.phase, self.total, self.done = phase, total, 0
            self.phase_started = time.time()


PROGRESS = Progress()


# ------------------------------------------------------------- 1. Scan ----

# Ordner, die nie Fotos des Nutzers enthalten: Papierkorb, Systemordner, NAS-Vorschaubilder/-Papierkörbe
SKIP_DIRS = {"fotoarchiv-papierkorb", "$recycle.bin", "system volume information", "@eadir", "#recycle", "#snapshot",
             "@recycle", "@recently-snapshot", ".snapshot", "@sharebin", "lost+found"}


def scan(con, folders):
    PROGRESS.set_phase("Dateien suchen")
    if not os.path.isdir(common.LIB_ROOT):
        # Fotoordner nicht erreichbar (Platte nicht angesteckt): nichts als "gelöscht" werten
        PROGRESS.message = "Fotoordner nicht erreichbar: " + common.LIB_ROOT
        return None
    # Einträge im Papierkorb (hidden=2) gehören nicht zur Bibliothek und bleiben unangetastet
    known = {row[0]: row[1:] for row in con.execute("SELECT path, id, size, mtime, xmp_mtime FROM items "
                                                    "WHERE COALESCE(hidden,0) != 2")}
    seen = set()
    new_rows, changed = [], []
    count = 0
    for top in folders:
        top_abs = to_abs(top)
        if not os.path.isdir(top_abs):
            continue
        stack = [top_abs]
        while stack:
            if PROGRESS.stop:
                return None
            dirpath = stack.pop()
            try:
                # scandir liefert Größe/Datum unter Windows ohne Extra-Zugriff
                entries = list(os.scandir(dirpath))
            except OSError:
                continue
            files = {}
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in SKIP_DIRS:
                            stack.append(e.path)
                    else:
                        files[e.name.lower()] = e
                except OSError:
                    pass
            for low, e in files.items():
                ext = low.rsplit(".", 1)[-1] if "." in low else ""
                kind = kind_for_ext(ext)
                if not kind:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                stem = low.rsplit(".", 1)[0]
                xe = files.get(stem + ".xmp") or files.get(low + ".xmp")
                xmp_m = None
                if xe is not None:
                    try:
                        xmp_m = xe.stat().st_mtime
                    except OSError:
                        pass
                rel = to_rel(e.path)
                seen.add(rel)
                old = known.get(rel)
                if old is None:
                    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    new_rows.append((rel, folder, common.norm(e.name), ext, kind, st.st_size, st.st_mtime, xmp_m))
                elif old[1] != st.st_size or _time_changed(old[2], st.st_mtime) or _time_changed(old[3], xmp_m):
                    changed.append((st.st_size, st.st_mtime, xmp_m, old[0]))
                count += 1
                if count % 2000 == 0:
                    with PROGRESS.lock:
                        PROGRESS.done = count
                        PROGRESS.message = "%d Dateien gefunden" % count
            if len(new_rows) > 5000:
                _insert(con, new_rows)
                new_rows = []
    _insert(con, new_rows)
    con.executemany("UPDATE items SET size=?, mtime=?, xmp_mtime=?, proc_ver=0 WHERE id=?", changed)
    # Entfernte Dateien löschen (nur in Ordnern, die gerade erreichbar sind)
    roots = [f + "/" for f in folders if os.path.isdir(to_abs(f))]
    gone = [(i,) for p, (i, *_x) in known.items() if p not in seen and any(p.startswith(r) for r in roots)]
    excluded = [(i,) for p, (i, *_x) in known.items() if not any(p.startswith(f + "/") for f in folders)]
    # während des Suchens in den Papierkorb gelegte Fotos nicht mitlöschen
    trashed = {r[0] for r in con.execute("SELECT id FROM items WHERE hidden=2")}
    remove_items(con, [g[0] for g in gone + excluded if g[0] not in trashed])
    con.commit()
    return count


def _time_changed(old, new):
    """Wurde die Datei geändert? exFAT-Zeitstempel erscheinen je nach Rechner/Zeitzone (Windows/Mac)
    um ganze Stunden verschoben – das allein gilt nicht als Änderung, sonst würde nach dem Umstecken
    an einen anderen Computer die ganze Bibliothek neu verarbeitet."""
    if old is None or new is None:
        return (old is None) != (new is None)
    diff = abs(old - new)
    if diff <= 2:
        return False
    hours = round(diff / 3600)
    return not (1 <= hours <= 14 and abs(diff - hours * 3600) <= 2)


def _insert(con, rows):
    con.executemany("INSERT OR IGNORE INTO items(path, folder, name, ext, kind, size, mtime, xmp_mtime) "
                    "VALUES(?,?,?,?,?,?,?,?)", rows)
    con.commit()


def remove_items(con, ids):
    if not ids:
        return
    face_ids = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        face_ids += [r[0] for r in con.execute("SELECT id FROM faces WHERE item_id IN (%s)" % ph, chunk)]
        con.execute("DELETE FROM faces WHERE item_id IN (%s)" % ph, chunk)
        con.execute("DELETE FROM items WHERE id IN (%s)" % ph, chunk)
        con.execute("DELETE FROM fts WHERE rowid IN (%s)" % ph, chunk)
    con.commit()
    THUMBS.delete(ids + [FACE_KEY_OFFSET + f for f in face_ids])


# ------------------------------------------------------- 2. Verarbeiten ----

def _worker_init():
    # Niedrige Priorität, damit der Computer nebenher flüssig bedienbar bleibt
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except (AttributeError, OSError):
        pass


def process_pending(con, workers):
    from media import PROC_VERSION, process

    todo = con.execute("SELECT id, path, kind, userrot FROM items WHERE proc_ver < ? "
                       "ORDER BY xmp_mtime IS NULL, ext IN ('psd', 'psb', 'tif', 'tiff'), folder DESC, name",  # Riesendateien zuletzt
                       (PROC_VERSION,)).fetchall()
    PROGRESS.set_phase("Fotos verarbeiten", len(todo))
    if not todo:
        return
    persons = {name.lower(): pid for pid, name in con.execute("SELECT id, name FROM persons")}
    mylio = load_mylio().get("media", {})
    # Wie viele Dateien teilen sich einen Namen (z. B. IMG_1234.CR2 + IMG_1234.JPG)?
    stems = {}
    for (p,) in con.execute("SELECT path FROM items"):
        k = mylio_key(p)
        stems[k] = stems.get(k, 0) + 1
    tasks = ({"id": i, "path": to_abs(p), "kind": k, "faces": True, "mylio": mylio.get(mylio_key(p)),
              "shared": stems.get(mylio_key(p), 1) > 1, "userrot": r or 0}
             for i, p, k, r in todo)
    ctx = mp.get_context("spawn")
    pending_thumbs = []
    buffer = []
    last_flush = time.time()

    def flush():
        # Ergebnisse gesammelt in einem kurzen Schreibvorgang speichern, damit die Datenbank
        # zwischendurch frei ist (Bearbeiten in der Oberfläche während des Einlesens)
        for attempt in range(20):
            try:
                for r in buffer:
                    _store(con, r, persons, pending_thumbs)
                con.commit()
                break
            except sqlite3.OperationalError as ex:
                # Katalog kurz von anderer Stelle belegt (Import, Oberfläche): zurückrollen, warten, nochmal
                if "locked" not in str(ex) or attempt == 19:
                    raise
                con.rollback()
                pending_thumbs.clear()
                persons.clear()
                persons.update({name.lower(): pid for pid, name in con.execute("SELECT id, name FROM persons")})
                time.sleep(3)
        buffer.clear()
        THUMBS.put_many(pending_thumbs)
        pending_thumbs.clear()

    with ctx.Pool(workers, initializer=_worker_init) as pool:
        for res in pool.imap_unordered(process, tasks, chunksize=2):
            buffer.append(res)
            with PROGRESS.lock:
                PROGRESS.done += 1
                if res.get("error"):
                    PROGRESS.errors += 1
            if len(buffer) >= 100 or time.time() - last_flush > 2:
                flush()
                last_flush = time.time()
            if PROGRESS.stop:
                pool.terminate()
                break
    flush()


def _person_id(con, persons, name):
    key = name.lower()
    if key not in persons:
        cur = con.execute("INSERT INTO persons(name) VALUES(?)", (name,))
        persons[key] = cur.lastrowid
    return persons[key]


def _store(con, res, persons, pending_thumbs):
    from media import PROC_VERSION

    iid = res["id"]
    m = res["meta"]
    if res.get("error"):
        path = con.execute("SELECT path, mtime FROM items WHERE id=?", (iid,)).fetchone()
        from media import date_from_name

        taken = date_from_name(path[0]) if path else None
        con.execute("UPDATE items SET proc_ver=?, error=?, taken=COALESCE(taken, ?), taken_src=COALESCE(taken_src, ?) "
                    "WHERE id=?", (PROC_VERSION, res["error"], taken, "name" if taken else None, iid))
        return
    con.execute(
        "UPDATE items SET taken=COALESCE(usertaken, ?), taken_src=CASE WHEN usertaken IS NULL THEN ? ELSE 'manual' END, width=?, height=?, duration=?, lat=?, lon=?, place=NULL, camera=?, "
        "rating=?, fav=MAX(fav, ?), keywords=?, caption=?, proc_ver=?, has_thumb=?, phash=?, orient=?, rotfix=?, "
        "error=NULL WHERE id=?",
        (m.get("taken"), m.get("taken_src"), m.get("width"), m.get("height"), m.get("duration"), m.get("lat"),
         m.get("lon"), m.get("camera"), m.get("rating", 0), m.get("flag", 0), m.get("keywords"),
         m.get("caption"), PROC_VERSION, 1 if res.get("thumb") else 0, m.get("phash"), m.get("orient"),
         m.get("rotfix", 0), iid))
    if res.get("thumb"):
        if con.execute("SELECT edit FROM items WHERE id=?", (iid,)).fetchone()[0]:
            THUMBS.delete([iid])  # bearbeitet: Vorschau wird beim nächsten Anzeigen mit Bearbeitung erzeugt
        else:
            pending_thumbs.append((iid, res["thumb"]))
    if res.get("faces") is not None:
        # Manuelle Zuordnungen bleiben bei erneuter Verarbeitung erhalten
        manual = con.execute("SELECT x, y, w, h, person_id FROM faces WHERE item_id=? AND source='manual'",
                             (iid,)).fetchall()
        old = [r[0] for r in con.execute("SELECT id FROM faces WHERE item_id=?", (iid,))]
        if old:
            con.execute("DELETE FROM faces WHERE item_id=?", (iid,))
            THUMBS.delete([FACE_KEY_OFFSET + f for f in old])
        for f in res["faces"]:
            pid = _person_id(con, persons, f["name"]) if f.get("name") else None
            source = f["source"] if pid else "det"
            if pid is None and f.get("x") is not None:
                for mx, my, mw, mh, mpid in manual:
                    if mx is not None and abs(mx - f["x"]) < 0.05 and abs(my - f["y"]) < 0.05:
                        pid, source = mpid, "manual"
            cur = con.execute(
                "INSERT INTO faces(item_id, x, y, w, h, px, score, emb, person_id, source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (iid, f.get("x"), f.get("y"), f.get("w"), f.get("h"), f.get("px"), f.get("score"), f.get("emb"),
                 pid, source))
            if f.get("crop"):
                pending_thumbs.append((FACE_KEY_OFFSET + cur.lastrowid, f["crop"]))


# ------------------------------------------------------ 3. Nacharbeiten ----

_MYLIO = None


def load_mylio():
    """Daten aus dem Mylio-Katalog (data/mylio.json, erzeugt von tools/mylio_export.py)."""
    global _MYLIO
    try:
        mt = os.path.getmtime(common.MYLIO_JSON)
    except OSError:
        return {}
    if _MYLIO is None or _MYLIO[0] != mt:
        with open(common.MYLIO_JSON, encoding="utf-8") as f:
            _MYLIO = (mt, json.load(f))
    return _MYLIO[1]


def _mylio_album_items(con, data, by_key, refresh_only, deleted=()):
    """Mylio-Alben anlegen (falls neu) und ihre Fotos eintragen. Gelöschte Alben bleiben gelöscht."""
    existing = {}
    for aid, name, parent in con.execute("SELECT id, name, parent FROM albums WHERE source='mylio'"):
        existing[(name, parent)] = aid
    albums = data.get("albums", [])
    ids = [None] * len(albums)

    def resolve(i, depth=0):
        if ids[i] is not None or depth > 20:
            return ids[i]
        a = albums[i]
        if "a:" + a["name"] in deleted:
            return None
        parent = resolve(a["parent"], depth + 1) if a.get("parent") is not None else None
        aid = existing.get((a["name"], parent))
        if aid is None and not refresh_only:
            aid = con.execute("INSERT INTO albums(name, parent, description, source) VALUES(?,?,?,'mylio')",
                              (a["name"], parent, a.get("description") or None)).lastrowid
            existing[(a["name"], parent)] = aid
        ids[i] = aid
        return aid
    removed = {(r[0], r[1]) for r in con.execute("SELECT album_id, item_id FROM album_removed")}
    for i, a in enumerate(albums):
        aid = resolve(i)
        if aid is None:
            continue
        con.executemany("INSERT OR IGNORE INTO album_items(album_id, item_id) VALUES(?,?)",
                        [(aid, iid) for k in a["items"] for iid in by_key.get(common.norm(k), [])
                         if (aid, iid) not in removed])


def mylio_key(rel):
    folder, _, name = rel.rpartition("/")
    return common.norm(folder + "/" + name.rsplit(".", 1)[0].lower())


def import_mylio(con):
    """Ereignisse, Alben, Bewertungen und Beschriftungen aus Mylio übernehmen."""
    data = load_mylio()
    if not data:
        return
    PROGRESS.set_phase("Mylio-Daten übernehmen")
    by_key = {}
    for iid, path in con.execute("SELECT id, path FROM items"):
        by_key.setdefault(mylio_key(path), []).append(iid)
    stamp = data.get("exported")
    done = con.execute("SELECT value FROM meta WHERE key='mylio_import'").fetchone()
    if done and done[0] == stamp:
        # Schon übernommen: nur neue Dateien in bestehende Mylio-Alben eintragen
        _mylio_album_items(con, data, by_key, refresh_only=True)
        _mylio_media_fields(con, data, by_key, with_fav=False)
        return
    # Ereignisse/Alben nur ergänzen, nie löschen oder überschreiben (Benutzer kann sie bearbeitet haben)
    known_events = {n for (n,) in con.execute("SELECT name FROM events")}
    deleted = set(json.loads((con.execute("SELECT value FROM meta WHERE key='mylio_deleted'").fetchone()
                              or ["[]"])[0]))
    con.executemany("INSERT INTO events(name, start, end, source) VALUES(?,?,?,'mylio')",
                    [(e["name"], e["start"], e["end"]) for e in data.get("events", [])
                     if e["name"] not in known_events and "e:" + e["name"] not in deleted])
    _mylio_album_items(con, data, by_key, refresh_only=False, deleted=deleted)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('mylio_import', ?)", (stamp,))
    _mylio_media_fields(con, data, by_key, with_fav=True)


def _mylio_media_fields(con, data, by_key, with_fav):
    """Bewertung, Beschreibung, Stichwörter aus Mylio (die Verarbeitung überschreibt sie sonst mit XMP).
    Favoriten nur beim ersten Import, damit vom Benutzer entfernte Favoriten entfernt bleiben."""
    upd = []
    for k, m in data.get("media", {}).items():
        if not ("r" in m or "f" in m or "c" in m or "k" in m):
            continue
        for iid in by_key.get(common.norm(k), []):
            upd.append((m.get("r", 0), m.get("f", 0) if with_fav else 0, m.get("c"), m.get("k"), iid))
    con.executemany("UPDATE items SET rating=MAX(COALESCE(rating,0), ?), fav=MAX(fav, ?), "
                    "caption=COALESCE(?, caption), keywords=COALESCE(?, keywords) WHERE id=?", upd)
    con.commit()
    _mylio_dates_places(con, data, by_key)


def _secs(a, b):
    return abs((datetime.datetime.strptime(a[:19], "%Y-%m-%d %H:%M:%S") -
                datetime.datetime.strptime(b[:19], "%Y-%m-%d %H:%M:%S")).total_seconds())


def _mylio_dates_places(con, data, by_key):
    """Aufnahmedatum (inkl. in Mylio korrigierter Daten), Undatiert-Markierung und Ort aus Mylio übernehmen.
    Mylio hat Vorrang vor Kamera/XMP/Dateidatum – außer das Datum wurde in FotoArchiv selbst gesetzt.
    Teilen sich mehrere Dateien einen Namen, nur wenn das Kameradatum zur Datei passt."""
    info = {r[0]: r[1:] for r in con.execute(
        "SELECT id, taken, taken_src, usertaken, lat, lon FROM items WHERE proc_ver > 0")}
    dates, places = [], []
    for k, m in data.get("media", {}).items():
        if not ("t" in m or "u" in m or "g" in m):
            continue
        ids = by_key.get(common.norm(k), [])
        for iid in ids:
            if iid not in info:
                continue
            taken, src, usertaken, lat, lon = info[iid]
            if len(ids) > 1:
                cam = m.get("d")
                if not (taken and cam and _secs(taken, cam) <= 120) and not (taken and m.get("t") and _secs(taken, m["t"]) <= 2):
                    continue
            if not usertaken:
                if m.get("u"):
                    if src not in ("exif", "video", "undated") and taken is not None:
                        dates.append((None, "undated", iid))
                elif m.get("t"):
                    t, te = m["t"], m.get("te")
                    if te:  # Zeitraum (z. B. "irgendwann im August 2002"): eigene Uhrzeit behalten, wenn sie hineinpasst
                        ok = taken is not None and t[:10] <= taken[:10] <= te[:10]
                    else:
                        ok = taken is not None and _secs(taken, t) <= 2
                    if not ok:
                        dates.append((t, "mylio", iid))
            g = m.get("g")
            if g and (lat is None or abs(lat - g[0]) > 0.001 or abs(lon - g[1]) > 0.001):
                places.append((g[0], g[1], iid))
    con.executemany("UPDATE items SET taken=?, taken_src=? WHERE id=?", dates)
    con.executemany("UPDATE items SET lat=?, lon=?, place=NULL WHERE id=?", places)
    con.commit()
    if places:
        geocode(con)
    changed = sorted({d[-1] for d in dates} | {p[-1] for p in places})
    for i in range(0, len(changed), 5000):
        rebuild_fts(con, changed[i:i + 5000])
    PROGRESS.message = "Mylio: %d Daten, %d Orte übernommen" % (len(dates), len(places))
    return len(dates), len(places)


_GEO = None
COUNTRY_DE = {
    "DE": "Deutschland", "AT": "Österreich", "CH": "Schweiz", "FR": "Frankreich", "IT": "Italien",
    "ES": "Spanien", "PT": "Portugal", "NL": "Niederlande", "BE": "Belgien", "LU": "Luxemburg",
    "DK": "Dänemark", "SE": "Schweden", "NO": "Norwegen", "FI": "Finnland", "PL": "Polen",
    "CZ": "Tschechien", "HU": "Ungarn", "HR": "Kroatien", "SI": "Slowenien", "GR": "Griechenland",
    "TR": "Türkei", "GB": "Großbritannien", "IE": "Irland", "US": "USA", "CA": "Kanada",
    "MX": "Mexiko", "EG": "Ägypten", "MA": "Marokko", "TN": "Tunesien", "ZA": "Südafrika",
    "TH": "Thailand", "JP": "Japan", "CN": "China", "AU": "Australien", "NZ": "Neuseeland",
    "IS": "Island", "MT": "Malta", "CY": "Zypern", "RU": "Russland", "SK": "Slowakei",
    "BG": "Bulgarien", "RO": "Rumänien", "AE": "Vereinigte Arabische Emirate", "IN": "Indien",
    "BR": "Brasilien", "AR": "Argentinien", "ID": "Indonesien", "VN": "Vietnam", "LI": "Liechtenstein",
    "ME": "Montenegro", "AL": "Albanien", "RS": "Serbien", "BA": "Bosnien und Herzegowina",
    "EE": "Estland", "LV": "Lettland", "LT": "Litauen", "MV": "Malediven", "MU": "Mauritius",
    "CU": "Kuba", "DO": "Dominikanische Republik", "KE": "Kenia", "TZ": "Tansania",
}


def _geo():
    global _GEO
    if _GEO is None:
        import numpy as np

        names, cc, lat, lon = [], [], [], []
        with open(os.path.join(common.MODELS_DIR, "cities.txt"), encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                names.append(c[0])
                lat.append(float(c[1]))
                lon.append(float(c[2]))
                cc.append(c[3])
        countries = {}
        with open(os.path.join(common.MODELS_DIR, "countries.txt"), encoding="utf-8") as f:
            for line in f:
                k, v = line.rstrip("\n").split("\t")
                countries[k] = v
        la, lo = np.radians(np.array(lat)), np.radians(np.array(lon))
        xyz = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], 1).astype("float32")
        _GEO = (names, cc, xyz, countries)
    return _GEO


def geocode(con):
    import numpy as np

    rows = con.execute("SELECT id, lat, lon FROM items WHERE lat IS NOT NULL AND place IS NULL").fetchall()
    if not rows:
        return
    PROGRESS.set_phase("Orte bestimmen", len(rows))
    names, cc, xyz, countries = _geo()
    for i in range(0, len(rows), 2000):
        chunk = rows[i:i + 2000]
        la = np.radians(np.array([r[1] for r in chunk]))
        lo = np.radians(np.array([r[2] for r in chunk]))
        q = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], 1).astype("float32")
        best = np.argmax(q @ xyz.T, axis=1)
        upd = []
        for (iid, _, _), b in zip(chunk, best):
            code = cc[b]
            country = COUNTRY_DE.get(code) or countries.get(code, code)
            upd.append((names[b] + ", " + country, iid))
        con.executemany("UPDATE items SET place=? WHERE id=?", upd)
        con.commit()
        PROGRESS.done = i + len(chunk)


def mark_duplicates(con):
    """Doppelte Fotos (gleiche Aufnahmezeit, Größe und praktisch gleicher Bildinhalt)
    sowie RAW+JPEG-Paare. Behalten wird jeweils die größte Datei."""
    PROGRESS.set_phase("Duplikate suchen")
    groups, photos, raws = {}, {}, []
    never = {r[0] for r in con.execute("SELECT item_id FROM dup_keep")}
    for iid, folder, name, ext, kind, size, taken, w, h, ph, stack, ev in con.execute(
            "SELECT id, folder, name, ext, kind, size, taken, width, height, phash, stack, ev_bias FROM items "
            "WHERE COALESCE(hidden,0) != 2 ORDER BY id"):
        if taken and w and taken_src_ok(taken):
            groups.setdefault((taken, kind, min(w, h), max(w, h)), []).append((size or 0, iid, ph, stack, ev))
        stem = (folder, name[:-(len(ext) + 1)].lower())
        if kind == "photo":
            photos.setdefault(stem, iid)
        elif kind == "raw":
            raws.append((stem, iid))
    dups = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (-m[0], m[1]))
        keep = []
        for size, iid, ph, st, ev in members:
            for _ksize, kid, kph, kst, kev in ([] if iid in never else keep):
                # Belichtungsreihe (stacks.py) bzw. andere Belichtungskorrektur: keine Kopie
                if (st is not None and st == kst) or (ev is not None and kev is not None and ev != kev):
                    continue
                if ph is not None and kph is not None and bin((ph ^ kph) & 0xFFFFFFFFFFFFFFFF).count("1") <= 5:
                    dups.append((kid, iid))
                    break
            else:
                keep.append((size, iid, ph, st, ev))
    con.execute("UPDATE items SET dup_of=NULL, raw_of=NULL")
    con.executemany("UPDATE items SET dup_of=? WHERE id=?", dups)
    con.executemany("UPDATE items SET raw_of=? WHERE id=?", [(photos[s], i) for s, i in raws if s in photos])
    con.commit()


def taken_src_ok(taken):
    return not taken.endswith("00:00:00")


def item_text(con, row, persons_of, extra_of=None):
    (iid, path, name, kind, taken, place, camera, keywords, caption) = row
    parts = [path.replace("/", " ").replace("_", " "), KIND_WORDS.get(kind, "")]
    if taken:
        y, mo = int(taken[:4]), int(taken[5:7])
        parts += [str(y), MONTHS[mo - 1], SEASONS[mo], taken[:10]]
    else:
        parts.append("undatiert ohne Datum")
    for v in (place, camera, keywords, caption):
        if v:
            parts.append(v)
    parts += persons_of.get(iid, [])
    if extra_of:
        parts += extra_of(iid, taken)
    return " ".join(parts)


def _extras(con):
    """Album- und Ereignisnamen pro Foto für die Suche."""
    albums = {}
    for iid, name in con.execute("SELECT ai.item_id, a.name FROM album_items ai JOIN albums a ON a.id=ai.album_id"):
        albums.setdefault(iid, []).append(name)
    events = con.execute("SELECT name, start, end FROM events").fetchall()

    def extra(iid, taken):
        out = list(albums.get(iid, []))
        if taken:
            out += [n for n, s, e in events if s[:10] <= taken[:10] <= e[:10]]
        return out
    return extra


def rebuild_fts(con, ids=None):
    persons_of = {}
    sql = ("SELECT DISTINCT f.item_id, p.name FROM faces f JOIN persons p ON p.id = f.person_id")
    if ids is not None:
        if not ids:
            return
        ph = ",".join("?" * len(ids))
        rows = con.execute(sql + " WHERE f.item_id IN (%s)" % ph, list(ids)).fetchall()
    else:
        rows = con.execute(sql).fetchall()
    for iid, name in rows:
        persons_of.setdefault(iid, []).append(name)
    q = "SELECT id, path, name, kind, taken, place, camera, COALESCE(keywords,'') || ' ' || COALESCE(usertags,''), caption FROM items"
    extra = _extras(con)
    if ids is not None:
        items = con.execute(q + " WHERE id IN (%s)" % ph, list(ids)).fetchall()
        con.execute("DELETE FROM fts WHERE rowid IN (%s)" % ph, list(ids))
        con.executemany("INSERT INTO fts(rowid, text) VALUES(?, ?)",
                        ((r[0], item_text(con, r, persons_of, extra)) for r in items))
        con.commit()
        return
    PROGRESS.set_phase("Suchindex aufbauen")
    items = con.execute(q).fetchall()
    con.execute("DELETE FROM fts WHERE rowid NOT IN (SELECT id FROM items)")
    con.commit()
    # in Häppchen schreiben, damit die Oberfläche zwischendurch speichern kann
    for start in range(0, len(items), 5000):
        chunk = items[start:start + 5000]
        con.executemany("DELETE FROM fts WHERE rowid=?", [(r[0],) for r in chunk])
        con.executemany("INSERT INTO fts(rowid, text) VALUES(?, ?)",
                        ((r[0], item_text(con, r, persons_of, extra)) for r in chunk))
        con.commit()


# ---------------------------------------------------------------- Ablauf ----

def _stay_awake():
    """Während des Einlesens nicht in den Ruhezustand gehen (Bildschirm darf aus)."""
    try:
        if os.name == "nt":
            import ctypes

            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED – gilt für diesen Thread bis zum Zurücksetzen
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            return "nt"
        if sys.platform == "darwin":
            import subprocess

            return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except Exception:
        pass
    return None


def _allow_sleep(token):
    try:
        if token == "nt":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        elif token is not None:
            token.terminate()
    except Exception:
        pass


def run(full_faces=False):
    if PROGRESS.running:
        return
    PROGRESS.reset()
    PROGRESS.running = True
    PROGRESS.started = time.time()
    awake = _stay_awake()
    con = connect()
    try:
        # erst am Ende wieder auf "1" – so erkennt der nächste Start (auch an einem anderen Computer),
        # dass ein Lauf unterbrochen wurde, und setzt ihn fort
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('index_complete', '0')")
        con.commit()
        cfg = common.load_config()
        folders = common.included_folders(cfg)
        workers = cfg.get("workers") or max(2, min(8, (os.cpu_count() or 4) - 2))
        if scan(con, folders) is None:
            return
        import_mylio(con)
        import importer
        import privacy

        importer.apply_ledger(con)
        privacy.refresh(con)
        process_pending(con, workers)
        if PROGRESS.stop:
            return
        geocode(con)
        import_mylio(con)
        import stacks

        PROGRESS.set_phase("Belichtungsreihen suchen")
        stacks.detect(con)
        import panos

        panos.detect(con)  # 360°-Fotos unter den neuen
        mark_duplicates(con)
        import faces

        faces.recompute(con, PROGRESS)
        rebuild_fts(con)
        privacy.refresh(con)
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_index', ?)",
                    (datetime.datetime.now().isoformat(timespec="seconds"),))
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('index_complete', '1')")
        con.commit()
        PROGRESS.message = "Fertig"
        try:
            import docs

            if docs.available() and docs.auto_level():
                docs.scan()  # nur noch nicht geprüfte Fotos, im Hintergrund
        except Exception:
            pass
    except Exception as e:
        import traceback

        traceback.print_exc()
        PROGRESS.message = "Fehler: %s" % e
    finally:
        con.close()
        _allow_sleep(awake)
        rerun = PROGRESS.rerun and not PROGRESS.stop
        PROGRESS.running = False
        PROGRESS.finished = time.time()
        PROGRESS.phase = ""
        if rerun:
            # während des Laufs wurde importiert: gleich nochmal, damit die neuen Dateien drin sind
            start_background()


def needs_resume():
    """Unterbrochener Lauf oder noch unverarbeitete Dateien?"""
    from media import PROC_VERSION

    con = connect()
    try:
        flag = con.execute("SELECT value FROM meta WHERE key='index_complete'").fetchone()
        pending = con.execute("SELECT 1 FROM items WHERE proc_ver < ? LIMIT 1", (PROC_VERSION,)).fetchone()
        return (flag is not None and flag[0] != "1") or pending is not None
    finally:
        con.close()


def start_background():
    if PROGRESS.running:
        return False
    threading.Thread(target=run, daemon=True).start()
    return True


if __name__ == "__main__":
    mp.freeze_support()
    run()
    print(PROGRESS.snapshot())
