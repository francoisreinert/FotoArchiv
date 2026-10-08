"""Dokumente finden: Fotos von Briefen, Rechnungen, Formularen, Notizen, Ausweisen – und Bildschirmfotos.

Erkannt wird am gespeicherten Vorschaubild (360 px): ein kleines Textdetektions-Modell (PP-OCRv3 aus dem
OpenCV-Zoo, models/text_detection_ppocr.onnx) findet Textzeilen, gelesen wird nichts. Gezählt werden nur
zeilenartige Kästen (flach und länglich) – ein einzelner großer Kasten über einem Gesicht oder Essen zählt
nicht. Ergebnis je Foto: items.doc_lines (Anzahl Zeilen) und items.doc_area (Anteil Textfläche).
Probelauf an 3.000 Fotos: ab 14 Zeilen praktisch nur Dokumente/Bildschirmfotos, 8–13 gemischt.
"""
import io
import os
import sqlite3
import threading
import time

import common
from common import MODELS_DIR, connect

MODEL = os.path.join(MODELS_DIR, "text_detection_ppocr.onnx")
LEVELS = {"streng": 14, "normal": 10, "weit": 6}
SORTED = 4  # items.hidden: als Dokument einsortiert – nicht in Zeitleiste/Kalender, aber auf "Dokumente"
ALBUM = "Dokumente"  # behaltene Dokumente (doc_state=1)
JOB = {"running": False, "stop": False, "done": 0, "total": 0, "found": 0, "sorted": 0, "started": None, "message": ""}


def auto_level():
    """Ab so vielen Textzeilen automatisch einsortieren (config "docs_auto": streng/normal/weit/aus)."""
    return LEVELS.get(common.load_config().get("docs_auto", "streng"))


def sort_in(con, ids=None, level=None):
    """Erkannte Dokumente aus der Zeitleiste nehmen (Dateien bleiben, wo sie sind). Gibt die Anzahl zurück."""
    lv = level or auto_level()
    if not lv:
        return 0
    cond = "doc_lines >= ? AND COALESCE(doc_no,0) = 0 AND COALESCE(hidden,0) = 0"
    if ids is None:
        n = con.execute("UPDATE items SET hidden=%d WHERE %s" % (SORTED, cond), (lv,)).rowcount
    else:
        n = 0
        manual = level == "hand"  # von Hand gewählt: unabhängig von der Texterkennung
        for k in range(0, len(ids), 500):
            ch = ids[k:k + 500]
            if manual:
                n += con.execute("UPDATE items SET hidden=%d, doc_no=0 WHERE COALESCE(hidden,0)=0 AND id IN (%s)"
                                 % (SORTED, ",".join("?" * len(ch))), ch).rowcount
            else:
                n += con.execute("UPDATE items SET hidden=%d WHERE %s AND id IN (%s)"
                                 % (SORTED, cond, ",".join("?" * len(ch))), [lv] + ch).rowcount
    con.commit()
    return n


def keep(con, ids):
    """Dauerhaft behalten: ins Album "Dokumente", bleibt aus der Zeitleiste."""
    row = con.execute("SELECT id FROM albums WHERE name=? AND parent IS NULL", (ALBUM,)).fetchone()
    aid = row[0] if row else con.execute("INSERT INTO albums(name) VALUES(?)", (ALBUM,)).lastrowid
    for i in ids:
        con.execute("UPDATE items SET doc_state=1, doc_no=0, hidden=CASE WHEN COALESCE(hidden,0)=0 THEN %d ELSE hidden END "
                    "WHERE id=?" % SORTED, (i,))
        con.execute("INSERT OR IGNORE INTO album_items(album_id, item_id) VALUES(?,?)", (aid, i))
    con.commit()
    return aid


def not_doc(con, ids):
    """Fehltreffer: zurück in die Zeitleiste, wird nie wieder einsortiert."""
    con.executemany("UPDATE items SET doc_no=1, doc_state=NULL, hidden=CASE WHEN hidden=%d THEN 0 ELSE hidden END "
                    "WHERE id=?" % SORTED, [(i,) for i in ids])
    con.commit()


def counts(con):
    lv = LEVELS["normal"]
    return {
        "neu": con.execute("SELECT COUNT(*) FROM items WHERE hidden=? AND doc_state IS NULL", (SORTED,)).fetchone()[0],
        "behalten": con.execute("SELECT COUNT(*) FROM items WHERE doc_state=1 AND COALESCE(hidden,0) != 2").fetchone()[0],
        "vorschlag": con.execute("SELECT COUNT(*) FROM items WHERE doc_lines >= ? AND COALESCE(doc_no,0)=0 "
                                 "AND COALESCE(hidden,0)=0 AND dup_of IS NULL AND raw_of IS NULL", (lv,)).fetchone()[0],
        "unsorted_auto": con.execute("SELECT COUNT(*) FROM items WHERE doc_lines >= ? AND COALESCE(doc_no,0)=0 "
                                     "AND COALESCE(hidden,0)=0", (auto_level() or 999,)).fetchone()[0],
        "auto": common.load_config().get("docs_auto", "streng"),
    }
_w = {}


def available():
    return os.path.exists(MODEL)


