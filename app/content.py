"""Bildinhalt erkennen – komplett lokal, nichts verlässt den Rechner.

1. Freie Bildsuche („Fußball“, „Strand“, „Hund im Schnee“): CLIP ViT-B/32 (OpenAI) berechnet je Foto einen
   Bild-Vektor aus dem Vorschaubild; Suchwörter gehen durch das mehrsprachige Gegenstück
   (sentence-transformers clip-ViT-B-32-multilingual-v1, auch Deutsch). Treffer = höchste Kosinus-Ähnlichkeit.
   Vektoren liegen als int8 (+ Faktor) in data/content.db (eine Datei, nicht viele kleine).
2. Heikle Fotos: NudeNet 320n (YOLOv8) findet Körperstellen. items.nude: NULL = ungeprüft, 0 = nichts,
   1 = knapp bekleidet (Bikini/Unterwäsche), 2 = nackt, -1 = vom Nutzer als „nicht heikel“ markiert.
   Rohwerte je Klasse in content.db (Tabelle nude), damit Schwellen ohne neuen Lauf änderbar sind.

Läuft in einem einzigen Hilfsprozess (onnxruntime nutzt mehrere Kerne, niedrige Priorität), weil die Modelle
viel Speicher brauchen. Modelle unter models/clip/ und models/nudenet_320n.onnx (Herunterladen: download()).
"""
import io
import json
import os
import sqlite3
import struct
import threading
import time
import unicodedata

import common
from common import MODELS_DIR, connect

CLIP_DIR = os.path.join(MODELS_DIR, "clip")
VISION = os.path.join(CLIP_DIR, "vision_model.onnx")
TEXT_X86 = os.path.join(CLIP_DIR, "text_multi_q8.onnx")
TEXT_ARM = os.path.join(CLIP_DIR, "text_multi_q8_arm64.onnx")
DENSE = os.path.join(CLIP_DIR, "text_dense.safetensors")
VOCAB = os.path.join(CLIP_DIR, "vocab.txt")
NUDE = os.path.join(MODELS_DIR, "nudenet_320n.onnx")
DB = os.path.join(common.DATA_DIR, "content.db")

_HF = "https://huggingface.co/"
_ST = _HF + "sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/"
URLS = {
    VISION: _HF + "Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx",
    TEXT_X86: _ST + "onnx/model_quint8_avx2.onnx",
    TEXT_ARM: _ST + "onnx/model_qint8_arm64.onnx",
    DENSE: _ST + "2_Dense/model.safetensors",
    VOCAB: _ST + "vocab.txt",
    NUDE: "https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/320n.onnx",
}

NUDE_LABELS = ["FEMALE_GENITALIA_COVERED", "FACE_FEMALE", "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED",
               "FEMALE_GENITALIA_EXPOSED", "MALE_BREAST_EXPOSED", "ANUS_EXPOSED", "FEET_EXPOSED", "BELLY_COVERED",
               "FEET_COVERED", "ARMPITS_COVERED", "ARMPITS_EXPOSED", "FACE_MALE", "BELLY_EXPOSED",
               "MALE_GENITALIA_EXPOSED", "ANUS_COVERED", "FEMALE_BREAST_COVERED", "BUTTOCKS_COVERED"]
EXPOSED = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED",
           "ANUS_EXPOSED"}
COVERED = {"FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED"}
KEEP = EXPOSED | COVERED | {"BELLY_EXPOSED", "MALE_BREAST_EXPOSED"}  # Rohwerte, die gespeichert werden
NUDE_MIN = {2: 0.45, 1: 0.5}  # Mindestwert je Stufe (an Stichprobe abgestimmt)

JOB = {"running": False, "stop": False, "done": 0, "total": 0, "nude": 0, "started": None, "message": ""}
_lock = threading.Lock()


def text_model():
    import platform

    return TEXT_ARM if platform.machine().lower() in ("arm64", "aarch64") else TEXT_X86


def available():
    return all(os.path.exists(p) for p in (VISION, text_model(), DENSE, VOCAB, NUDE)) and _ort() is not None


def missing():
    return [os.path.basename(p) for p in (VISION, text_model(), DENSE, VOCAB, NUDE) if not os.path.exists(p)]


def _ort():
    try:
        import onnxruntime

        return onnxruntime
    except ImportError:
        return None


