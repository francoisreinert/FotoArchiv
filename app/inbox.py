"""Eingangsordner: Dateien hineinkopieren, FotoArchiv übernimmt sie von selbst.

<Platte>/FotoArchiv-Eingang/ wird alle EVERY Sekunden angesehen. Eine Datei gilt als fertig kopiert, wenn sich
Größe und Änderungszeit seit dem letzten Blick nicht geändert haben, sie mindestens SETTLE Sekunden alt ist und
sich zum Schreiben öffnen lässt (Windows sperrt Dateien, die gerade kopiert werden). Dann läuft der normale
Import (Abgleich mit der Bibliothek, Ablage in Bilder/<Jahr>). Neue Dateien werden in die Bibliothek
VERSCHOBEN (gleiche Platte = nur umbenennen, sofort, kein doppelter Platz), XMP/AAE daneben wandern mit.
Was schon im Archiv war, kommt nach _erledigt/<Datum>/<gleicher Pfad> – nie gelöscht; leeren tut der Nutzer.
Unterordner werden zu Alben (Eingang/Urlaub Sylt/… → Album „Urlaub Sylt“). ZIPs werden ausgepackt.
Nicht lesbare Dateien landen in _fehler/.
"""
import datetime
import os
import shutil
import threading
import time

import common
import importer
from common import kind_for_ext

NAME = "FotoArchiv-Eingang"
DONE = "_erledigt"
FAILED = "_fehler"
EVERY = 60
SETTLE = 30
SIDE_EXT = {".xmp", ".aae", ".json"}

_seen = {}       # Pfad -> (Größe, mtime) beim letzten Blick
STATE = {"last_check": None, "waiting": 0, "files": [], "last_run": None, "last_result": ""}
_lock = threading.Lock()


def enabled(cfg=None):
    return (cfg or common.load_config()).get("inbox", True)


def folder():
    return os.path.join(common.LIB_ROOT, NAME)


def ensure():
    try:
        os.makedirs(folder(), exist_ok=True)
    except OSError:
        pass


def _candidates():
    """Alle Mediendateien und ZIPs im Eingang (ohne _erledigt/_fehler, ohne versteckte)."""
    root = folder()
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath == root:
            dirnames[:] = [d for d in dirnames if d not in (DONE, FAILED)]
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext == "zip" or kind_for_ext(ext):
                out.append(os.path.join(dirpath, fn))
    return sorted(out, key=str.lower)  # Foto vor gleichnamigem Video (Live Photo)


def _writable(path):
    try:
        with open(path, "r+b"):
            return True
    except OSError:
        return False


def ready_files():
    """Fertig kopierte Dateien; merkt sich den Stand für den nächsten Blick."""
    now = time.time()
    ready, cur, names = [], {}, []
    for p in _candidates():
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = (st.st_size, int(st.st_mtime))
        cur[p] = sig
        names.append((os.path.relpath(p, folder()).replace(os.sep, "/"), st.st_size))
        if _seen.get(p) == sig and now - st.st_mtime >= SETTLE and _writable(p):
            ready.append(p)
    _seen.clear()
    _seen.update(cur)
    STATE["waiting"] = len(cur)
    STATE["files"] = names[:20]
    return ready


def _album_for(path):
    """Erster Unterordner unter dem Eingang = Album; Dateien direkt im Eingang: kein Album."""
    rel = os.path.relpath(path, folder()).replace(os.sep, "/")
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else None


