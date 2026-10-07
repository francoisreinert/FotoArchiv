"""Import mit Abgleich: iCloud (automatisch), Google Takeout und beliebige Ordner/ZIPs.

Grundsatz: Es wird nur kopiert, was noch nicht auf der Platte liegt. Erkannt wird das über
  1. gleichen Dateinamen + gleiche Größe,
  2. gleiche Aufnahmezeit (auch bei Zeitzonenversatz) + gleiche Bildmaße,
  3. bei heruntergeladenen/entpackten Dateien zusätzlich über den Bildinhalt (Wahrnehmungs-Hash).
Jede Quelle führt eine Liste bereits erledigter Einträge (Tabelle imports), damit Wiederholungen schnell sind.
Neue Dateien landen in Bilder/<Jahr>/ – die Herkunft wird als Stichwort gespeichert.
"""
import datetime
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile

import common
from common import LIB_ROOT, connect, kind_for_ext, to_rel

TARGET_ROOT = "Bilder"
ICLOUD_DIR = os.path.join(common.DATA_DIR, "icloud")
ACCOUNTS_FILE = os.path.join(ICLOUD_DIR, "accounts.json")
RESUME_FILE = os.path.join(common.DATA_DIR, "import_resume.json")
TMP_DIR = os.path.join(common.DATA_DIR, "_import_tmp")
TRASH_DIRS = {"papierkorb", "trash", "bin", "corbeille", "recently deleted", "zuletzt gelöscht"}
YEAR_DIR = re.compile(r"^(photos from|fotos von|fotos aus|photos de)\s+\d{4}$", re.I)


# ------------------------------------------------------------- Fortschritt ----

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.reset("")

    def reset(self, title):
        # "running" bleibt unverändert: start() setzt es vor dem Thread, sonst sähe die Oberfläche
        # kurz "läuft nicht" und hörte auf, den Fortschritt abzufragen
        self.stop = False
        self.title = title
        self.phase = ""
        self.done = 0
        self.total = 0
        self.new = 0
        self.existing = 0
        self.errors = 0
        self.bytes = 0
        self.message = ""
        self.log = []
        self.finished = None

    def note(self, msg):
        with self.lock:
            self.log.append(time.strftime("%H:%M:%S ") + msg)
            self.log = self.log[-60:]

    def snapshot(self):
        with self.lock:
            return {k: getattr(self, k) for k in ("running", "title", "phase", "done", "total", "new", "existing",
                                                  "errors", "bytes", "message", "finished")} | {"log": self.log[-15:]}


JOB = Job()


# ------------------------------------------------------- Abgleich-Index ----

def _ts(s):
    return datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def _hamming(a, b):
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


class LibraryIndex:
    """Was liegt schon auf der Platte? Aus dem Katalog aufgebaut, beim Import laufend ergänzt."""

    def __init__(self, con):
        self.by_name = {}
        self.by_time = {}
        for path, name, kind, size, taken, w, h, ph in con.execute(
                "SELECT path, name, kind, size, taken, width, height, phash FROM items"):
            self.add(path, name, kind, size, taken, w, h, ph)

    def add(self, path, name, kind, size, taken, w, h, ph):
        if name and size:
            self.by_name[(name.lower(), size)] = path
        if taken and len(taken) >= 19:
            self.by_time.setdefault(taken[14:19], []).append((taken, path, kind, w, h, ph))

    def find(self, name, size, kind, taken=None, w=None, h=None, phash=None, content_required=False):
        """Pfad der vorhandenen Datei oder None. content_required: Treffer nur mit passendem Bildinhalt."""
        p = self.by_name.get(((name or "").lower(), size))
        if p:
            return p
        if not taken or len(taken) < 19:
            return None
        t = _ts(taken)
        image = kind in ("photo", "raw")
        # Minute:Sekunde als Schlüssel – so passen auch Zeitzonenverschiebungen um ganze Stunden
        for ds in (-2, -1, 0, 1, 2):
            key = (t + datetime.timedelta(seconds=ds)).strftime("%M:%S")
            for (lt, path, lkind, lw, lh, lph) in self.by_time.get(key, ()):
                if (lkind in ("photo", "raw")) != image:
                    continue
                diff = abs((_ts(lt) - t).total_seconds())
                hours_off = round(diff / 3600)
                if abs(diff - hours_off * 3600) > 2 or hours_off > 14:
                    continue
                if w and h and lw and lh and sorted((w, h)) != sorted((lw, lh)):
                    # gleiches Seitenverhältnis bei anderer Größe (z. B. verkleinerte Kopie)
                    if abs(max(w, h) / max(1, min(w, h)) - max(lw, lh) / max(1, min(lw, lh))) > 0.02:
                        continue
                if phash is not None and lph is not None:
                    if _hamming(phash, lph) <= 8:
                        return path
                    continue
                if content_required:
                    continue
                if hours_off == 0 or (w and lw):
                    return path
        return None


