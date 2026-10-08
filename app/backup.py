"""Sicherung der Bibliothek auf S3 (Amazon S3 oder eigener S3-Speicher wie StorageGRID/MinIO) und Wiederherstellen.

- Ziele (Zugangsdaten) in data/backup.json – data/ wird nie weitergegeben; der geheime Schlüssel geht nie zurück
  an die Oberfläche.
- Gesichert werden alle Dateien der eingelesenen Ordner (auch XMP/AAE-Begleitdateien) mit gleicher Ordnerstruktur:
  <Präfix><Pfad relativ zur Bibliothek>. Ohne FotoArchiv lesbar. Dazu FotoArchiv-Katalog/ (Katalog-Kopie,
  config.json, mylio.json; Tageskopien der letzten 10 Läufe).
- Inkrementell: Tabelle backup_files merkt Größe+Änderungszeit je Ziel; zu Beginn wird mit der Liste im Bucket
  abgeglichen (z. B. neuer Rechner/Katalog). Gelöschte Dateien bleiben in der Sicherung (Schutz vor Versehen).
- Große Dateien in Teilen (Multipart); Teile stehen in backup_mp, ein abgebrochener Upload läuft nach Neustart
  an derselben Stelle weiter. Läuft im Hintergrund, "Anhalten" jederzeit, setzt nach Neustart fort.
"""
import datetime
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import threading
import time

import common
import s3
from common import DATA_DIR, connect, to_abs, to_rel

CFG_FILE = os.path.join(DATA_DIR, "backup.json")
CAT_DIR = "FotoArchiv-Katalog/"
SINGLE_MAX = 64 * 1024 * 1024  # bis hier ein einzelner PUT, darüber Multipart
KEEP_CATALOGS = 10
SKIP_DIRS = {"fotoarchiv-papierkorb", "$recycle.bin", "system volume information", "@eadir", "#recycle", "#snapshot",
             ".spotlight-v100", ".fseventsd", ".trashes", ".temporaryitems", ".documentrevisions-v100"}
SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_files (target TEXT, rel TEXT, size INTEGER, mtime REAL, done TEXT,
    PRIMARY KEY (target, rel)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS backup_mp (target TEXT, rel TEXT, upload_id TEXT, part_size INTEGER, size INTEGER,
    mtime REAL, parts TEXT, PRIMARY KEY (target, rel)) WITHOUT ROWID;
"""
_lock = threading.Lock()


# ---------------------------------------------------------------- Einstellungen ----

def load():
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("targets", [])
    d.setdefault("backup", {})
    b = d["backup"]
    for k, v in (("target", ""), ("videos", True), ("dups", True), ("previews", False), ("limit_mbit", 0),
                 ("auto_hours", 0), ("workers", 4), ("mirror_deletes", False)):
        b.setdefault(k, v)
    return d


def save(d):
    with _lock:
        tmp = CFG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CFG_FILE)


def public(d=None):
    """Einstellungen für die Oberfläche – ohne geheimen Schlüssel."""
    d = d or load()
    out = json.loads(json.dumps(d))
    for t in out["targets"]:
        t["secret_set"] = bool(t.pop("secret_key", ""))
    return out


def target(tid, d=None):
    for t in (d or load())["targets"]:
        if t["id"] == tid:
            return t
    raise KeyError("Sicherungsziel nicht gefunden")


def client(t):
    return s3.Client(t.get("endpoint", ""), t.get("region", ""), t["bucket"], t["access_key"], t["secret_key"],
                     {"auto": None, "path": True, "virtual": False}.get(t.get("addressing", "auto")),
                     verify=t.get("verify", True), ca_file=t.get("ca_file", ""), sig=t.get("sig", "v4"))


def prefix_of(t):
    p = (t.get("prefix") or "").strip().strip("/")
    return p + "/" if p else ""


def save_target(data):
    """Ziel anlegen/ändern. Leerer secret_key beim Ändern = alten behalten."""
    d = load()
    fields = ("name", "endpoint", "region", "bucket", "prefix", "access_key", "addressing", "verify", "ca_file",
              "storage_class", "sse", "sig")
    if data.get("id"):
        t = target(data["id"], d)
    else:
        t = {"id": secrets.token_hex(4)}
        d["targets"].append(t)
    for k in fields:
        if k in data:
            t[k] = data[k]
    if data.get("secret_key"):
        t["secret_key"] = data["secret_key"]
    if not t.get("bucket") or not t.get("access_key") or not t.get("secret_key"):
        raise ValueError("Bucket, Zugangsschlüssel und geheimer Schlüssel sind nötig")
    t["name"] = t.get("name") or t["bucket"]
    if not d["backup"].get("target"):
        d["backup"]["target"] = t["id"]
    save(d)
    return t["id"]


def delete_target(tid):
    d = load()
    d["targets"] = [t for t in d["targets"] if t["id"] != tid]
    if d["backup"].get("target") == tid:
        d["backup"]["target"] = d["targets"][0]["id"] if d["targets"] else ""
    save(d)
    con = connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM backup_files WHERE target=?", (tid,))
    con.execute("DELETE FROM backup_mp WHERE target=?", (tid,))
    con.commit()
    con.close()


def test_target(data):
    """Verbindung prüfen (mit den eingegebenen oder gespeicherten Daten); wirft bei Fehlern mit Klartext."""
    t = dict(target(data["id"])) if data.get("id") else {}
    t.update({k: v for k, v in data.items() if v not in (None, "") or k in ("verify",)})
    if not t.get("secret_key"):
        raise ValueError("Geheimer Schlüssel fehlt")
    c = client(t)
    try:
        c.check()
    except s3.S3Error as ex:
        if "Hostname mismatch" in str(ex) or "doesn't match" in str(ex):
            return cert_help(c)
        if ex.code in ("SignatureDoesNotMatch", "AuthorizationHeaderMalformed", "InvalidAccessKeyId", "AccessDenied"):
            if t.get("sig", "v4") != "v2":  # ältere Zugänge (S3 Browser: "Signature V2")
                try:
                    client(dict(t, sig="v2")).check()
                    return {"ok": True, "url": c.public_url(prefix_of(t)), "sig": "v2"}
                except s3.S3Error:
                    pass
            if not (t.get("region") or "").strip() and t.get("sig", "v4") != "v2":
                found = _try_regions(t)
                if found:
                    return {"ok": True, "url": c.public_url(prefix_of(t)), "region": found}
        if ex.code == "SignatureDoesNotMatch":
            raise ValueError(signature_help(c, ex)) from None
        raise ValueError(explain(ex)) from None
    return {"ok": True, "url": c.public_url(prefix_of(t))}


REGIONS = ("us-east-1", "eu-central-1", "eu-west-1", "us-west-2")


def _try_regions(t):
    """Region unbekannt: übliche Regionen durchprobieren (StorageGRID: Region aus der Grid-Konfiguration)."""
    for reg in REGIONS[1:]:  # us-east-1 (Standard bei leerer Region) wurde schon versucht
        try:
            client(dict(t, region=reg)).check()
            return reg
        except s3.S3Error:
            continue
    return None


def signature_help(c, ex):
    """SignatureDoesNotMatch genauer erklären: S3 schickt oft mit, wie es die Anfrage gesehen hat."""
    import xml.etree.ElementTree as ET

    mine = c.last_signed()
    theirs = {}
    try:
        x = s3._strip_ns(ET.fromstring(ex.body or b"<x/>"))
        theirs = {k: x.findtext(k) for k in ("CanonicalRequest", "StringToSign")}
    except ET.ParseError:
        pass
    base = "Der Server lehnt die Anmeldung ab (SignatureDoesNotMatch)."
    if mine and theirs.get("CanonicalRequest"):
        a, b = mine[0].split("\n"), theirs["CanonicalRequest"].split("\n")
        if a == b:
            return base + " Die Anfrage kam unverändert an – also ist der geheime Schlüssel falsch. Bitte direkt aus " \
                          "dem S3 Browser kopieren und einfügen (nicht vom Passwortmanager ausfüllen lassen)."
        diff = [("FotoArchiv: %s | Server sah: %s" % (p, q)) for p, q in zip(a, b) if p != q]
        return base + " Unterwegs wurde die Anfrage verändert (Proxy/Load-Balancer?): " + "; ".join(diff[:3])
    return base + " Meist ist der geheime Schlüssel nicht ganz richtig (bitte aus dem S3 Browser kopieren und " \
                  "einfügen, nicht vom Passwortmanager ausfüllen lassen); sonst unter „Erweitert“ die Region " \
                  "aus dem S3 Browser eintragen. Probiert wurden: " + ", ".join(REGIONS) + "."


def cert_help(c):
    """Zertifikat lautet auf einen anderen Namen (z. B. Alias-Adresse): passenden Namen suchen, der auf denselben
    Server zeigt (*.firma.com → <erster Teil>.firma.com), sonst "Namen nicht prüfen" anbieten."""
    import socket

    host, _, port = c.host.partition(":")
    port = int(port or (443 if c.scheme == "https" else 80))
    try:
        names = s3.cert_names(host, port)
    except OSError:
        names = []
    suggest = None
    try:
        ip = socket.gethostbyname(host)
        first = host.split(".")[0]
        for n in names:
            cand = first + n[1:] if n.startswith("*.") else n
            if cand != host and "*" not in cand:
                try:
                    if socket.gethostbyname(cand) == ip:
                        suggest = "%s://%s%s" % (c.scheme, cand, (":%d" % port) if port not in (443, 80) else "")
                        break
                except OSError:
                    pass
    except OSError:
        pass
    return {"ok": False, "cert_mismatch": True, "names": names, "suggest": suggest,
            "error": "Das Zertifikat des Servers lautet auf %s, nicht auf %s." % (", ".join(names) or "einen anderen Namen", host)}


def explain(ex):
    code = getattr(ex, "code", "")
    hints = {"NoSuchBucket": "Den Bucket gibt es nicht (Name prüfen oder im Speicher anlegen).",
             "AccessDenied": "Zugriff verweigert – Schlüssel oder Rechte des Benutzers prüfen.",
             "InvalidAccessKeyId": "Zugangsschlüssel unbekannt.",
             "SignatureDoesNotMatch": "Geheimer Schlüssel falsch (oder Region passt nicht).",
             "AuthorizationHeaderMalformed": "Region passt nicht zum Bucket.",
             "PermanentRedirect": "Bucket liegt in einer anderen Region.",
             "RequestTimeTooSkewed": "Uhrzeit des Computers weicht zu stark ab.",
             "Netzwerk": "Server nicht erreichbar (Adresse/Port, Netz, Zertifikat?)."}
    msg = str(ex)
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return "Zertifikat wird nicht anerkannt – eigene Zertifizierungsstelle angeben oder Prüfung ausschalten. (%s)" % msg
    return (hints.get(code, "") + " " + msg).strip()


# ---------------------------------------------------------------- Fortschritt ----

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset("")

    def reset(self, kind):
        self.kind = kind  # "backup" | "restore" | "catalog"
        self.running = False
        self.stop = False
        self.phase = ""
        self.files_total = self.files_done = self.files_skip = 0
        self.bytes_total = self.bytes_done = 0
        self.started = None
        self.current = []
        self.errors = []
        self.message = ""
        self.finished = None
        self._rate = []

    def add_bytes(self, n):
        with self.lock:
            self.bytes_done += n

    def err(self, msg):
        with self.lock:
            self.errors.append(msg)
            self.errors = self.errors[-200:]

    def snapshot(self):
        with self.lock:
            d = {k: getattr(self, k) for k in ("kind", "running", "phase", "files_total", "files_done", "files_skip",
                                               "bytes_total", "bytes_done", "message", "finished")}
            d["current"] = list(self.current)[:6]
            d["errors"] = self.errors[-20:]
            d["error_count"] = len(self.errors)
            now = time.time()
            self._rate.append((now, self.bytes_done))
            self._rate = [r for r in self._rate if now - r[0] <= 60]
            if self.running and len(self._rate) > 1 and now - self._rate[0][0] >= 3:
                rate = (self._rate[-1][1] - self._rate[0][1]) / (now - self._rate[0][0])
                d["rate"] = rate
                if rate > 0 and self.bytes_total:
                    d["eta"] = (self.bytes_total - self.bytes_done) / rate
            return d


JOB = State()


class Limiter:
    """Gemeinsames Tempolimit (Bytes/s) für alle Upload-Threads."""

    def __init__(self, mbit):
        self.rate = (mbit or 0) * 125000
        self.lock = threading.Lock()
        self.t = time.time()
        self.allow = 0.0

    def take(self, n):
        if not self.rate:
            return
        with self.lock:
            now = time.time()
            self.allow = min(self.rate, self.allow + (now - self.t) * self.rate)
            self.t = now
            self.allow -= n
            wait = -self.allow / self.rate if self.allow < 0 else 0
        if wait:
            time.sleep(wait)


# ---------------------------------------------------------------- Sicherung ----

def _local_files(cfg, con):
    """(rel, abs, size, mtime) aller zu sichernden Dateien."""
    skip_rel = set()
    if not cfg["dups"]:
        skip_rel = {r[0] for r in con.execute("SELECT path FROM items WHERE dup_of IS NOT NULL AND COALESCE(hidden,0) != 2")}
    out = []
    for top in common.included_folders():
        root = to_abs(top)
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            if JOB.stop:
                return out
            dn[:] = [d for d in dn if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for f in fn:
                if f.startswith(".") or f.lower() in SKIP_FILES or f.endswith(".fa-part"):
                    continue
                ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                if not cfg["videos"] and ext in common.VIDEO_EXT:
                    continue
                full = os.path.join(dp, f)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = to_rel(full)
                if rel in skip_rel:
                    continue
                out.append((rel, full, st.st_size, st.st_mtime))
        JOB.phase = "Dateien zusammenstellen (%s Dateien)" % format(len(out), ",").replace(",", ".")
    return out


def _headers(t, rel, mtime):
    h = {"x-amz-meta-mtime": "%.3f" % mtime}
    ctype = mimetypes.guess_type(rel)[0]
    if ctype:
        h["Content-Type"] = ctype
    if t.get("storage_class"):
        h["x-amz-storage-class"] = t["storage_class"]
    if t.get("sse"):
        h["x-amz-server-side-encryption"] = "AES256"
    return h


def _upload_file(c, t, tid, key, rel, full, size, mtime, lim, con):
    if size <= SINGLE_MAX:
        with open(full, "rb") as f:
            data = f.read()
        if len(data) != size:
            raise OSError("Datei hat sich während des Lesens geändert")
        lim.take(len(data))
        c.put(key, data, _headers(t, rel, mtime))
        JOB.add_bytes(len(data))
        return
    row = con.execute("SELECT upload_id, part_size, size, mtime, parts FROM backup_mp WHERE target=? AND rel=?",
                      (tid, rel)).fetchone()
    parts = {}
    if row and row[2] == size and abs((row[3] or 0) - mtime) < 1:
        uid, ps = row[0], row[1]
        try:
            remote = c.mp_parts(key, uid)
            parts = {int(n): e for n, e in json.loads(row[4] or "[]") if int(n) in remote}
        except s3.S3Error as ex:
            if ex.code not in ("NoSuchUpload", "NoSuchKey") and ex.status != 404:
                raise
            row = None
    else:
        if row:
            c.mp_abort(key, row[0])
        row = None
    if not row:
        ps = c.part_size(size)
        uid = c.mp_create(key, _headers(t, rel, mtime))
        con.execute("INSERT OR REPLACE INTO backup_mp VALUES (?,?,?,?,?,?,?)", (tid, rel, uid, ps, size, mtime, "[]"))
        con.commit()
    JOB.add_bytes(sum(min(ps, size - (n - 1) * ps) for n in parts))
    n_parts = (size + ps - 1) // ps
    with open(full, "rb") as f:
        for n in range(1, n_parts + 1):
            if n in parts:
                continue
            if JOB.stop:
                raise InterruptedError
            f.seek((n - 1) * ps)
            data = f.read(ps)
            lim.take(len(data))
            parts[n] = c.mp_part(key, uid, n, data)
            JOB.add_bytes(len(data))
            con.execute("UPDATE backup_mp SET parts=? WHERE target=? AND rel=?",
                        (json.dumps(sorted(parts.items())), tid, rel))
            con.commit()
    if os.path.getsize(full) != size:
        raise OSError("Datei hat sich während des Hochladens geändert")
    c.mp_complete(key, uid, sorted(parts.items()))
    con.execute("DELETE FROM backup_mp WHERE target=? AND rel=?", (tid, rel))
    con.commit()


def _upload_catalog(c, t, pfx, cfg):
    """Katalog (konsistente Kopie per SQLite-Backup), Einstellungen und Tageskopie hochladen."""
    JOB.phase = "Katalog sichern"
    snap = os.path.join(DATA_DIR, "catalog-sicherung.tmp")
    src = sqlite3.connect(common.CATALOG_DB, timeout=60)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    lim = Limiter(0)
    con = connect()
    try:
        files = [("catalog.db", snap)]
        for name, p in (("config.json", common.CONFIG_PATH), ("mylio.json", os.path.join(DATA_DIR, "mylio.json"))):
            if os.path.exists(p):
                files.append((name, p))
        if cfg.get("previews"):
            files += [("thumbs.db", common.THUMBS_DB), ("previews.db", common.PREVIEWS_DB)]
        day = datetime.date.today().isoformat()
        for name, p in files:
            if not os.path.exists(p):
                continue
            st = os.stat(p)
            key = pfx + CAT_DIR + name
            _upload_file(c, t, t["id"], key, CAT_DIR + name, p, st.st_size, st.st_mtime, lim, con)
            if name == "catalog.db":  # Tageskopie: auf dem Server kopieren statt erneut hochladen
                c.copy(key, pfx + CAT_DIR + "Tageskopien/catalog-%s.db" % day)
        old = sorted(k for k, *_ in (x for x in c.list(pfx + CAT_DIR + "Tageskopien/") if x[0] != "prefix"))
        for k in old[:-KEEP_CATALOGS]:  # nur eigene Tageskopien aufräumen
            c.delete(k)
    finally:
        con.close()
        try:
            os.remove(snap)
        except OSError:
            pass


def run_backup():
    d = load()
    cfg = d["backup"]
    t = target(cfg["target"], d)
    tid = t["id"]
    pfx = prefix_of(t)
    c = client(t)
    con = connect()
    con.executescript(SCHEMA)
    JOB.phase = "Verbindung prüfen"
    c.check()
    JOB.phase = "Dateien zusammenstellen"
    files = _local_files(cfg, con)
    if JOB.stop:
        return
    JOB.phase = "Abgleich mit der Sicherung"
    remote = {}
    for x in c.list(pfx):
        if x[0] != "prefix":
            remote[x[0]] = x[1]
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT rel, size, mtime FROM backup_files WHERE target=?", (tid,))}
    todo, now = [], datetime.datetime.now().isoformat(timespec="seconds")
    adopt = []
    for rel, full, size, mtime in files:
        key = pfx + rel
        k = known.get(rel)
        if remote.get(key) == size and (k is None or (k[0] == size and abs((k[1] or 0) - mtime) < 2)):
            if k is None:
                adopt.append((tid, rel, size, mtime, now))
            continue
        todo.append((rel, full, size, mtime))
    if adopt:
        con.executemany("INSERT OR REPLACE INTO backup_files VALUES (?,?,?,?,?)", adopt)
        con.commit()
    JOB.files_skip = len(files) - len(todo)
    JOB.files_total = len(todo)
    JOB.bytes_total = sum(x[2] for x in todo)
    JOB.phase = "Hochladen"
    lim = Limiter(cfg.get("limit_mbit") or 0)
    queue = list(reversed(todo))
    qlock = threading.Lock()

    def worker():
        wc = connect()
        wcl = client(t)
        try:
            while not JOB.stop:
                with qlock:
                    if not queue:
                        return
                    rel, full, size, mtime = queue.pop()
                    JOB.current.append(rel)
                try:
                    _upload_file(wcl, t, tid, pfx + rel, rel, full, size, mtime, lim, wc)
                    for attempt in range(20):
                        try:
                            wc.execute("INSERT OR REPLACE INTO backup_files VALUES (?,?,?,?,?)",
                                       (tid, rel, size, mtime, datetime.datetime.now().isoformat(timespec="seconds")))
                            wc.commit()
                            break
                        except sqlite3.OperationalError:
                            time.sleep(1)
                    with JOB.lock:
                        JOB.files_done += 1
                except InterruptedError:
                    return
                except (OSError, s3.S3Error) as ex:
                    JOB.err("%s: %s" % (rel, explain(ex) if isinstance(ex, s3.S3Error) else ex))
                    with JOB.lock:
                        JOB.files_done += 1
                finally:
                    with JOB.lock:
                        if rel in JOB.current:
                            JOB.current.remove(rel)
        finally:
            wc.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(8, int(cfg.get("workers") or 4))))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if JOB.stop:
        JOB.message = "Angehalten – %d von %d Dateien hochgeladen. Weiter mit „Sicherung starten“." % (
            JOB.files_done, JOB.files_total)
        return
    _upload_catalog(c, t, pfx, cfg)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", ("last_backup_" + tid, stamp))
    con.commit()
    con.close()
    errs = len(JOB.errors)
    JOB.message = "Fertig: %d Dateien hochgeladen, %d waren schon gesichert%s." % (
        JOB.files_done - errs, JOB.files_skip, (", %d Fehler" % errs) if errs else "")


def _runner(fn, kind, *args):
    try:
        fn(*args)
    except s3.S3Error as ex:
        JOB.message = "Fehler: " + explain(ex)
    except Exception as ex:  # noqa: BLE001 – in der Oberfläche anzeigen
        JOB.message = "Fehler: %s" % ex
    finally:
        JOB.running = False
        JOB.finished = time.time()
        JOB.current = []
        if kind == "backup":  # nur wenn FotoArchiv mitten drin beendet wird, bleibt "resume" gesetzt
            d = load()
            d["backup"]["resume"] = False
            d["backup"]["last_message"] = JOB.message
            save(d)


def start(kind, fn, *args):
    if JOB.running:
        return False
    JOB.reset(kind)
    JOB.running = True
    JOB.started = time.time()
    if kind == "backup":
        d = load()
        d["backup"]["resume"] = True  # bleibt gesetzt, falls FotoArchiv mitten drin beendet wird
        save(d)
    threading.Thread(target=_runner, args=(fn, kind) + args, daemon=True).start()
    return True


def start_backup():
    d = load()
    if not d["backup"].get("target") or not d["targets"]:
        raise ValueError("Erst ein Sicherungsziel anlegen")
    return start("backup", run_backup)


def stop():
    JOB.stop = "user"


def status():
    d = load()
    st = JOB.snapshot()
    tid = d["backup"].get("target")
    if tid:
        con = connect()
        con.executescript(SCHEMA)
        st["last_backup"] = (con.execute("SELECT value FROM meta WHERE key=?", ("last_backup_" + tid,)).fetchone()
                             or [None])[0]
        st["saved_files"], st["saved_bytes"] = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM backup_files WHERE target=?", (tid,)).fetchone()
        con.close()
    st["last_message"] = d["backup"].get("last_message", "")
    return st


def resume_pending():
    """Beim Start: unterbrochene Sicherung fortsetzen; automatische Sicherung planen."""
    d = load()
    if d["backup"].get("resume") and d["targets"]:
        start_backup()
        return True
    return False


def auto_loop():
    """Automatisch alle n Stunden sichern (nur Neues), wenn eingestellt und nichts anderes läuft."""
    import indexer

    while True:
        time.sleep(600)
        try:
            d = load()
            hours = float(d["backup"].get("auto_hours") or 0)
            tid = d["backup"].get("target")
            if not hours or not tid or JOB.running or indexer.PROGRESS.running:
                continue
            con = connect()
            last = (con.execute("SELECT value FROM meta WHERE key=?", ("last_backup_" + tid,)).fetchone() or [None])[0]
            con.close()
            if not last or (datetime.datetime.now() - datetime.datetime.fromisoformat(last)).total_seconds() > hours * 3600:
                start_backup()
        except Exception:
            pass


# ---------------------------------------------------------------- Gelöschtes ----

def on_purge(con, rels):
    """Nach dem endgültigen Löschen: mit Option "mirror_deletes" auch in allen Sicherungen löschen (im Hintergrund),
    sonst nur zählen, wie viele davon weiterhin in einer Sicherung liegen."""
    con.executescript(SCHEMA)
    hits = []
    for k in range(0, len(rels), 500):
        ch = rels[k:k + 500]
        hits += con.execute("SELECT target, rel FROM backup_files WHERE rel IN (%s)" % ",".join("?" * len(ch)), ch).fetchall()
    if not hits:
        return {}
    d = load()
    if not d["backup"].get("mirror_deletes"):
        return {"only_backup": len({r for _t, r in hits})}

    def work():
        wc = connect()
        try:
            for tid in {t for t, _r in hits}:
                try:
                    t = target(tid, d)
                except KeyError:
                    continue
                c = client(t)
                for _t, rel in [x for x in hits if x[0] == tid]:
                    try:
                        c.delete(prefix_of(t) + rel)
                        wc.execute("DELETE FROM backup_files WHERE target=? AND rel=?", (tid, rel))
                    except s3.S3Error:
                        pass  # bleibt verzeichnet, "Aufräumen" erwischt es später
                wc.commit()
        finally:
            wc.close()
    threading.Thread(target=work, daemon=True).start()
    return {"mirrored": len({r for _t, r in hits})}


def orphans(tid):
    """Gesicherte Dateien, die es in der Bibliothek nicht mehr gibt (weder im Katalog noch auf der Platte)."""
    con = connect()
    try:
        con.executescript(SCHEMA)
        present = {r[0] for r in con.execute("SELECT path FROM items WHERE COALESCE(hidden,0) != 2")}
        present |= {r[0] for r in con.execute("SELECT trash_from FROM items WHERE hidden=2")}  # noch zurückholbar
        out, size = [], 0
        for rel, sz in con.execute("SELECT rel, size FROM backup_files WHERE target=?", (tid,)):
            if rel in present or os.path.exists(to_abs(rel)):
                continue
            out.append(rel)
            size += sz or 0
        return {"count": len(out), "size": size, "sample": sorted(out)[:30]}
    finally:
        con.close()


def run_prune(tid):
    t = target(tid)
    c = client(t)
    rels = []
    con = connect()
    present = {r[0] for r in con.execute("SELECT path FROM items WHERE COALESCE(hidden,0) != 2")}
    present |= {r[0] for r in con.execute("SELECT trash_from FROM items WHERE hidden=2")}
    for rel, sz in con.execute("SELECT rel, size FROM backup_files WHERE target=?", (tid,)).fetchall():
        if rel not in present and not os.path.exists(to_abs(rel)):
            rels.append((rel, sz or 0))
    JOB.phase = "In der Sicherung löschen"
    JOB.files_total, JOB.bytes_total = len(rels), sum(s for _r, s in rels)
    for rel, sz in rels:
        if JOB.stop:
            break
        try:
            c.delete(prefix_of(t) + rel)
            con.execute("DELETE FROM backup_files WHERE target=? AND rel=?", (tid, rel))
        except s3.S3Error as ex:
            JOB.err("%s: %s" % (rel, explain(ex)))
        JOB.files_done += 1
        JOB.add_bytes(sz)
        if JOB.files_done % 200 == 0:
            con.commit()
    con.commit()
    con.close()
    JOB.message = "Aufgeräumt: %d Dateien in der Sicherung gelöscht." % (JOB.files_done - len(JOB.errors))


def start_prune(tid):
    return start("prune", run_prune, tid)


# ---------------------------------------------------------------- Wiederherstellen ----

def browse(tid, path=""):
    """Ordner und Dateien in der Sicherung unter path (relativ zur Bibliothek)."""
    t = target(tid)
    c = client(t)
    pfx = prefix_of(t)
    path = path.strip("/")
    base = pfx + (path + "/" if path else "")
    folders, files = [], []
    try:
        for x in c.list(base, "/"):
            if x[0] == "prefix":
                name = x[1][len(base):].rstrip("/")
                if not (not path and name + "/" == CAT_DIR):
                    folders.append(name)
            else:
                files.append({"name": x[0][len(base):], "size": x[1], "modified": x[3]})
    except s3.S3Error as ex:
        raise ValueError(explain(ex)) from None
    return {"path": path, "folders": sorted(folders, key=str.lower), "files": files[:500], "more": len(files) > 500,
            "file_count": len(files)}


def run_restore(tid, path, dest_dir):
    """Dateien unter path holen. dest_dir leer = an den Originalplatz (nur Fehlendes, nie überschreiben)."""
    t = target(tid)
    c = client(t)
    pfx = prefix_of(t)
    base = pfx + (path.strip("/") + "/" if path.strip("/") else "")
    JOB.phase = "Liste der Sicherung laden"
    todo = []
    for x in c.list(base):
        if x[0] == "prefix" or x[0].startswith(pfx + CAT_DIR):
            continue
        rel = x[0][len(pfx):]
        dest = os.path.join(dest_dir, *rel.split("/")) if dest_dir else to_abs(rel)
        if os.path.exists(dest):
            if os.path.getsize(dest) == x[1]:
                JOB.files_skip += 1
            else:
                JOB.err("%s: liegt schon mit anderem Inhalt da – nicht überschrieben" % rel)
            continue
        todo.append((x[0], rel, dest, x[1]))
    JOB.files_total = len(todo)
    JOB.bytes_total = sum(x[3] for x in todo)
    JOB.phase = "Herunterladen"
    queue = list(reversed(todo))
    qlock = threading.Lock()

    def worker():
        wcl = client(t)
        while not JOB.stop:
            with qlock:
                if not queue:
                    return
                key, rel, dest, size = queue.pop()
                JOB.current.append(rel)
            try:
                wcl.get_to_file(key, dest, size, progress=JOB.add_bytes)
                h = wcl.head(key) or {}
                mt = h.get("x-amz-meta-mtime")
                if mt:
                    os.utime(dest, (float(mt), float(mt)))
            except (OSError, s3.S3Error, ValueError) as ex:
                JOB.err("%s: %s" % (rel, explain(ex) if isinstance(ex, s3.S3Error) else ex))
            finally:
                with JOB.lock:
                    JOB.files_done += 1
                    if rel in JOB.current:
                        JOB.current.remove(rel)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    errs = len(JOB.errors)
    if JOB.stop:
        JOB.message = "Angehalten – %d von %d Dateien geholt (angefangene werden beim nächsten Mal fortgesetzt)." % (
            JOB.files_done, JOB.files_total)
    else:
        JOB.message = "Fertig: %d Dateien geholt, %d waren schon da%s.%s" % (
            JOB.files_done - errs, JOB.files_skip, (", %d Fehler" % errs) if errs else "",
            "" if dest_dir else " Jetzt „Bibliothek aktualisieren“.")


def start_restore(tid, path, dest_dir=""):
    if dest_dir:
        dest_dir = os.path.abspath(os.path.expanduser(dest_dir))
        os.makedirs(dest_dir, exist_ok=True)
    return start("restore", run_restore, tid, path, dest_dir)


def catalogs(tid):
    t = target(tid)
    c = client(t)
    pfx = prefix_of(t) + CAT_DIR
    out = []
    for x in c.list(pfx):
        if x[0] == "prefix" or not x[0].endswith(".db") or "/thumbs" in x[0] or "/previews" in x[0]:
            continue
        out.append({"key": x[0], "name": x[0][len(pfx):], "size": x[1], "modified": x[3]})
    return sorted(out, key=lambda o: o["modified"] or "", reverse=True)


RESTORE_DB = os.path.join(DATA_DIR, "catalog.restore.db")


def run_catalog(tid, key):
    t = target(tid)
    c = client(t)
    JOB.phase = "Katalog laden"
    h = c.head(key) or {}
    size = int(h.get("content-length") or 0)
    JOB.files_total, JOB.bytes_total = 1, size
    c.get_to_file(key, RESTORE_DB, size or None, progress=JOB.add_bytes)
    JOB.files_done = 1
    JOB.message = "Katalog geladen – FotoArchiv startet neu und übernimmt ihn (der bisherige bleibt als Kopie)."


def apply_catalog_restore():
    """Beim Programmstart vor dem ersten Datenbankzugriff: geladenen Katalog einsetzen."""
    if not os.path.exists(RESTORE_DB):
        return False
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    if os.path.exists(common.CATALOG_DB):
        shutil.move(common.CATALOG_DB, os.path.join(DATA_DIR, "catalog-vorher-%s.db" % stamp))
    for ext in ("-wal", "-shm"):
        if os.path.exists(common.CATALOG_DB + ext):
            os.remove(common.CATALOG_DB + ext)
    shutil.move(RESTORE_DB, common.CATALOG_DB)
    return True