def ensure_runtime():
    """onnxruntime nachinstallieren, falls die Laufzeit älter ist (liegt in runtime/wheels-<Plattform>)."""
    if _ort() is not None:
        return True
    import platform
    import subprocess
    import sys

    plat = "win-x64" if os.name == "nt" else ("mac-arm64" if platform.machine() == "arm64" else "mac-x64")
    wheels = os.path.join(common.BASE_DIR, "runtime", "wheels-" + plat)
    if not os.path.isdir(wheels):
        return False
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-warn-script-location",
                        "--disable-pip-version-check", "-q", "--find-links", wheels, "onnxruntime"],
                       capture_output=True, text=True)
    import importlib

    importlib.invalidate_caches()
    return r.returncode == 0 and _ort() is not None


def download(progress=None):
    """Fehlende Modelle herunterladen (~620 MB)."""
    import urllib.request

    os.makedirs(CLIP_DIR, exist_ok=True)
    for path, url in URLS.items():
        if os.path.exists(path) or (path in (TEXT_X86, TEXT_ARM) and path != text_model()):
            continue
        if progress:
            progress(os.path.basename(path))
        if "github.com" in url:  # Release-Datei über die API (der direkte Link leitet manchmal zur Anmeldung)
            api = "https://api.github.com/repos/notAI-tech/NudeNet/releases/tags/v3.4-weights"
            with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "FotoArchiv"})) as r:
                asset = [a for a in json.load(r)["assets"] if a["name"] == "320n.onnx"][0]
            req = urllib.request.Request(asset["url"], headers={"User-Agent": "FotoArchiv",
                                                                "Accept": "application/octet-stream"})
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "FotoArchiv"})
        tmp = path + ".part"
        with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(tmp, path)