# ---------------------------------------------------- Dateien untersuchen ----

def file_meta(path, kind):
    """Aufnahmezeit, Maße und Bild-Hash einer lokalen Datei."""
    import media

    if kind == "video":
        im, vm = media.video_frame(path)
        w, h = vm["size"]
        return {"taken": vm.get("dt"), "w": w, "h": h, "phash": media.dhash(im), "lat": vm.get("lat")}
    im, exif, xmp, size, _o = media.open_image(path, kind, max_side=640)
    ex = media.exif_info(exif)
    taken = ex.get("dt")
    if not taken and xmp:
        x = media.parse_xmp(xmp)
        taken = media.parse_dt(x.get("dt_exif") or x.get("dt_photoshop"))
    return {"taken": taken, "w": size[0], "h": size[1], "phash": media.dhash(im), "lat": ex.get("lat")}


def dest_path(taken, name):
    folder = os.path.join(LIB_ROOT, TARGET_ROOT, taken[:4] if taken else "Undatiert")
    os.makedirs(folder, exist_ok=True)
    base, ext = os.path.splitext(name)
    # Kein anderer Dateiname mit gleichem Stamm (IMG_1.HEIC/IMG_1.MOV/IMG_1.xmp würden sonst als Paar gelten)
    stems = {os.path.splitext(f)[0].lower() for f in os.listdir(folder)}
    cand, n = base, 1
    while cand.lower() in stems:
        cand = "%s_%d" % (base, n)
        n += 1
    return os.path.join(folder, cand + ext)


def _gps_xmp(v, pos, neg):
    d = abs(v)
    deg = int(d)
    return "%d,%.6f%s" % (deg, (d - deg) * 60, pos if v >= 0 else neg)


def write_sidecar(dest, taken=None, lat=None, lon=None):
    """Datum/Ort, die nur in Googles Begleitdatei standen, als XMP neben das Foto schreiben."""
    attrs = []
    if taken:
        iso = taken.replace(" ", "T")
        attrs += ['exif:DateTimeOriginal="%s"' % iso, 'photoshop:DateCreated="%s"' % iso]
    if lat is not None and lon is not None:
        attrs += ['exif:GPSLatitude="%s"' % _gps_xmp(lat, "N", "S"), 'exif:GPSLongitude="%s"' % _gps_xmp(lon, "E", "W")]
    if not attrs:
        return
    xml = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description rdf:about="" xmlns:exif="http://ns.adobe.com/exif/1.0/" '
           'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" %s/></rdf:RDF></x:xmpmeta>' % " ".join(attrs))
    with open(os.path.splitext(dest)[0] + ".xmp", "w", encoding="utf-8") as f:
        f.write(xml)


def _check_space(size):
    free = shutil.disk_usage(LIB_ROOT).free
    if free < size + 2 * 1024 ** 3:
        raise OSError("Festplatte fast voll (nur noch %.1f GB frei)" % (free / 1024 ** 3))


