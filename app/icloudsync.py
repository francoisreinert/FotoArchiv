"""Rückweg nach iCloud über „iCloud für Windows“ und Import der geteilten Alben.

iCloud für Windows legt ~/Pictures/iCloud Photos/Photos (eigene Bibliothek, Platzhalter) und …/Shared/<Album>
(geteilte Alben, lokal, von Apple verkleinert) an. Getestet 14.10.2026:
  - Datei in Photos legen → nach Sekunden hochgeladen (Cloud-Platzhalter, InSyncState = 1).
  - „Speicher freigeben“ (attrib -P +U) → bleibt in iCloud, lokal 0 Byte.
  - Datei in Photos LÖSCHEN → auch in iCloud gelöscht („Zuletzt gelöscht“). Deshalb löscht dieses Modul dort NIE etwas.

Ablauf je Foto (Tabelle icloud_push): wartet → kopiert (über einen Zwischenordner, damit iCloud keine halbe Datei
sieht) → hochgeladen (Windows meldet „in sync“, Speicher freigegeben). Was schon in iCloud ist (aus iCloud importiert
oder gleicher Name+Größe im Ordner), wird „vorhanden“ und nicht doppelt geschickt. Live Photos: das Video mit gleichem
Namensstamm geht mit gleichem Namen mit. Alben/Ereignisse mit Haken (Tabelle icloud_sync) schicken neue Fotos von
selbst (nie private). Nur in eine Richtung: Entfernen aus dem Album löscht nichts in iCloud.
"""
import ctypes
import datetime
import os
import shutil
import subprocess
import sys
import threading
import time

import common
from common import connect, to_abs

SCHEMA = """
CREATE TABLE IF NOT EXISTS icloud_push (item_id INTEGER PRIMARY KEY, name TEXT, state TEXT, at TEXT,
    size INTEGER, error TEXT, via TEXT);
CREATE TABLE IF NOT EXISTS icloud_sync (kind TEXT, id INTEGER, PRIMARY KEY(kind, id));
"""
EVERY = 120                 # Sekunden zwischen zwei Abgleichen
SHARED_EVERY = 3600         # geteilte Alben stündlich
MAX_INFLIGHT = 5 * 1024 ** 3   # höchstens so viel gleichzeitig „kopiert, noch nicht oben“ (Platz auf C:)
RESERVE = 10 * 1024 ** 3       # auf C: immer frei lassen
STATE = {"last": None, "message": "", "shared_last": None, "shared_result": ""}
_lock = threading.Lock()


# ------------------------------------------------------------------ Orte ----

def root():
    cfg = common.load_config()
    return cfg.get("icloud_photos") or os.path.join(os.path.expanduser("~"), "Pictures", "iCloud Photos")


def photos_dir():
    return os.path.join(root(), "Photos")


def shared_dir():
    return os.path.join(root(), "Shared")


def available():
    return sys.platform == "win32" and os.path.isdir(photos_dir())


def _staging():
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "FotoArchiv", "icloud_upload")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------ Windows-Cloud-API ----

class _Basic(ctypes.Structure):
    _fields_ = [("PinState", ctypes.c_int), ("InSyncState", ctypes.c_int), ("FileId", ctypes.c_longlong),
                ("SyncRootFileId", ctypes.c_longlong), ("FileIdentityLength", ctypes.c_ulong),
                ("FileIdentity", ctypes.c_byte * 256)]