def db():
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS vec(id INTEGER PRIMARY KEY, v BLOB, s REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS nude(id INTEGER PRIMARY KEY, cls TEXT)")
    return con


# ------------------------------------------------------------------ Bilder ----

def _clip_input(img):
    """CLIP-Vorverarbeitung: kürzere Seite 224 (bikubisch), Mitte 224×224, normalisieren."""
    import numpy as np
    from PIL import Image

    img = img.convert("RGB")
    w, h = img.size
    s = 224 / min(w, h)
    img = img.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = img.size
    l, t = (w - 224) // 2, (h - 224) // 2
    a = np.asarray(img.crop((l, t, l + 224, t + 224)), np.float32) / 255.0
    a = (a - (0.48145466, 0.4578275, 0.40821073)) / (0.26862954, 0.26130258, 0.27577711)
    return a.transpose(2, 0, 1).astype(np.float32)


def _nude_input(img):
    """NudeNet: rechts/unten auf ein Quadrat auffüllen, 320×320, 0..1."""
    import numpy as np
    from PIL import Image

    img = img.convert("RGB")
    m = max(img.size)
    sq = Image.new("RGB", (m, m))
    sq.paste(img, (0, 0))
    a = np.asarray(sq.resize((320, 320), Image.BILINEAR), np.float32) / 255.0
    return a.transpose(2, 0, 1)


def _nude_classes(out):
    """Höchster Wert je Klasse (≥ 0,2) aus der YOLO-Ausgabe (22 × N)."""
    sc = out[4:, :]  # 18 Klassen × N
    best = sc.max(axis=1)
    return {NUDE_LABELS[k]: round(float(v), 3) for k, v in enumerate(best) if v >= 0.2 and NUDE_LABELS[k] in KEEP}


def nude_level(cls):
    if any(cls.get(c, 0) >= NUDE_MIN[2] for c in EXPOSED):
        return 2, max(cls.get(c, 0) for c in EXPOSED)
    if any(cls.get(c, 0) >= NUDE_MIN[1] for c in COVERED):
        return 1, max(cls.get(c, 0) for c in COVERED)
    return 0, 0.0


_w = {}


def _init(threads):
    ort = _ort()
    o = ort.SessionOptions()
    o.intra_op_num_threads = threads
    o.inter_op_num_threads = 1
    _w["vis"] = ort.InferenceSession(VISION, o, providers=["CPUExecutionProvider"])
    _w["nude"] = ort.InferenceSession(NUDE, o, providers=["CPUExecutionProvider"])
    _w["thumbs"] = connect(common.THUMBS_DB, common.BLOB_SCHEMA)
    try:  # niedrige Priorität: der Rechner bleibt bedienbar
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except (AttributeError, OSError):
        pass


def _work(ids):
    """Für eine Gruppe Fotos: [(id, int8-Vektor-Bytes, Faktor, nude-Klassen-JSON)]."""
    import numpy as np
    from PIL import Image

    imgs, ok = [], []
    for i in ids:
        row = _w["thumbs"].execute("SELECT data FROM blobs WHERE id=?", (i,)).fetchone()
        if not row:
            continue
        try:
            im = Image.open(io.BytesIO(row[0]))
            im.load()
            imgs.append(im)
            ok.append(i)
        except Exception:
            pass
    oks = set(ok)
    out = [(i, None, None, None) for i in ids if i not in oks]
    if not ok:
        return out
    emb = _w["vis"].run(None, {"pixel_values": np.stack([_clip_input(im) for im in imgs])})[0]
    emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9
    nud = _w["nude"].run(None, {"images": np.stack([_nude_input(im) for im in imgs])})[0]
    for k, i in enumerate(ok):
        v = emb[k]
        s = float(np.abs(v).max()) / 127.0 or 1.0
        q = np.clip(np.round(v / s), -127, 127).astype(np.int8)
        out.append((i, q.tobytes(), s, json.dumps(_nude_classes(nud[k]), separators=(",", ":"))))
    return out


def counts(con):
    c = db()
    try:
        done = c.execute("SELECT COUNT(*) FROM vec").fetchone()[0]
    finally:
        c.close()
    # wie die Listen: ohne Duplikat-Kopien (folgen dem Hauptfoto); private extra (Haken „auch schon private zeigen“)
    r = dict(con.execute("SELECT (COALESCE(nude, -9) * 10 + (nude_fix IS NOT NULL AND nude > 0)) * 2 "
                         "+ (COALESCE(priv_eff,0) != 0 AND nude_fix IS NULL), COUNT(*) FROM items "
                         "WHERE COALESCE(hidden,0) != 2 AND kind IN ('photo','raw','video') AND raw_of IS NULL "
                         "AND dup_of IS NULL GROUP BY 1").fetchall())
    # nackt/knapp = Vorschläge, noch zu entscheiden; ok = von dir bestätigt
    return {"done": done, "nackt": r.get(40, 0), "knapp": r.get(20, 0), "nackt_priv": r.get(41, 0),
            "knapp_priv": r.get(21, 0), "ok": r.get(42, 0) + r.get(22, 0),
            "todo": r.get(-180, 0) + r.get(-179, 0),
            "available": available(), "missing": missing(), "runtime": _ort() is not None}


def scan():
    """Alle noch nicht geprüften Fotos/Videos (Vorschaubild) untersuchen. False, wenn es schon läuft."""
    if JOB["running"]:
        return False
    if not available():
        raise RuntimeError("Modelle für die Bildinhalt-Erkennung fehlen: " + ", ".join(missing()))
    JOB.update(running=True, stop=False, done=0, total=0, nude=0, started=time.time(), message="")
    threading.Thread(target=_run, daemon=True).start()
    return True


def _run():
    import multiprocessing as mp

    con = connect()
    cdb = db()
    pool = None
    try:
        have = {r[0] for r in cdb.execute("SELECT id FROM vec")} | {r[0] for r in cdb.execute("SELECT id FROM nude")}
        ids = [r[0] for r in con.execute("SELECT id FROM items WHERE kind IN ('photo','raw','video') "
                                         "AND COALESCE(hidden,0) != 2 AND raw_of IS NULL AND has_thumb=1 ORDER BY id DESC")
               if r[0] not in have]
        JOB["total"] = len(ids)
        if not ids:
            JOB["message"] = "Alles ist schon geprüft."
            return
        threads = max(1, min(6, (os.cpu_count() or 2) - 2))
        pool = mp.get_context("spawn").Pool(1, initializer=_init, initargs=(threads,))
        chunks = [ids[k:k + 32] for k in range(0, len(ids), 32)]
        for res in pool.imap(_work, chunks):
            cdb.executemany("INSERT OR REPLACE INTO vec(id, v, s) VALUES(?,?,?)",
                            [(i, v, s) for i, v, s, _n in res if v is not None])
            cdb.executemany("INSERT OR REPLACE INTO nude(id, cls) VALUES(?,?)",
                            [(i, n or "{}") for i, _v, _s, n in res])
            cdb.commit()
            upd = []
            for i, _v, _s, n in res:
                lvl, sc = nude_level(json.loads(n)) if n else (0, 0.0)
                upd.append((lvl, sc, i))
                JOB["nude"] += lvl > 0
            for attempt in range(30):  # Katalog kurz belegt: warten statt abbrechen
                try:
                    con.executemany("UPDATE items SET nude=?, nude_score=? WHERE id=? AND COALESCE(nude,0) != -1 "
                                    "AND nude_fix IS NULL", upd)
                    con.commit()
                    break
                except sqlite3.OperationalError as ex:
                    if "locked" not in str(ex) or attempt == 29:
                        raise
                    con.rollback()
                    time.sleep(2)
            JOB["done"] += len(res)
            if JOB["stop"]:
                JOB["message"] = "Angehalten."
                break
        else:
            JOB["message"] = "Fertig."
    except Exception as ex:
        import traceback

        traceback.print_exc()
        JOB["message"] = "Fehler: %s" % ex
    finally:
        if pool:
            pool.terminate()
        cdb.close()
        JOB["running"] = False


def relevel(con):
    """Stufen aus den gespeicherten Rohwerten neu setzen (nach Änderung von NUDE_MIN); -1 (vom Nutzer) bleibt."""
    c = db()
    upd = [(*nude_level(json.loads(n)), i) for i, n in c.execute("SELECT id, cls FROM nude")]
    c.close()
    con.executemany("UPDATE items SET nude=?, nude_score=? WHERE id=? AND COALESCE(nude,0) != -1 AND nude_fix IS NULL", upd)
    con.commit()


# Zeitleiste/Fernseher: heikle Fotos weglassen (Teilindex items_nude – „nude <> 0“ muss wörtlich drinstehen)
SENSITIVE_IDS = "SELECT id FROM items WHERE nude <> 0 AND nude > 0 AND nude_fix = 1"  # nur von dir bestätigte


def _with_copies(con, ids):
    """Dazu die Duplikat-Kopien (dup_of) – sie zeigen dasselbe Bild und folgen der Entscheidung."""
    ids = [int(i) for i in ids]
    out = list(ids)
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        out += [r[0] for r in con.execute("SELECT id FROM items WHERE dup_of IN (%s)" % ",".join(map(str, part)))]
    return out


def sync_copies(con):
    """Kopien übernehmen die Entscheidung ihres Hauptfotos (einmalig/nachträglich)."""
    n = con.execute("UPDATE items SET nude = (SELECT k.nude FROM items k WHERE k.id = items.dup_of), nude_fix = 1 "
                    "WHERE dup_of IS NOT NULL AND nude_fix IS NULL AND "
                    "(SELECT k.nude_fix FROM items k WHERE k.id = items.dup_of) = 1").rowcount
    con.commit()
    return n


def set_level(con, ids, level=None):
    """Von Hand bestätigt: level 1 knapp bekleidet, 2 nackt, None = erkannte Stufe bestätigen.
    Bleibt bei weiteren Läufen und relevel(); nur bestätigte blendet der Schalter/Fernseher aus."""
    ids = _with_copies(con, ids)
    if level is None:
        con.executemany("UPDATE items SET nude_fix=1 WHERE id=? AND nude > 0", [(i,) for i in ids])
    else:
        con.executemany("UPDATE items SET nude=?, nude_fix=1 WHERE id=?", [(int(level), i) for i in ids])
    con.commit()


def not_nude(con, ids, on=True):
    """„Nicht heikel“ (bleibt bei weiteren Läufen so) bzw. wieder zurück auf die erkannte Stufe."""
    ids = _with_copies(con, ids)
    con.executemany("UPDATE items SET nude=?, nude_fix=? WHERE id=?",
                    [(-1 if on else None, 1 if on else None, i) for i in ids])
    con.commit()
    if not on:
        c = db()
        rows = {i: n for i, n in c.execute("SELECT id, cls FROM nude WHERE id IN (%s)" % ",".join(str(int(x)) for x in ids))}
        c.close()
        con.executemany("UPDATE items SET nude=?, nude_score=? WHERE id=?",
                        [(*nude_level(json.loads(rows[i])), i) for i in ids if i in rows])
        con.commit()


# ------------------------------------------------------------------ Suche ----

_VEC = {}  # Zwischenspeicher: ids, Matrix (int8), Faktoren
_TXT = {}


def _vectors():
    import numpy as np

    c = db()
    try:
        n = c.execute("SELECT COUNT(*), MAX(rowid) FROM vec").fetchone()
        if _VEC.get("key") != n:
            rows = c.execute("SELECT id, v, s FROM vec").fetchall()
            _VEC.clear()
            _VEC["key"] = n
            _VEC["ids"] = np.fromiter((r[0] for r in rows), np.int64, len(rows))
            _VEC["m"] = np.frombuffer(b"".join(r[1] for r in rows), np.int8).reshape(len(rows), 512) if rows \
                else np.zeros((0, 512), np.int8)
            _VEC["s"] = np.fromiter((r[2] for r in rows), np.float32, len(rows))
    finally:
        c.close()
    return _VEC


def _vocab():
    if "vocab" not in _TXT:
        with open(VOCAB, encoding="utf-8") as f:
            _TXT["vocab"] = {w.rstrip("\n"): k for k, w in enumerate(f)}
    return _TXT["vocab"]


def _is_punct(ch):
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def tokenize(text):
    """BERT-WordPiece (multilingual, Groß-/Kleinschreibung bleibt) → Token-Nummern mit [CLS] … [SEP]."""
    voc = _vocab()
    words = []
    for w in unicodedata.normalize("NFC", text).split():
        cur = ""
        for ch in w:
            cp = ord(ch)
            if _is_punct(ch) or 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
                if cur:
                    words.append(cur)
                words.append(ch)
                cur = ""
            elif unicodedata.category(ch) not in ("Cc", "Cf"):
                cur += ch
        if cur:
            words.append(cur)
    ids = [voc["[CLS]"]]
    for w in words:
        start, sub = 0, []
        while start < len(w):
            end = len(w)
            while end > start:
                piece = ("##" if start else "") + w[start:end]
                if piece in voc:
                    sub.append(voc[piece])
                    break
                end -= 1
            if end == start:
                sub = [voc["[UNK]"]]
                break
            start = end
        ids += sub
    return ids[:126] + [voc["[SEP]"]]


def _dense():
    import numpy as np

    if "dense" not in _TXT:
        b = open(DENSE, "rb").read()
        n = struct.unpack("<Q", b[:8])[0]
        meta = json.loads(b[8:8 + n])["linear.weight"]
        a, e = meta["data_offsets"]
        _TXT["dense"] = np.frombuffer(b[8 + n + a:8 + n + e], np.float32).reshape(meta["shape"])
    return _TXT["dense"]


def text_vector(q):
    import numpy as np

    ort = _ort()
    if "sess" not in _TXT:
        o = ort.SessionOptions()
        o.intra_op_num_threads = 2
        _TXT["sess"] = ort.InferenceSession(text_model(), o, providers=["CPUExecutionProvider"])
    _TXT["used"] = time.time()
    ids = np.array([tokenize(q)], np.int64)
    mask = np.ones_like(ids)
    h = _TXT["sess"].run(None, {"input_ids": ids, "attention_mask": mask})[0][0]
    v = h.mean(axis=0) @ _dense().T
    return v / (np.linalg.norm(v) + 1e-9)


def unload_idle(sec=600):
    """Textmodell nach längerer Pause wieder freigeben (Speicher)."""
    if "sess" in _TXT and time.time() - _TXT.get("used", 0) > sec:
        _TXT.pop("sess", None)


def search(q, limit=3000):
    """[(id, Ähnlichkeit)] absteigend. Mehrere Begriffe mit „,“ = beliebiger davon (höchster Wert zählt)."""
    import numpy as np

    with _lock:
        V = _vectors()
        if not len(V["ids"]):
            return []
        parts = [x.strip() for x in q.split(",") if x.strip()] or [q]
        tv = np.stack([text_vector(x) for x in parts]).astype(np.float32)
    sim = np.empty(len(V["ids"]), np.float32)
    step = 20000
    for k in range(0, len(V["ids"]), step):
        sim[k:k + step] = (V["m"][k:k + step].astype(np.float32) @ tv.T).max(axis=1) * V["s"][k:k + step]
    top = np.argsort(-sim)[:limit]
    return [(int(V["ids"][j]), round(float(sim[j]), 4)) for j in top]