# ---------------------------------------------------------------- Ledger ----

class Ledger:
    def __init__(self, con, source):
        self.con = con
        self.source = source
        self.known = {k for (k,) in con.execute("SELECT key FROM imports WHERE source=?", (source,))}

    def has(self, key):
        return key in self.known

    def put(self, key, status, path=None, tag=None, album=None):
        self.known.add(key)
        self.con.execute("INSERT OR REPLACE INTO imports(source, key, status, path, tag, album, at, applied) "
                         "VALUES(?,?,?,?,?,?,?,0)", (self.source, key, status, path, tag, album,
                                                     datetime.datetime.now().isoformat(timespec="seconds")))
        # sofort abschließen: sonst bliebe der Katalog während des nächsten (evtl. minutenlangen)
        # Downloads gesperrt und das Einlesen müsste warten
        self.con.commit()


# -------------------------------------------------------- Ordner und ZIPs ----

class Entry:
    """Eine Mediendatei aus Ordner oder ZIP."""

    def __init__(self, key, name, size, dirname, opener, sidecar=None, container=None):
        self.key, self.name, self.size, self.dirname = key, name, size, dirname
        self.opener = opener            # liefert (lokaler Pfad, temporär?)
        self.sidecar = sidecar          # liefert JSON-Bytes (Google Takeout) oder None
        self.container = container


def _json_index(names):
    """Google-Takeout-Begleitdateien je Ordner: Name -> Liste JSON-Dateinamen."""
    by_dir = {}
    for n in names:
        if n.lower().endswith(".json"):
            d, _, b = n.rpartition("/")
            by_dir.setdefault(d, []).append(b)
    return by_dir


def _find_json(jsons, base):
    """Begleitdatei zu einer Mediendatei finden (Takeout kürzt und nummeriert Namen eigenwillig)."""
    if not jsons:
        return None
    low = {j.lower(): j for j in jsons}
    b = base.lower()
    for cand in (b + ".json", b + ".supplemental-metadata.json"):
        if cand in low:
            return low[cand]
    m = re.match(r"^(.*)\((\d+)\)(\.[^.]+)$", base)
    if m:  # "IMG_1(1).jpg" -> "IMG_1.jpg(1).json" / "IMG_1.jpg.supplemental-metadata(1).json"
        stem = (m.group(1) + m.group(3)).lower()
        for j in low:
            if j.startswith(stem[:40]) and "(%s)" % m.group(2) in j:
                return low[j]
    for j in low:  # gekürzte Namen
        if len(b) > 30 and j.startswith(b[:40]) and j.endswith(".json"):
            return low[j]
    stem = os.path.splitext(b)[0]
    for cand in (stem + ".json",):
        if cand in low:
            return low[cand]
    return None


def iter_folder_source(root):
    """Alle Mediendateien in einem Ordner (inkl. darin liegender ZIPs) oder in einer ZIP-Datei."""
    if os.path.isfile(root) and root.lower().endswith(".zip"):
        yield from _iter_zip(root)
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in TRASH_DIRS
                       and os.path.abspath(os.path.join(dirpath, d)) != os.path.abspath(common.BASE_DIR)]
        jsons = [f for f in filenames if f.lower().endswith(".json")]
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if fn.lower().endswith(".zip"):
                yield from _iter_zip(full)
                continue
            if fn.startswith("._") or not kind_for_ext(fn.rsplit(".", 1)[-1].lower() if "." in fn else ""):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            j = _find_json(jsons, fn)
            sidecar = (lambda p=os.path.join(dirpath, j): open(p, "rb").read()) if j else None
            yield Entry("f:%s|%d" % (common.norm(os.path.abspath(full)), size), fn, size,
                        os.path.basename(dirpath), lambda p=full: (p, False), sidecar)