def _init():
    import cv2

    cv2.setNumThreads(1)
    m = cv2.dnn_TextDetectionModel_DB(MODEL)
    m.setBinaryThreshold(0.3).setPolygonThreshold(0.5).setMaxCandidates(400).setUnclipRatio(2.0)
    _w["model"] = m
    _w["thumbs"] = connect(common.THUMBS_DB, common.BLOB_SCHEMA)
    try:  # niedrige Priorität: der Rechner bleibt bedienbar
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except (AttributeError, OSError):
        pass


def measure(img, model=None):
    """(Textzeilen, Textflächen-Anteil) eines PIL-Bildes."""
    import cv2
    import numpy as np

    model = model or _w["model"]
    w, h = img.size
    W, H = (544, 352) if w >= h else (352, 544)
    arr = cv2.cvtColor(np.asarray(img.convert("RGB").resize((W, H))), cv2.COLOR_RGB2BGR)
    model.setInputParams(1.0 / 255.0, (W, H), (122.67891434, 116.66876762, 104.00698793))
    boxes, _ = model.detect(arr)
    mask = np.zeros((H, W), np.uint8)
    n = 0
    for b in boxes:
        (_cx, _cy), (bw, bh), _a = cv2.minAreaRect(np.asarray(b, np.float32))
        long_, short = max(bw, bh), min(bw, bh)
        if short <= 0.08 * min(W, H) and long_ >= 2.0 * short:  # Textzeile: flach und länglich
            n += 1
            cv2.fillPoly(mask, [np.asarray(b, np.int32)], 1)
    return n, round(float(mask.mean()), 4)


def _work(ids):
    from PIL import Image

    out = []
    for i in ids:
        row = _w["thumbs"].execute("SELECT data FROM blobs WHERE id=?", (i,)).fetchone()
        if not row:
            out.append((i, 0, 0.0))  # kein Vorschaubild: nicht erneut versuchen
            continue
        try:
            n, a = measure(Image.open(io.BytesIO(row[0])))
        except Exception:
            n, a = 0, 0.0
        out.append((i, n, a))
    return out


def scan(workers=None):
    """Alle noch nicht geprüften Fotos untersuchen (im Hintergrund). Gibt False zurück, wenn es schon läuft."""
    if JOB["running"]:
        return False
    if not available():
        raise RuntimeError("Modell für die Texterkennung fehlt (models/text_detection_ppocr.onnx)")
    JOB.update(running=True, stop=False, done=0, total=0, found=0, sorted=0, started=time.time(), message="")
    threading.Thread(target=_run, args=(workers,), daemon=True).start()
    return True


def _run(workers):
    import multiprocessing as mp

    con = connect()
    pool = None
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM items WHERE kind IN ('photo','raw') AND doc_lines IS NULL "
                                         "AND COALESCE(hidden,0) != 2 AND raw_of IS NULL ORDER BY id")]
        JOB["total"] = len(ids)
        if not ids:
            JOB["message"] = "Alle Fotos sind schon geprüft."
            return
        n = workers or max(1, min(6, (os.cpu_count() or 2) - 2))
        pool = mp.get_context("spawn").Pool(n, initializer=_init)
        chunks = [ids[k:k + 100] for k in range(0, len(ids), 100)]
        for res in pool.imap_unordered(_work, chunks):
            # Katalog kurz belegt (Einlesen, Bearbeiten …): warten und erneut versuchen statt abbrechen
            for attempt in range(30):
                try:
                    con.executemany("UPDATE items SET doc_lines=?, doc_area=? WHERE id=?",
                                    [(n_, a, i) for i, n_, a in res])
                    con.commit()
                    break
                except sqlite3.OperationalError as ex:
                    if "locked" not in str(ex) or attempt == 29:
                        raise
                    con.rollback()
                    time.sleep(2)
            JOB["done"] += len(res)
            JOB["found"] += sum(1 for _i, n_, _a in res if n_ >= LEVELS["normal"])
            lv = auto_level()
            if lv:  # neue Dokumente gleich einsortieren
                hits = [i for i, n_, _a in res if n_ >= lv]
                if hits:
                    for attempt in range(30):
                        try:
                            JOB["sorted"] += sort_in(con, hits, lv)
                            break
                        except sqlite3.OperationalError:
                            con.rollback()
                            time.sleep(2)
            if JOB["stop"]:
                JOB["message"] = "Angehalten – beim nächsten Start geht es an derselben Stelle weiter."
                break
        else:
            JOB["message"] = "Fertig: %d Fotos geprüft%s." % (
                JOB["done"], ", %d Dokumente einsortiert" % JOB["sorted"] if JOB["sorted"] else "")
    except Exception as ex:  # noqa: BLE001 – in der Oberfläche anzeigen
        JOB["message"] = "Fehler: %s" % ex
    finally:
        if pool:
            pool.terminate()
        con.close()
        JOB["running"] = False


def status(con):
    st = dict(JOB)
    st["available"] = available()
    if JOB["running"] and JOB["done"] and JOB["started"]:
        rate = JOB["done"] / max(1.0, time.time() - JOB["started"])
        st["eta"] = (JOB["total"] - JOB["done"]) / rate
    st.update(counts(con))
    st["open"] = con.execute("SELECT COUNT(*) FROM items WHERE kind IN ('photo','raw') AND doc_lines IS NULL "
                             "AND COALESCE(hidden,0) != 2 AND raw_of IS NULL").fetchone()[0]
    return st