def in_sync(path):
    """True, wenn iCloud die Datei fertig hochgeladen hat (Cloud-Platzhalter im Zustand „in sync“)."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    # FILE_READ_ATTRIBUTES, alle Freigaben, OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT
    h = k32.CreateFileW(path, 0x80, 7, None, 3, 0x02000000 | 0x00200000, None)
    if h == wintypes.HANDLE(-1).value:
        return False
    try:
        b, ret = _Basic(), ctypes.c_ulong()
        hr = ctypes.WinDLL("cldapi").CfGetPlaceholderInfo(wintypes.HANDLE(h), 0, ctypes.byref(b), ctypes.sizeof(b),
                                                          ctypes.byref(ret))
        return (hr & 0xFFFFFFFF) == 0 and b.InSyncState == 1
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def free_space(path):
    """„Speicher freigeben“: Datei bleibt in iCloud und als Platzhalter sichtbar, belegt lokal nichts mehr."""
    subprocess.run(["attrib", "-P", "+U", path], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# --------------------------------------------------------------- Auswahl ----

def _con():
    con = connect()
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _partners(con, ids):
    """Live Photos: Video mit gleichem Namensstamm im selben Ordner."""
    out = set()
    for iid in ids:
        row = con.execute("SELECT folder, name, kind FROM items WHERE id=?", (iid,)).fetchone()
        if not row or row[2] not in ("photo", "raw"):
            continue
        stem = row[1].rsplit(".", 1)[0]
        for (vid,) in con.execute("SELECT id FROM items WHERE folder=? AND kind='video' AND COALESCE(hidden,0)<>2 "
                                  "AND (name LIKE ? ESCAPE '\\')", (row[0], stem.replace("_", "\\_").replace("%", "\\%") + ".%")):
            out.add(vid)
    return out


def queue(ids, via="hand", con=None):
    """Fotos zum Hochladen vormerken (bereits vorgemerkte bleiben unverändert). Liefert die Anzahl neuer Einträge."""
    own = con is None
    con = con or _con()
    try:
        ids = {int(i) for i in ids}
        ids |= _partners(con, ids)
        n = 0
        for iid in ids:
            cur = con.execute("INSERT OR IGNORE INTO icloud_push(item_id, state, at, via) VALUES(?, 'wartet', ?, ?)",
                              (iid, _now(), via))
            n += cur.rowcount
        con.commit()
        return n
    finally:
        if own:
            con.close()


def set_flag(kind, oid, on):
    con = _con()
    try:
        if on:
            con.execute("INSERT OR IGNORE INTO icloud_sync(kind, id) VALUES(?, ?)", (kind, int(oid)))
        else:
            con.execute("DELETE FROM icloud_sync WHERE kind=? AND id=?", (kind, int(oid)))
        con.commit()
    finally:
        con.close()


def _auto_ids(con):
    """Alle nicht privaten Fotos/Videos der Alben/Ereignisse mit iCloud-Haken."""
    ok = "COALESCE(i.hidden,0) IN (0,3,4) AND i.dup_of IS NULL AND COALESCE(i.priv_eff,0)=0"
    ids = set()
    for kind, oid in con.execute("SELECT kind, id FROM icloud_sync").fetchall():
        if kind == "album":
            ids |= {r[0] for r in con.execute(
                "SELECT i.id FROM album_items ai JOIN items i ON i.id=ai.item_id WHERE ai.album_id=? AND " + ok, (oid,))}
        elif kind == "event":
            ev = con.execute("SELECT start, end FROM events WHERE id=?", (oid,)).fetchone()
            if ev:
                ids |= {r[0] for r in con.execute(
                    "SELECT i.id FROM items i WHERE i.taken >= ? AND i.taken <= ? AND i.raw_of IS NULL AND " + ok,
                    (ev[0][:10], (ev[1] or ev[0])[:10] + " 23:59:59"))}
    return ids


# --------------------------------------------------------------- Abgleich ----

def _names():
    """Was liegt schon im iCloud-Ordner? {name klein: Größe} (Platzhalter melden ihre echte Größe)."""
    out = {}
    try:
        for e in os.scandir(photos_dir()):
            if e.is_file():
                out[e.name.lower()] = e.stat().st_size
    except OSError:
        pass
    return out


def _from_icloud(con):
    return {r[0] for r in con.execute("SELECT path FROM imports WHERE source LIKE 'icloud:%' AND path IS NOT NULL")}


def _dest_name(names, name, iid, stem=None):
    base, ext = os.path.splitext(name)
    base = stem or base
    cand = base + ext
    if cand.lower() in names:
        cand = "%s_FA%d%s" % (base, iid, ext)
    return cand


def step():
    """Ein Abgleich: Haken-Alben einsammeln, Wartende kopieren, Hochgeladene freigeben."""
    if not available():
        return
    with _lock:
        con = _con()
        try:
            auto = _auto_ids(con)
            if auto:
                known = {r[0] for r in con.execute("SELECT item_id FROM icloud_push")}
                if auto - known:
                    queue(auto - known, "auto", con)
            names = _names()
            pdir = photos_dir()
            # 1. Hochgeladene erkennen → Speicher freigeben
            for iid, name in con.execute("SELECT item_id, name FROM icloud_push WHERE state='kopiert'").fetchall():
                p = os.path.join(pdir, name)
                if not os.path.exists(p):
                    con.execute("UPDATE icloud_push SET state='fehler', error=? WHERE item_id=?",
                                ("Datei im iCloud-Ordner fehlt (gelöscht?)", iid))
                elif in_sync(p):
                    free_space(p)
                    con.execute("UPDATE icloud_push SET state='hochgeladen', at=? WHERE item_id=?", (_now(), iid))
            con.commit()
            # 2. Wartende kopieren (Fotos vor Videos, damit Live-Photo-Videos den Namen des Fotos bekommen)
            inflight = con.execute("SELECT COALESCE(SUM(size),0) FROM icloud_push WHERE state='kopiert'").fetchone()[0]
            from_ic = _from_icloud(con)
            rows = con.execute(
                "SELECT q.item_id, i.path, i.name, i.size, i.kind, i.folder FROM icloud_push q JOIN items i ON i.id=q.item_id "
                "WHERE q.state='wartet' ORDER BY i.kind='video', i.taken").fetchall()
            stems = {}  # (Ordner, Stamm) -> Zielstamm des Fotos (Live Photos)
            for iid, path, name, size, kind, folder in rows:
                if path in from_ic or names.get(name.lower()) == size:
                    con.execute("UPDATE icloud_push SET state='vorhanden', at=? WHERE item_id=?", (_now(), iid))
                    continue
                src = to_abs(path)
                if not os.path.exists(src):
                    con.execute("UPDATE icloud_push SET state='fehler', error='Datei nicht erreichbar' WHERE item_id=?", (iid,))
                    continue
                if inflight + size > MAX_INFLIGHT and inflight:
                    break   # erst warten, bis iCloud das Bisherige hochgeladen hat
                if shutil.disk_usage(pdir).free < size + RESERVE:
                    STATE["message"] = "Zu wenig Platz auf dem Laufwerk des iCloud-Ordners – warte"
                    break
                key = (folder, name.rsplit(".", 1)[0].lower())
                dest = _dest_name(names, name, iid, stems.get(key) if kind == "video" else None)
                tmp = os.path.join(_staging(), dest)
                try:
                    shutil.copy2(src, tmp)
                    shutil.move(tmp, os.path.join(pdir, dest))   # erst fertig kopiert in den iCloud-Ordner
                except OSError as ex:
                    con.execute("UPDATE icloud_push SET state='fehler', error=? WHERE item_id=?", (str(ex), iid))
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    continue
                if kind in ("photo", "raw"):
                    stems[key] = os.path.splitext(dest)[0]
                names[dest.lower()] = size
                inflight += size
                con.execute("UPDATE icloud_push SET state='kopiert', name=?, size=?, at=?, error=NULL WHERE item_id=?",
                            (dest, size, _now(), iid))
                con.commit()
            con.commit()
            STATE["last"] = _now()
        finally:
            con.close()


def retry_errors():
    con = _con()
    try:
        con.execute("UPDATE icloud_push SET state='wartet', error=NULL WHERE state='fehler'")
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------- geteilte Alben ----

def _shared_new():
    """Gibt es im Shared-Ordner Dateien, die der Import noch nicht kennt? (sonst gar nicht erst starten)"""
    import importer

    con = _con()
    try:
        led = importer.Ledger(con, "folder:" + SHARED_LABEL)
        for e in importer.iter_folder_source(shared_dir()):
            if not led.has(e.key):
                return True
        return False
    finally:
        con.close()


SHARED_LABEL = "iCloud geteilte Alben"


def import_shared(force=False):
    """Geteilte Alben importieren (jeder Unterordner = Album). Startet nur, wenn es Neues gibt."""
    import importer

    if not os.path.isdir(shared_dir()) or importer.JOB.running:
        return False
    STATE["shared_last"] = _now()
    if not force and not _shared_new():
        STATE["shared_result"] = "nichts Neues"
        return False
    STATE["shared_result"] = "Import gestartet"
    return importer.start(importer.import_folder, shared_dir(), SHARED_LABEL, True)


def loop():
    last_shared = 0
    while True:
        try:
            if available():
                step()
                if common.load_config().get("icloud_shared_import") and time.time() - last_shared > SHARED_EVERY:
                    last_shared = time.time()
                    import_shared()
        except Exception as ex:
            STATE["message"] = "Fehler: %s" % ex
            print(" iCloud-Abgleich:", ex)
        time.sleep(EVERY)


def status():
    out = {"available": available(), "folder": photos_dir(), "shared": shared_dir(),
           "shared_on": bool(common.load_config().get("icloud_shared_import"))} | STATE
    if sys.platform != "win32":
        out["why"] = "nur unter Windows mit „iCloud für Windows“"
    elif not out["available"]:
        out["why"] = "iCloud für Windows ist nicht eingerichtet (Ordner fehlt): " + photos_dir()
    con = _con()
    try:
        out["counts"] = dict(con.execute("SELECT state, COUNT(*) FROM icloud_push GROUP BY state").fetchall())
        out["errors"] = [{"id": i, "name": n, "error": e} for i, n, e in con.execute(
            "SELECT q.item_id, i.name, q.error FROM icloud_push q JOIN items i ON i.id=q.item_id "
            "WHERE q.state='fehler' LIMIT 20")]
        flags = []
        for kind, oid in con.execute("SELECT kind, id FROM icloud_sync").fetchall():
            row = con.execute("SELECT name FROM %s WHERE id=?" % ("albums" if kind == "album" else "events"), (oid,)).fetchone()
            flags.append({"kind": kind, "id": oid, "name": row[0] if row else "(gelöscht)"})
        out["flags"] = flags
    finally:
        con.close()
    if os.path.isdir(shared_dir()):
        out["shared_albums"] = sorted(d for d in os.listdir(shared_dir()) if os.path.isdir(os.path.join(shared_dir(), d)))
    return out


def flag_of(kind, oid):
    con = _con()
    try:
        return con.execute("SELECT 1 FROM icloud_sync WHERE kind=? AND id=?", (kind, int(oid))).fetchone() is not None
    finally:
        con.close()