def _iter_zip(zpath):
    try:
        z = zipfile.ZipFile(zpath)
    except (zipfile.BadZipFile, OSError):
        JOB.note("Kein gültiges ZIP: " + os.path.basename(zpath))
        JOB.errors += 1
        return
    names = z.namelist()
    jsons = _json_index(names)
    for info in z.infolist():
        n = info.filename
        if info.is_dir():
            continue
        d, _, base = n.rpartition("/")
        parts = [p.lower() for p in d.split("/")]
        if any(p in TRASH_DIRS for p in parts) or base.startswith("._"):
            continue
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        if not kind_for_ext(ext):
            continue

        def opener(info=info, ext=ext):
            os.makedirs(TMP_DIR, exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix="." + ext, prefix="fa_imp_", dir=TMP_DIR)
            with os.fdopen(fd, "wb") as out, z.open(info) as src:
                shutil.copyfileobj(src, out, 1 << 20)
            return tmp, True
        j = _find_json(jsons.get(d), base)
        sidecar = (lambda jn=(d + "/" + j if d else j): z.read(jn)) if j else None
        # Schlüssel ohne ZIP-Namen: mehrteilige Takeouts (…-001.zip, …-002.zip) und spätere Exporte
        # enthalten dieselben Pfade
        yield Entry("z:%s|%d" % (common.norm(n), info.file_size), base, info.file_size,
                    d.rpartition("/")[2], opener, sidecar, container=os.path.basename(zpath))


def parse_takeout_json(data):
    try:
        j = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return {}
    out = {}
    ts = (j.get("photoTakenTime") or {}).get("timestamp") or (j.get("creationTime") or {}).get("timestamp")
    if ts:
        try:
            out["taken"] = datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            pass
    for key in ("geoDataExif", "geoData"):
        g = j.get(key) or {}
        if g.get("latitude") or g.get("longitude"):
            out["lat"], out["lon"] = float(g["latitude"]), float(g["longitude"])
            break
    if j.get("description"):
        out["caption"] = j["description"]
    return out


def import_folder(root, label=None, albums=None, dry=False):
    """Ordner/ZIP importieren. albums=None: bei Google Takeout automatisch Alben aus Ordnernamen."""
    JOB.reset("Import: " + os.path.basename(root.rstrip("\\/")))
    JOB.running = True
    if not dry:
        _set_resume({"type": "folder", "path": root, "label": label, "albums": albums})
    con = connect()
    try:
        JOB.phase = "Dateien suchen"
        entries = list(iter_folder_source(root))
        is_takeout = any(e.sidecar for e in entries[:2000]) or "takeout" in root.lower()
        if albums is None:
            albums = is_takeout
        label = label or ("Google Fotos" if is_takeout else os.path.basename(root.rstrip("\\/")) or root)
        source = "folder:" + label
        tag = "Import " + label
        JOB.total = len(entries)
        JOB.note("%d Mediendateien gefunden%s" % (len(entries), " (Google Takeout)" if is_takeout else ""))
        if not entries:
            JOB.message = "Keine Fotos oder Videos gefunden."
            return
        JOB.phase = "Abgleich mit der Bibliothek" + (" (Vorschau)" if dry else "")
        lib = LibraryIndex(con)
        ledger = Ledger(con, source)
        last_commit = time.time()
        for e in entries:
            if JOB.stop:
                break
            JOB.done += 1
            if ledger.has(e.key):
                JOB.existing += 1
                continue
            album = e.dirname if albums and e.dirname and not YEAR_DIR.match(e.dirname) else None
            try:
                _import_entry(e, lib, ledger, tag, album, dry)
            except Exception as ex:  # eine kaputte Datei stoppt den Import nicht
                JOB.errors += 1
                JOB.note("Fehler bei %s: %s" % (e.name, ex))
            if time.time() - last_commit > 2:
                con.commit()
                last_commit = time.time()
        con.commit()
        JOB.message = ("Vorschau: %d neu, %d schon vorhanden" if dry else "Fertig: %d neu kopiert, %d schon vorhanden") % (
            JOB.new, JOB.existing)
        JOB.note(JOB.message)
    finally:
        con.close()
        if not dry:
            _set_resume(None)
        _finish(dry)