def _park(path, sub):
    """Datei (+ gleichnamige XMP/AAE/JSON daneben) nach _erledigt/<Tag>/… bzw. _fehler/… verschieben."""
    rel = os.path.relpath(path, folder())
    dest = os.path.join(folder(), sub, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    base, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = "%s_%d%s" % (base, n, ext)
        n += 1
    shutil.move(path, dest)
    stem = os.path.splitext(path)[0]
    d = os.path.dirname(path)
    try:
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            low = f.lower()
            if (os.path.splitext(fp)[0].lower() in (stem.lower(), path.lower())
                    and os.path.splitext(low)[1] in SIDE_EXT):
                shutil.move(fp, os.path.join(os.path.dirname(dest), f))
    except OSError:
        pass
    return dest


def _move_sidecars(src, dest):
    """XMP/AAE (Bearbeitungen, Stichwörter) mit dem Foto in die Bibliothek verschieben, mit dessen neuem Namen."""
    stem, dstem = os.path.splitext(src)[0], os.path.splitext(dest)[0]
    d = os.path.dirname(src)
    try:
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            base, ext = os.path.splitext(fp)
            if ext.lower() in (".xmp", ".aae") and base.lower() in (stem.lower(), src.lower()):
                target = (dstem if base.lower() == stem.lower() else dest) + ext
                if not os.path.exists(target):
                    shutil.move(fp, target)
    except OSError:
        pass


def _cleanup_dirs():
    """Leer gewordene Unterordner im Eingang entfernen (nicht _erledigt/_fehler)."""
    root = folder()
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        top = os.path.relpath(dirpath, root).split(os.sep)[0]
        if top in (DONE, FAILED):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def _log(day_dir, line):
    try:
        os.makedirs(day_dir, exist_ok=True)
        with open(os.path.join(day_dir, "protokoll.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except OSError:
        pass


def _entries(path):
    """Import-Einträge für eine Datei im Eingang (eine Mediendatei oder der Inhalt eines ZIPs)."""
    st = os.stat(path)
    tag = "%d|%d" % (st.st_size, int(st.st_mtime))
    if path.lower().endswith(".zip"):
        for e in importer._iter_zip(path):
            e.key = "e:%s|%s|%s" % (common.norm(os.path.basename(path).lower()), tag, e.key)
            yield e
        return
    fn = os.path.basename(path)
    d = os.path.dirname(path)
    jsons = [f for f in os.listdir(d) if f.lower().endswith(".json")]
    j = importer._find_json(jsons, fn)
    sidecar = (lambda p=os.path.join(d, j): open(p, "rb").read()) if j else None
    yield importer.Entry("e:%s|%s" % (common.norm(fn.lower()), tag), fn, st.st_size, "",
                         lambda p=path: (p, False), sidecar)


def import_inbox(files):
    """Läuft als Import-Job (importer.start)."""
    J = importer.JOB
    J.reset("Eingangsordner")
    J.running = True
    con = common.connect()
    day = datetime.date.today().isoformat()
    day_dir = os.path.join(folder(), DONE, day)
    try:
        J.phase = "Abgleich mit der Bibliothek"
        J.total = len(files)
        lib = importer.LibraryIndex(con)
        ledger = importer.Ledger(con, "inbox")
        park = []  # erst am Ende verschieben – Live-Photo-Partner müssen bis dahin nebeneinander liegen
        for path in files:
            if J.stop:
                break
            J.done += 1
            rel = os.path.relpath(path, folder()).replace(os.sep, "/")
            album = _album_for(path)
            results = []   # (Name, Ergebnis, Pfad in der Bibliothek)
            try:
                for e in _entries(path):
                    if ledger.has(e.key):   # schon übernommen, nur das Verschieben fehlte
                        J.existing += 1
                        results.append((e.name, "schon übernommen", ""))
                        continue
                    try:
                        src = e.opener()[0] if not e.container else None
                        st, where = importer._import_entry(e, lib, ledger, "Eingang", album, False, move=True)
                        if st == "neu" and src:
                            _move_sidecars(src, common.to_abs(where))
                        results.append((e.name, st, where))
                    except Exception as ex:
                        J.errors += 1
                        results.append((e.name, "Fehler: %s" % ex, ""))
            except Exception as ex:
                J.errors += 1
                results.append((rel, "Fehler: %s" % ex, ""))
            if any("Festplatte fast voll" in r[1] for r in results):
                J.note("Festplatte fast voll – Eingang angehalten")
                break
            ok = [r for r in results if not r[1].startswith("Fehler")]
            park.append((path, DONE if ok else FAILED))
            for name, st, where in results:
                _log(day_dir, "%s%s: %s%s" % (rel, "" if name == os.path.basename(path) else " › " + name,
                                              st, " → " + where if where else ""))
            if not ok:
                _log(day_dir, "%s liegt jetzt in %s/%s" % (rel, FAILED, day))
        for path, sub in park:
            if not os.path.exists(path):   # neu übernommen = schon in die Bibliothek verschoben
                continue
            try:
                _park(path, os.path.join(sub, day))
            except OSError as ex:
                J.note("Konnte %s nicht verschieben: %s" % (os.path.basename(path), ex))
        J.message = "Eingang: %d neu, %d schon vorhanden%s" % (J.new, J.existing, ", %d Fehler" % J.errors if J.errors else "")
        J.note(J.message)
        STATE["last_result"] = J.message
        STATE["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
    finally:
        con.close()
        _cleanup_dirs()
        importer._finish(False)


def check():
    """Einmal nachsehen; startet bei fertigen Dateien einen Import. Liefert Anzahl gestarteter Dateien."""
    with _lock:
        STATE["last_check"] = datetime.datetime.now().isoformat(timespec="seconds")
        if importer.JOB.running or not os.path.isdir(folder()):
            return 0
        files = ready_files()
        if files and importer.start(import_inbox, files):
            return len(files)
        return 0


def loop():
    ensure()
    while True:
        try:
            if enabled():
                check()
        except Exception as ex:
            print(" Eingangsordner:", ex)
        time.sleep(EVERY)


def status():
    import indexer

    P = indexer.PROGRESS
    return dict(STATE, enabled=enabled(), path=folder(), interval=EVERY,
                indexing={"phase": P.phase, "done": P.done, "total": P.total} if P.running else None)
