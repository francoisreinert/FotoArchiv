"""Texterkennung (OCR) für Dokumente – mit der in Windows eingebauten OCR (Windows.Media.Ocr über ocr_win.ps1),
offline, ohne Zusatzpakete. Erkannt werden alle Dokumente (items.hidden=4 oder doc_state=1, also auch Dokumentenalben).
Der Text steht in items.ocr und im Suchindex – nur lokal im Katalog, nie in der Archiv-Ansicht oder auf Webseiten.
items.ocr_ver: NULL = noch offen, VERSION = erledigt (auch wenn kein Text gefunden wurde).
Gedrehte Dokumente: ocr_win.ps1 probiert bei wenig Text die anderen Drehungen."""
import json
import os
import subprocess
import tempfile
import threading
import time

from common import connect, to_abs

VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.join(HERE, "ocr_win.ps1")
NATIVE = {"jpg", "jpeg", "png", "bmp", "gif", "tif", "tiff"}  # liest Windows direkt; Rest (HEIC, RAW …) als JPEG
DOCSET = "(hidden = 4 OR doc_state = 1)"
JOB = {"running": False, "done": 0, "total": 0, "message": "", "stop": False}
_lock = threading.Lock()
_wake = threading.Event()


def available():
    return os.name == "nt" and os.path.exists(PS)


def counts(con):
    open_, done = con.execute("SELECT SUM(ocr_ver IS NULL), SUM(ocr_ver IS NOT NULL AND COALESCE(ocr,'') <> '') "
                              "FROM items WHERE %s AND kind='photo'" % DOCSET).fetchone()
    return {"available": available(), "open": open_ or 0, "with_text": done or 0, **JOB}


def _pending(con, limit=200):
    return con.execute("SELECT id, path, ext, kind, userrot FROM items WHERE %s AND kind='photo' AND ocr_ver IS NULL "
                       "LIMIT ?" % DOCSET, (limit,)).fetchall()


def _input_file(row, tmpdir):
    iid, path, ext, kind, userrot = row
    full = to_abs(path)
    if (ext or "").lower() in NATIVE and not userrot:
        return full
    import media

    im = media.open_image(full, kind, max_side=2560, userrot=userrot or 0)[0]
    out = os.path.join(tmpdir, "%d.jpg" % iid)
    im.convert("RGB").save(out, quality=92)
    return out


def run():
    """Alle offenen Dokumente erkennen (fortsetzbar). Gibt die Zahl erkannter Fotos zurück."""
    if not available() or not _lock.acquire(blocking=False):
        return 0
    n = 0
    proc = None
    try:
        con = connect()
        JOB.update(running=True, done=0, stop=False, message="",
                   total=con.execute("SELECT COUNT(*) FROM items WHERE %s AND kind='photo' AND ocr_ver IS NULL"
                                     % DOCSET).fetchone()[0])
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", creationflags=flags)
        with tempfile.TemporaryDirectory(prefix="fa_ocr_") as tmp:
            while not JOB["stop"]:
                rows = _pending(con, 50)
                if not rows:
                    break
                res = []
                for row in rows:
                    if proc.poll() is not None:
                        raise RuntimeError("Windows-Texterkennung hat sich beendet")
                    try:
                        src = _input_file(row, tmp)
                        proc.stdin.write(src + "\n")
                        proc.stdin.flush()
                        d = json.loads(proc.stdout.readline() or "{}")
                        text = (d.get("text") or "").strip()
                    except Exception:
                        text = ""
                    res.append((text or None, VERSION, row[0]))
                    JOB["done"] += 1
                    n += 1
                con.executemany("UPDATE items SET ocr=?, ocr_ver=? WHERE id=?", res)
                con.commit()
                import indexer

                indexer.rebuild_fts(con, [r[2] for r in res])
                for f in os.listdir(tmp):
                    try:
                        os.remove(os.path.join(tmp, f))
                    except OSError:
                        pass
        JOB["message"] = "Text in %d Dokumenten erkannt" % n if n else ""
        con.close()
    except Exception as ex:
        JOB["message"] = "Texterkennung: %s" % ex
    finally:
        if proc:
            try:
                proc.stdin.close()
                proc.wait(10)
            except Exception:
                proc.kill()
        JOB["running"] = False
        _lock.release()
    return n


def start():
    """Im Hintergrund anstoßen (z. B. nach dem Einsortieren)."""
    _wake.set()


def loop():
    """Thread: alle 10 Minuten (oder sofort nach start()) offene Dokumente erkennen."""
    if not available():
        return
    time.sleep(60)
    while True:
        try:
            con = connect()
            todo = _pending(con, 1)
            con.close()
            if todo:
                run()
        except Exception:
            pass
        _wake.wait(600)
        _wake.clear()