def _import_entry(e, lib, ledger, tag, album, dry):
    kind = kind_for_ext(e.name.rsplit(".", 1)[-1].lower())
    side = parse_takeout_json(e.sidecar()) if e.sidecar else {}
    # Schnelltest ohne Auspacken: gleicher Name und gleiche Größe
    quick = lib.find(e.name, e.size, kind)
    if quick:
        JOB.existing += 1
        if not dry:
            ledger.put(e.key, "exists", quick, tag, album)
        return
    local, temp = e.opener()
    try:
        try:
            m = file_meta(local, kind)
        except Exception:
            m = {"taken": None, "w": None, "h": None, "phash": None}
        taken = m["taken"] or side.get("taken")
        match = lib.find(e.name, e.size, kind, taken, m["w"], m["h"], m["phash"])
        if match:
            JOB.existing += 1
            if not dry:
                ledger.put(e.key, "exists", match, tag, album)
            return
        JOB.new += 1
        if dry:
            return
        _check_space(e.size)
        dest = dest_path(taken, e.name)
        if temp:
            shutil.move(local, dest)
            local = None
        else:
            shutil.copy2(local, dest)
        if taken and not m["taken"]:
            ts = time.mktime(_ts(taken).timetuple())
            os.utime(dest, (ts, ts))
        need_date = side.get("taken") if not m["taken"] else None
        need_gps = side.get("lat") is not None and not m.get("lat")
        if need_date or need_gps:
            write_sidecar(dest, need_date, side.get("lat") if need_gps else None, side.get("lon") if need_gps else None)
        rel = to_rel(dest)
        lib.add(rel, os.path.basename(dest), kind, e.size, taken, m["w"], m["h"], m["phash"])
        ledger.put(e.key, "imported", rel, tag, album)
        JOB.bytes += e.size
        if JOB.new <= 30 or JOB.new % 50 == 0:
            JOB.note("Neu: %s → %s" % (e.name, rel.rsplit("/", 1)[0]))
    finally:
        if temp and local and os.path.exists(local):
            os.remove(local)


def _finish(dry):
    JOB.running = False
    JOB.finished = time.time()
    JOB.phase = ""
    if not dry and (JOB.new or JOB.existing):
        import indexer

        # neue Dateien einlesen; läuft der Index gerade, wird danach nochmal gelaufen
        if not indexer.start_background():
            indexer.PROGRESS.rerun = True


def apply_ledger(con):
    """Nach dem Einlesen: Herkunft als Stichwort und Takeout-Alben an die Fotos hängen."""
    rows = con.execute("SELECT source, key, path, tag, album, status FROM imports "
                       "WHERE applied=0 AND path IS NOT NULL").fetchall()
    if not rows:
        return
    ids = {p: i for i, p in con.execute("SELECT id, path FROM items")}
    albums = {}
    for aid, name in con.execute("SELECT id, name FROM albums WHERE parent IS NULL"):
        albums.setdefault(name, aid)
    done, changed = [], set()
    for source, key, path, tag, album, status in rows:
        iid = ids.get(path)
        if iid is None:
            continue
        # Herkunft nur bei wirklich neu kopierten Fotos vermerken
        if tag and status == "imported":
            cur = (con.execute("SELECT usertags FROM items WHERE id=?", (iid,)).fetchone() or [None])[0]
            tags = [t for t in (cur or "").split(", ") if t]
            if tag.lower() not in {t.lower() for t in tags}:
                tags.append(tag)
                con.execute("UPDATE items SET usertags=? WHERE id=?", (", ".join(tags), iid))
        if album:
            aid = albums.get(album)
            if aid is None:
                aid = albums[album] = con.execute("INSERT INTO albums(name, source) VALUES(?, 'import')",
                                                  (album,)).lastrowid
            con.execute("INSERT OR IGNORE INTO album_items(album_id, item_id) VALUES(?,?)", (aid, iid))
        changed.add(iid)
        done.append((source, key))
    con.executemany("UPDATE imports SET applied=1 WHERE source=? AND key=?", done)
    con.commit()
    return sorted(changed)


# ------------------------------------------------------------------ iCloud ----

_sessions = {}   # Konto-ID -> angemeldeter PyiCloudService (nur im Speicher)
_pending = {}    # Konto-ID -> Dienst, der noch auf den 2FA-Code wartet


def _accounts():
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_accounts(accs):
    os.makedirs(ICLOUD_DIR, exist_ok=True)
    with open(ACCOUNTS_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(accs, f, ensure_ascii=False, indent=2)
    os.replace(ACCOUNTS_FILE + ".tmp", ACCOUNTS_FILE)


def _account_id(apple_id):
    return re.sub(r"[^a-z0-9]+", "_", apple_id.lower()).strip("_")


def _cookie_dir(acc_id):
    d = os.path.join(ICLOUD_DIR, acc_id)
    os.makedirs(d, exist_ok=True)
    return d


def accounts():
    out = []
    for a in _accounts():
        a = dict(a)
        a["connected"] = a["id"] in _sessions
        out.append(a)
    return out


def _service(acc_id, apple_id, password=None):
    from pyicloud_ipd.base import PyiCloudService

    return PyiCloudService("com", apple_id, lambda: password, cookie_directory=_cookie_dir(acc_id))


def login(apple_id, password, label):
    """Anmelden. Antwort: 'ok' oder 'code' (2FA-Code nötig). Das Passwort wird nicht gespeichert."""
    from pyicloud_ipd.exceptions import PyiCloudFailedLoginException

    acc_id = _account_id(apple_id)
    try:
        api = _service(acc_id, apple_id, password)
    except PyiCloudFailedLoginException:
        return {"status": "error", "error": "Anmeldung fehlgeschlagen – Apple-ID oder Passwort falsch?"}
    accs = [a for a in _accounts() if a["id"] != acc_id]
    old = next((a for a in _accounts() if a["id"] == acc_id), {})
    accs.append({"id": acc_id, "apple_id": apple_id, "label": label or old.get("label") or apple_id,
                 "last_run": old.get("last_run"), "since": old.get("since")})
    _save_accounts(accs)
    if api.requires_2fa:
        _pending[acc_id] = {"api": api, "sms": None}
        return {"status": "code", "id": acc_id, "phones": _phones(api), "pushed": _push(api)}
    if getattr(api, "requires_2sa", False):
        return {"status": "error", "error": "Dieses Konto nutzt noch die alte „Bestätigung in zwei Schritten“. "
                                            "Bitte in den Apple-ID-Einstellungen auf Zwei-Faktor-Authentifizierung umstellen."}
    _sessions[acc_id] = api
    return {"status": "ok", "id": acc_id}


def _push(api):
    # Apple verschickt den Code an die Geräte erst nach ausdrücklicher Anforderung
    try:
        return bool(api.trigger_push_notification())
    except Exception:
        return False


def _phones(api):
    try:
        return [{"id": d.id, "number": d.obfuscated_number} for d in api.get_trusted_phone_numbers()]
    except Exception:
        return []


def resend_code(acc_id, phone_id=None):
    """Code nochmal an die Geräte schicken oder per SMS an eine hinterlegte Nummer."""
    p = _pending.get(acc_id)
    if p is None:
        return {"status": "error", "error": "Bitte zuerst neu anmelden."}
    if phone_id is None:
        p["sms"] = None
        return {"status": "code", "id": acc_id, "pushed": _push(p["api"])}
    if not p["api"].send_2fa_code_sms(int(phone_id)):
        return {"status": "error", "error": "SMS konnte nicht angefordert werden."}
    p["sms"] = int(phone_id)
    return {"status": "code", "id": acc_id, "sms": True}


def submit_code(acc_id, code):
    p = _pending.get(acc_id)
    if p is None:
        return {"status": "error", "error": "Bitte zuerst neu anmelden."}
    api, code = p["api"], re.sub(r"\D", "", code or "")
    if len(code) != 6:
        return {"status": "error", "error": "Der Code hat 6 Ziffern."}
    ok = api.validate_2fa_code_sms(p["sms"], code) if p["sms"] is not None else api.validate_2fa_code(code)
    if not ok:
        return {"status": "error", "error": "Code falsch oder abgelaufen – bitte nochmal oder neuen Code anfordern."}
    _pending.pop(acc_id, None)
    _sessions[acc_id] = api
    return {"status": "ok", "id": acc_id}


def _connect(acc):
    """Gespeicherte Sitzung nutzen (hält meist 1–2 Monate)."""
    api = _sessions.get(acc["id"])
    if api is not None:
        return api
    try:
        api = _service(acc["id"], acc["apple_id"], None)
    except Exception:
        return None
    if not api.data or api.requires_2fa:
        return None
    _sessions[acc["id"]] = api
    return api


def remove_account(acc_id):
    _sessions.pop(acc_id, None)
    _pending.pop(acc_id, None)
    _save_accounts([a for a in _accounts() if a["id"] != acc_id])
    shutil.rmtree(os.path.join(ICLOUD_DIR, acc_id), ignore_errors=True)


def run_icloud(acc_id, since=None):
    """Neue Fotos aus iCloud holen (eigene und geteilte Bibliothek)."""
    from pyicloud_ipd.item_type import AssetItemType
    from pyicloud_ipd.version_size import AssetVersionSize

    acc = next((a for a in _accounts() if a["id"] == acc_id), None)
    if not acc:
        return
    JOB.reset("iCloud: " + acc["label"])
    JOB.running = True
    _set_resume({"type": "icloud", "id": acc_id, "since": since})
    con = connect()
    started = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        JOB.phase = "Verbinden"
        api = _connect(acc)
        if api is None:
            JOB.message = "Anmeldung abgelaufen – bitte bei „%s“ neu anmelden." % acc["label"]
            JOB.errors += 1
            return
        libs = api.photos.private_libraries or {"PrimarySync": api.photos}
        source = "icloud:" + acc_id
        tag = "iCloud " + acc["label"]
        ledger = Ledger(con, source)
        lib = LibraryIndex(con)
        since_dt = _ts(since + " 00:00:00") if since else None
        for zone, plib in libs.items():
            if JOB.stop:
                break
            shared = zone.startswith("SharedSync")
            album = plib.all
            JOB.phase = "Abgleich " + ("geteilte Bibliothek" if shared else "eigene Bibliothek")
            try:
                JOB.total += len(album)
            except Exception:
                pass
            JOB.note("%s: %s Einträge" % ("Geteilte Bibliothek" if shared else "Eigene Bibliothek", JOB.total))
            last_commit = time.time()
            for asset in album:
                if JOB.stop:
                    break
                JOB.done += 1
                key = asset.id
                if ledger.has(key):
                    JOB.existing += 1
                    continue
                try:
                    _icloud_asset(api, asset, ledger, lib, tag + (" (geteilt)" if shared else ""), since_dt,
                                  AssetItemType, AssetVersionSize)
                except Exception as ex:
                    JOB.errors += 1
                    JOB.note("Fehler bei %s: %s" % (getattr(asset, "id", "?"), ex))
                if time.time() - last_commit > 2:
                    con.commit()
                    last_commit = time.time()
            con.commit()
        accs = _accounts()
        for a in accs:
            if a["id"] == acc_id:
                a["last_run"] = started
                if since:
                    a["since"] = since
        _save_accounts(accs)
        JOB.message = "Fertig: %d neu geholt, %d schon vorhanden" % (JOB.new, JOB.existing)
        JOB.note(JOB.message)
    except Exception as ex:
        JOB.message = "Fehler: %s" % ex
        JOB.errors += 1
    finally:
        con.close()
        _set_resume(None)
        _finish(False)


def _icloud_asset(api, asset, ledger, lib, tag, since_dt, AssetItemType, AssetVersionSize):
    created = asset.created.replace(tzinfo=None)
    if since_dt and created < since_dt:
        JOB.existing += 1   # älter als gewünscht: zählt als "nicht nötig", wird aber nicht vermerkt
        return
    versions = asset.versions
    ver = versions.get(AssetVersionSize.ORIGINAL)
    if ver is None:
        return
    name = asset.filename
    kind = "video" if asset.item_type == AssetItemType.MOVIE else kind_for_ext(name.rsplit(".", 1)[-1].lower()) or "photo"
    taken = created.strftime("%Y-%m-%d %H:%M:%S")
    try:
        w, h = asset.dimensions
    except (KeyError, TypeError):
        w = h = None
    # Abgleich nur mit Metadaten – spart das Herunterladen von allem, was schon da ist
    match = lib.find(name, ver.size, kind, taken, w, h)
    if match:
        JOB.existing += 1
        ledger.put(asset.id, "exists", match, tag)
        return
    _check_space(ver.size)
    ext = os.path.splitext(name)[1] or "." + asset.item_type_extension.lower()
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="fa_icloud_", dir=TMP_DIR)
    try:
        with os.fdopen(fd, "wb") as out:
            resp = asset.download(api.photos.session, ver.url)
            for chunk in resp.iter_content(1 << 20):
                if JOB.stop:
                    raise InterruptedError("abgebrochen")
                out.write(chunk)
        try:
            m = file_meta(tmp, kind)
        except Exception:
            m = {"taken": None, "w": w, "h": h, "phash": None}
        file_taken = m["taken"] or taken
        match = lib.find(name, ver.size, kind, file_taken, m["w"], m["h"], m["phash"], content_required=True)
        if match:
            JOB.existing += 1
            ledger.put(asset.id, "exists", match, tag)
            return
        dest = dest_path(file_taken, name)
        shutil.move(tmp, dest)
        ts = time.mktime(_ts(file_taken).timetuple())
        os.utime(dest, (ts, ts))
        rel = to_rel(dest)
        lib.add(rel, os.path.basename(dest), kind, ver.size, file_taken, m["w"], m["h"], m["phash"])
        ledger.put(asset.id, "imported", rel, tag)
        JOB.new += 1
        JOB.bytes += ver.size
        if JOB.new <= 30 or JOB.new % 50 == 0:
            JOB.note("Neu: %s → %s" % (name, rel.rsplit("/", 1)[0]))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ------------------------------------------------------------- Starten ----

def _set_resume(info):
    """Merken, welcher Import läuft. Wird FotoArchiv mittendrin beendet (Fenster zu, Neustart, Platte
    an anderen Computer), bleibt die Datei stehen und der Import wird beim nächsten Start fortgesetzt."""
    try:
        if info is None:
            if os.path.exists(RESUME_FILE):
                os.remove(RESUME_FILE)
        else:
            with open(RESUME_FILE, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False)
    except OSError:
        pass


def resume_pending():
    """Beim Start: Reste abgebrochener Downloads löschen und einen unterbrochenen Import fortsetzen."""
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    try:
        with open(RESUME_FILE, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if info.get("type") == "icloud" and any(a["id"] == info.get("id") for a in _accounts()):
        start(run_icloud, info["id"], info.get("since"))
        return "iCloud"
    if info.get("type") == "folder" and os.path.exists(info.get("path") or ""):
        start(import_folder, info["path"], info.get("label"), info.get("albums"))
        return info["path"]
    _set_resume(None)
    return None


def start(fn, *args, **kw):
    if JOB.running:
        return False
    JOB.running = True
    threading.Thread(target=fn, args=args, kwargs=kw, daemon=True).start()
    return True
