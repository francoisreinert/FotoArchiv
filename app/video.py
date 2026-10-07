"""Videoschnitt: Projekte (Sequenzen) aus Fotos und Videos, Musik darunter, lokal als MP4 rendern (PyAV/FFmpeg).

Ein Projekt ist eine Schnittfolge: die Reihenfolge der Clips IST die Reihenfolge im Ergebnis. Videos haben
Ein-/Ausstieg (Sekunden ab Dateianfang), Fotos eine Dauer (optional mit Kamerafahrt). Übergänge an jeder
Schnittstelle: harter Schnitt, Überblenden (Clips überlappen) oder über Schwarz; dazu Ein-/Ausblenden am
Anfang und Ende des Films. Musikspuren liegen ab einer Sequenzzeit darunter.

Die Zeitachse (timeline) gibt es identisch in static/video.js – bei Änderungen beide anpassen.
Gerendert wird in einer Warteschlange (ein Auftrag nach dem anderen), Ergebnis im Ordner „FotoArchiv Export“.
„Vorschau ab hier“ rechnet einen kurzen Ausschnitt klein und schnell (preview()).
"""
import datetime
import json
import math
import os
import random
import re
import shutil
import tempfile
import threading
import time
import uuid

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

import common
from common import connect, to_abs

FORMATS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080)}
SR = 48000
MAX_MINUTES = 180  # Ton wird stückweise gemischt (AUDIO_CHUNK), Länge kostet keinen Speicher
AUDIO_CHUNK = 30 * 48000  # Samples je Mischstück
TRANSITIONS = ("cut", "fade", "black")
PREVIEW_DIR = os.path.join(tempfile.gettempdir(), "FotoArchiv-Vorschau")
PREVIEW_SIDE = 640


# ------------------------------------------------------------ Projekte ----

def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(v) else max(lo, min(hi, v))


def clean(d, con=None):
    """Vom Browser kommende Projektdaten prüfen (Werte begrenzen, unbekannte Felder weg)."""
    import share

    d = d or {}
    out = {"format": d.get("format") if d.get("format") in FORMATS else "16:9",
           "fps": 30 if d.get("fps") == 30 else 25, "uhd": bool(d.get("uhd")),
           "fadein": _num(d.get("fadein"), 0, 5, 0.0), "fadeout": _num(d.get("fadeout"), 0, 5, 1.0),
           "clips": [], "music": []}
    con = con or connect()
    for c in (d.get("clips") or [])[:2000]:
        try:
            item = int(c.get("item"))
        except (TypeError, ValueError):
            continue
        row = con.execute("SELECT kind, duration FROM items WHERE id=?", (item,)).fetchone()
        if not row:
            continue
        kind, dur = row[0], row[1] or 0
        x = {"id": str(c.get("id") or uuid.uuid4().hex[:10])[:16], "item": item,
             "kind": "video" if kind == "video" else "photo",
             "trans": c.get("trans") if c.get("trans") in TRANSITIONS else "cut",
             "tdur": _num(c.get("tdur"), 0.2, 3, 1.0)}
        if x["kind"] == "video":
            full = dur if dur > 0 else 36000
            x["in"] = _num(c.get("in"), 0, max(0, full - 0.2), 0)
            x["out"] = _num(c.get("out"), x["in"] + 0.2, full, min(full, x["in"] + 30) if not c.get("out") else full)
            x["vol"] = _num(c.get("vol"), 0, 1.5, 1.0)
        else:
            x["dur"] = _num(c.get("dur"), 0.5, 120, 4.0)
            x["kb"] = bool(c.get("kb", True))
        out["clips"].append(x)
    for m in (d.get("music") or [])[:20]:
        p = m.get("path") or ""
        if not share.music_allowed(p):
            continue
        out["music"].append({"path": p, "name": str(m.get("name") or os.path.basename(p))[:200],
                             "start": _num(m.get("start"), 0, 36000, 0), "offset": _num(m.get("offset"), 0, 36000, 0),
                             "vol": _num(m.get("vol"), 0, 1.5, 0.8), "fin": _num(m.get("fin"), 0, 20, 1.0),
                             "fout": _num(m.get("fout"), 0, 20, 3.0)})
    return out


def new_clip(con, item):
    row = con.execute("SELECT kind, duration FROM items WHERE id=?", (item,)).fetchone()
    if not row:
        return None
    if row[0] == "video":
        dur = row[1] or 10
        return {"id": uuid.uuid4().hex[:10], "item": item, "kind": "video", "in": 0, "out": min(dur, 600),
                "vol": 1.0, "trans": "fade", "tdur": 0.5}
    return {"id": uuid.uuid4().hex[:10], "item": item, "kind": "photo", "dur": 4.0, "kb": True,
            "trans": "fade", "tdur": 1.0}


def clip_len(c):
    return (c["out"] - c["in"]) if c["kind"] == "video" else c["dur"]


def timeline(d):
    """[(start, länge, überblend_rein, schwarz_rein)] je Clip und Gesamtlänge (wie vTimeline() in video.js).
    Überblenden: der Clip beginnt tdur vor dem Ende des vorigen; über Schwarz: halbe Dauer aus, halbe ein."""
    out, t = [], 0.0
    prev_len = None
    for c in d["clips"]:
        ln = clip_len(c)
        x_in = black = 0.0
        if prev_len is not None and c["trans"] == "fade":
            x_in = min(c["tdur"], prev_len / 2, ln / 2)
            t -= x_in
        elif prev_len is not None and c["trans"] == "black":
            black = min(c["tdur"], prev_len, ln)
        out.append((t, ln, x_in, black))
        t += ln
        prev_len = ln
    return out, max(0.0, t)


# ---------------------------------------------------------------- Musik ----

_dur_cache = {}


def audio_duration(path):
    """Länge einer Audiodatei in Sekunden (zwischengespeichert)."""
    import av

    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _dur_cache:
        try:
            with av.open(path) as c:
                s = c.streams.audio[0]
                dur = float(s.duration * s.time_base) if s.duration else (c.duration or 0) / 1e6
        except Exception:
            dur = None
        _dur_cache[key] = dur
    return _dur_cache[key]


def import_music(src):
    """Beliebige Audiodatei in den Musik-Ordner von FotoArchiv übernehmen (Kopie). Gibt den neuen Pfad zurück."""
    import share

    if os.path.splitext(src)[1].lower() not in share.AUDIO_EXT or not os.path.isfile(src):
        raise RuntimeError("Keine Audiodatei (MP3, M4A, WAV …)")
    dest_dir = os.path.join(common.BASE_DIR, "Musik")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.abspath(dest) == os.path.abspath(src):
        return dest
    base, ext = os.path.splitext(dest)
    k = 2
    while os.path.exists(dest):
        if os.path.getsize(dest) == os.path.getsize(src):
            return dest  # schon übernommen
        dest = "%s (%d)%s" % (base, k, ext)
        k += 1
    shutil.copy2(src, dest)
    return dest


# ------------------------------------------------------------ Rendern ----

def _fit(im, W, H, blur_bg=True):
    """Bild auf W×H: passt es ungefähr, randlos füllen; sonst ganz zeigen mit unscharfem Hintergrund."""
    ar, car = im.width / im.height, W / H
    if abs(ar - car) / car < 0.12:
        return ImageOps.fit(im, (W, H), Image.LANCZOS)
    if blur_bg:
        bg = ImageOps.fit(im, (max(1, W // 10), max(1, H // 10)), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3))
        bg = ImageEnhance.Brightness(bg.resize((W, H), Image.BILINEAR)).enhance(0.5)
    else:
        bg = Image.new("RGB", (W, H))
    s = min(W / im.width, H / im.height)
    fg = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.BILINEAR)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg


class _Photo:
    def __init__(self, row, W, H, kb, rnd):
        import media

        path, kind, userrot, edit = row
        im = media.open_image(to_abs(path), kind, max_side=int(max(W, H) * 1.3), userrot=userrot or 0,
                              edit=edit)[0].convert("RGB")
        self.W, self.H, self.kb = W, H, kb
        self.c = _fit(im, int(W * 1.15), int(H * 1.15)) if kb else _fit(im, W, H)
        z0, z1 = (1.0, 1 / 1.15) if rnd.random() < 0.5 else (1 / 1.15, 1.0)
        self.z, self.p0, self.p1 = (z0, z1), (rnd.random(), rnd.random()), (rnd.random(), rnd.random())

    def frame(self, u):
        if not self.kb:
            return self.c
        u = u * u * (3 - 2 * u)
        cw, ch = self.c.size
        z = self.z[0] + (self.z[1] - self.z[0]) * u
        ww, hh = cw * z, ch * z
        x = (cw - ww) * (self.p0[0] + (self.p1[0] - self.p0[0]) * u)
        y = (ch - hh) * (self.p0[1] + (self.p1[1] - self.p0[1]) * u)
        return self.c.resize((self.W, self.H), Image.BILINEAR, box=(x, y, x + ww, y + hh))

    def close(self):
        pass


class _Video:
    """Liest Bilder eines Videos der Reihe nach; frame(t) liefert das Bild zur Clipzeit t (= Datei start + t).
    seek: ab wo gelesen wird (für Vorschauen mitten im Clip)."""

    def __init__(self, path, start, userrot, W, H, seek=None):
        import av

        self.c = av.open(path)
        self.vs = self.c.streams.video[0]
        self.vs.thread_type = "AUTO"
        pos = start if seek is None else seek
        if pos > 0.5:
            self.c.seek(int((pos - 0.5) * 1_000_000))
        self.start, self.userrot, self.W, self.H = start, userrot or 0, W, H
        self.it = self.c.decode(self.vs)
        self.cur = None
        self.cur_t = -1.0
        self.nxt = None
        self.done = False

    def _read(self):
        try:
            fr = next(self.it)
        except (StopIteration, Exception):
            self.done = True
            return None
        t = float(fr.pts * self.vs.time_base) if fr.pts is not None else self.cur_t + 0.04
        return t, fr

    def _img(self, fr):
        im = fr.to_image()
        rot = getattr(fr, "rotation", 0) or 0
        if rot:
            im = im.rotate(rot, expand=True)
        if self.userrot:
            import media

            im = media.rotate_cw(im, self.userrot)
        return _fit(im, self.W, self.H)

    def frame(self, t):
        target = self.start + t
        while not self.done:
            if self.nxt is None:
                self.nxt = self._read()
                if self.nxt is None:
                    break
            nt, fr = self.nxt
            if nt > target + 1e-3 and self.cur is not None:
                break
            self.cur, self.cur_t, self.nxt = fr, nt, None
        if self.cur is None:
            return Image.new("RGB", (self.W, self.H))
        if not hasattr(self, "_cache") or self._cache[0] is not self.cur:
            self._cache = (self.cur, self._img(self.cur))
        return self._cache[1]

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


class _AudioStream:
    """Ton einer Datei ab Sekunde start, fortlaufend gelesen (bleibt offen, damit beim stückweisen Mischen
    keine Lücken oder Sprünge an den Stückgrenzen entstehen – erneutes Ansteuern trifft nie samplegenau)."""

    def __init__(self, path, start):
        import av
        import numpy as np

        self.np = np
        self.buf = np.zeros((2, 0), dtype=np.float32)
        self.done, self.c, self.pos = False, None, None
        try:
            self.c = av.open(path)
            if not self.c.streams.audio:
                raise ValueError
            st = self.c.streams.audio[0]
            if start > 1:
                self.c.seek(int((start - 1) * 1_000_000))
            self.it = self.c.decode(st)
            self.rs = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        except Exception:
            self.done = True
        self.start, self.skip = start, None

    def read(self, n):
        np = self.np
        while self.buf.shape[1] < n and not self.done:
            try:
                fr = next(self.it)
            except Exception:
                self.done = True
                break
            if self.skip is None:
                t0 = float(fr.pts * fr.time_base) if fr.pts is not None else 0.0
                self.skip = max(0, int(round((self.start - t0) * SR)))
            fr.pts = None
            for r in self.rs.resample(fr):
                a = r.to_ndarray().astype(np.float32)
                if a.shape[0] == 1:
                    a = np.vstack([a, a])
                if self.skip:
                    k = min(self.skip, a.shape[1])
                    a, self.skip = a[:, k:], self.skip - k
                if a.shape[1]:
                    self.buf = np.concatenate([self.buf, a], axis=1)
        out, self.buf = self.buf[:, :n], self.buf[:, n:]
        if out.shape[1] < n:
            out = np.concatenate([out, np.zeros((2, n - out.shape[1]), dtype=np.float32)], axis=1)
        return out

    def close(self):
        try:
            if self.c:
                self.c.close()
        except Exception:
            pass


def _gain(lt, ln, fin, fout):
    """Lautstärke an den Stellen lt (Sekunden im Clip, Länge ln): linear ein-/ausblenden."""
    import numpy as np

    g = np.ones_like(lt)
    if fin > 0:
        g = np.minimum(g, lt / fin)
    if fout > 0:
        g = np.minimum(g, (ln - lt) / fout)
    return np.clip(g, 0, 1).astype(np.float32)


def mix_audio(d, rows, tl, total, r0=0.0, r1=None, n=None, streams=None):
    """Tonspur für die Sequenzzeit r0…r1 (Standard: ganzer Film) mischen: Originalton der Videos + Musik.
    n: genaue Anzahl Samples. streams: offene Tonquellen über mehrere Aufrufe (stückweises Mischen beim Rendern);
    ohne werden sie hier geöffnet und wieder geschlossen. Gerechnet wird in ganzen Samples ab Filmanfang."""
    import numpy as np

    r1 = total if r1 is None else min(r1, total)
    A = int(round(r0 * SR))
    n = max(0, int(round(r1 * SR)) - A) if n is None else n
    buf = np.zeros((2, n), dtype=np.float32)
    if not n:
        return buf
    own = streams is None
    streams = {} if own else streams

    def place(key, path, src_start, seq_start, ln, fin, fout, vol):
        S, L = int(round(seq_start * SR)), int(round(ln * SR))
        lo, hi = max(S, A), min(S + L, A + n)
        if hi <= lo:
            return
        st = streams.get(key)
        if st is None or st.pos != lo:  # neu öffnen (erster Teil oder nicht fortlaufend)
            if st:
                st.close()
            st = streams[key] = _AudioStream(path, src_start + (lo - S) / SR)
        a = st.read(hi - lo)
        st.pos = hi
        lt = (np.arange(lo, hi, dtype=np.float64) - S) / SR
        buf[:, lo - A:hi - A] += a * (_gain(lt, ln, fin, fout) * vol)

    for i, (c, (start, ln, x_in, black)) in enumerate(zip(d["clips"], tl)):
        if c["kind"] != "video" or c["vol"] <= 0:
            continue
        x_out = tl[i + 1][2] if i + 1 < len(tl) else 0
        b_out = tl[i + 1][3] / 2 if i + 1 < len(tl) else 0
        place(("c", i), to_abs(rows[c["item"]][0]), c["in"], start, ln,
              max(x_in, black / 2, 0.01), max(x_out, b_out, 0.01), c["vol"])
    for j, m in enumerate(d["music"]):
        mdur = audio_duration(m["path"]) or 36000
        ln = min(mdur - m["offset"], total - m["start"])  # wie lange das Lied im Film läuft
        if ln <= 0:
            continue
        fout = m["fout"] if m["start"] + ln >= total - 0.05 else min(m["fout"], 1.0)
        place(("m", j), m["path"], m["offset"], m["start"], ln, m["fin"], max(fout, 0.05), m["vol"])
    # Film ein-/ausblenden
    t = (A + np.arange(n, dtype=np.float64)) / SR
    buf *= _gain(t, total, d.get("fadein", 0), d.get("fadeout", 0))
    np.clip(buf, -1, 1, out=buf)
    if own:
        for st in streams.values():
            st.close()
    return buf


def _video_stream(out, W, H, fps, fast=False):
    import av

    for codec, opts in (("libx264", {"crf": "26" if fast else "19", "preset": "ultrafast" if fast else "veryfast"}),
                        ("h264_videotoolbox", {}), ("h264_mf", {}), ("mpeg4", {})):
        try:
            av.codec.Codec(codec, "w")
        except Exception:
            continue
        s = out.add_stream(codec, rate=fps)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        if opts:
            s.options = opts
        else:
            s.bit_rate = 16_000_000 if W * H > 2_500_000 else 10_000_000
        return s
    raise RuntimeError("Kein Video-Encoder verfügbar")


def render(d, out_path, job, region=None, preview=False):
    """Projekt als MP4 (H.264 + AAC, faststart). job: dict mit progress/stop.
    region=(t0, t1): nur dieser Ausschnitt der Sequenz; preview: klein (640 px) und schnell."""
    import av
    import numpy as np

    W, H = FORMATS[d["format"]]
    if preview:
        s = PREVIEW_SIDE / max(W, H)
        W, H = int(W * s) // 2 * 2, int(H * s) // 2 * 2
    elif d.get("uhd"):
        W, H = W * 2, H * 2
    fps = d["fps"]
    tl, total = timeline(d)
    if not d["clips"] or total <= 0:
        raise RuntimeError("Das Projekt ist leer")
    if total > MAX_MINUTES * 60:
        raise RuntimeError("Höchstens %d Minuten je Video" % MAX_MINUTES)
    nframes = int(round(total * fps))
    f0, f1 = 0, nframes
    if region:
        f0 = max(0, min(nframes - 1, int(region[0] * fps)))
        f1 = max(f0 + 1, min(nframes, int(math.ceil(region[1] * fps))))
    con = connect()
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT id, path, kind, userrot, edit FROM items WHERE id IN (%s)" % ",".join(
            str(int(c["item"])) for c in d["clips"]))}
    con.close()
    missing = [c["item"] for c in d["clips"] if c["item"] not in rows]
    if missing:
        raise RuntimeError("%d Clips gibt es nicht mehr im Katalog" % len(missing))
    # Ton stückweise mischen (je AUDIO_CHUNK Samples), während die Bilder entstehen
    a_first, a_last = int(round(f0 / fps * SR)), int(round(f1 / fps * SR))
    pending = np.zeros((2, 0), dtype=np.float32)
    mixed = a_first
    astreams = {}

    def audio_upto(k):
        """Sicherstellen, dass die Samples bis k (ab Ausschnittbeginn) gemischt bereitliegen."""
        nonlocal pending, mixed
        while a_written + pending.shape[1] < k and mixed < a_last:
            nxt = min(a_last, mixed + AUDIO_CHUNK)
            part = mix_audio(d, rows, tl, total, mixed / SR, nxt / SR, n=nxt - mixed, streams=astreams)
            pending = np.concatenate([pending, part], axis=1)
            mixed = nxt

    a_written = 0
    out = av.open(out_path, "w", format="mp4", options={"movflags": "+faststart"})  # Zwischendatei heißt .part
    try:
        vs = _video_stream(out, W, H, fps, fast=preview)
        try:
            ast = out.add_stream("aac", rate=SR, layout="stereo")
        except TypeError:
            ast = out.add_stream("aac", rate=SR)
        ast.bit_rate = 128_000 if preview else 192_000
        rnd = random.Random(42)
        rnd_seeds = [rnd.random() for _ in d["clips"]]  # Kamerafahrt je Clip fest, egal ab wo gerendert wird
        open_src = {}
        black = Image.new("RGB", (W, H))
        t_first = f0 / fps
        job["phase"] = "Bilder rechnen"

        def src(i):
            if i not in open_src:
                c = d["clips"][i]
                r = rows[c["item"]]
                if c["kind"] == "video":
                    seek = c["in"] + max(0.0, t_first - tl[i][0])
                    open_src[i] = _Video(to_abs(r[0]), c["in"], r[2], W, H, seek=seek)
                else:
                    open_src[i] = _Photo((r[0], r[1], r[2], r[3]), W, H, c.get("kb"), random.Random(rnd_seeds[i]))
            return open_src[i]

        def write_audio(upto):
            nonlocal a_written, pending
            upto = min(upto, a_last - a_first)
            audio_upto(upto)
            while a_written + 1024 <= upto and pending.shape[1] >= 1024:
                fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(pending[:, :1024]), format="fltp", layout="stereo")
                fr.sample_rate = SR
                fr.pts = a_written
                for p in ast.encode(fr):
                    out.mux(p)
                pending = pending[:, 1024:]
                a_written += 1024

        fin, fout = d.get("fadein", 0), d.get("fadeout", 0)
        for f in range(f0, f1):
            if job.get("stop"):
                raise RuntimeError("Abgebrochen")
            t = (f + 0.5) / fps
            act = [i for i, (s, ln, _x, _b) in enumerate(tl) if s <= t < s + ln]
            for i in [k for k in open_src if k not in act and k < (act[0] if act else len(tl))]:
                open_src.pop(i).close()
            if not act:
                img = black
            else:
                i = act[-1]
                s, ln, x_in, blk = tl[i]
                lt = t - s
                c = d["clips"][i]
                img = src(i).frame(lt / ln if c["kind"] == "photo" else lt)
                if len(act) > 1 and x_in > 0 and lt < x_in:
                    j = act[-2]
                    pc = d["clips"][j]
                    plt = t - tl[j][0]
                    prev = src(j).frame(min(1.0, plt / tl[j][1]) if pc["kind"] == "photo" else plt)
                    img = Image.blend(prev, img, lt / x_in)
                if blk > 0 and lt < blk / 2:
                    img = Image.blend(black, img, lt / (blk / 2))
                nb = tl[i + 1][3] if i + 1 < len(tl) else 0
                if nb > 0 and ln - lt < nb / 2:
                    img = Image.blend(black, img, max(0.0, (ln - lt) / (nb / 2)))
            if fin > 0 and t < fin:
                img = Image.blend(black, img, max(0.0, t / fin))
            if fout > 0 and total - t < fout:
                img = Image.blend(black, img, max(0.0, (total - t) / fout))
            vf = av.VideoFrame.from_image(img.convert("RGB"))
            vf.pts = f - f0
            for p in vs.encode(vf):
                out.mux(p)
            write_audio(int(round((f - f0 + 1) / fps * SR)))
            job["progress"] = (f - f0 + 1) / (f1 - f0)
        for p in vs.encode(None):
            out.mux(p)
        write_audio(int(round((f1 - f0) / fps * SR)))  # Ton genau so lang wie das Bild
        for p in ast.encode(None):
            out.mux(p)
        for s_ in list(open_src.values()):
            s_.close()
    finally:
        out.close()
        for st in astreams.values():
            st.close()


def preview(d, t0, seconds=8.0):
    """Kurzen Ausschnitt ab t0 klein rendern (mit Übergängen und Ton). Gibt den Dateinamen in PREVIEW_DIR zurück."""
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    for f in os.listdir(PREVIEW_DIR):  # alte Vorschauen weg (älter als 1 Stunde)
        p = os.path.join(PREVIEW_DIR, f)
        try:
            if time.time() - os.path.getmtime(p) > 3600:
                os.remove(p)
        except OSError:
            pass
    _tl, total = timeline(d)
    t0 = max(0.0, min(t0, max(0.0, total - 0.5)))
    name = "v%s.mp4" % uuid.uuid4().hex[:12]
    path = os.path.join(PREVIEW_DIR, name)
    render(d, path + ".part", {"progress": 0}, region=(t0, min(total, t0 + seconds)), preview=True)
    os.replace(path + ".part", path)
    return name, t0


def preview_path(name):
    if not re.fullmatch(r"v[0-9a-f]{12}\.mp4", name or ""):
        return None
    p = os.path.join(PREVIEW_DIR, name)
    return p if os.path.exists(p) else None


# ------------------------------------------------------- Warteschlange ----

_lock = threading.Lock()
_queue = []
_jobs = {}   # id -> laufende Angaben (Fortschritt), Rest steht in vrenders
_worker = None


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enqueue(pid):
    import share

    con = connect()
    row = con.execute("SELECT name, data FROM vprojects WHERE id=?", (pid,)).fetchone()
    if not row:
        raise RuntimeError("Projekt nicht gefunden")
    d = clean(json.loads(row[1] or "{}"), con)
    if not d["clips"]:
        raise RuntimeError("Das Projekt hat noch keine Clips")
    dest = share.default_dest()
    os.makedirs(dest, exist_ok=True)
    base = share._safe(row[0] or "Video") or "Video"
    out = os.path.join(dest, "%s %s.mp4" % (base, datetime.datetime.now().strftime("%Y-%m-%d %H-%M")))
    k = 2
    while os.path.exists(out):
        out = out[:-4] + " (%d).mp4" % k
        k += 1
    jid = con.execute("INSERT INTO vrenders(project, name, status, progress, out, created, data) VALUES(?,?,?,?,?,?,?)",
                      (pid, row[0], "wartet", 0, out, _now(), json.dumps(d))).lastrowid
    con.commit()
    con.close()
    with _lock:
        _queue.append(jid)
        _jobs[jid] = {"progress": 0.0, "phase": "wartet", "stop": False}
    _start_worker()
    return jid


def _start_worker():
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_work, daemon=True)
        _worker.start()


def _work():
    while True:
        with _lock:
            if not _queue:
                return
            jid = _queue.pop(0)
        job = _jobs[jid]
        con = connect()
        row = con.execute("SELECT data, out FROM vrenders WHERE id=?", (jid,)).fetchone()
        if job.get("stop") or not row:
            con.execute("UPDATE vrenders SET status='abgebrochen', finished=? WHERE id=?", (_now(), jid))
            con.commit()
            con.close()
            continue
        con.execute("UPDATE vrenders SET status='läuft' WHERE id=?", (jid,))
        con.commit()
        t0 = time.time()
        tmp = row[1] + ".part"
        try:
            render(json.loads(row[0]), tmp, job)
            os.replace(tmp, row[1])
            con.execute("UPDATE vrenders SET status='fertig', progress=1, finished=?, seconds=?, error=NULL WHERE id=?",
                        (_now(), time.time() - t0, jid))
        except Exception as ex:  # noqa: BLE001 – Grund in der Renderliste zeigen
            try:
                os.remove(tmp)
            except OSError:
                pass
            st = "abgebrochen" if job.get("stop") else "fehler"
            con.execute("UPDATE vrenders SET status=?, error=?, finished=? WHERE id=?", (st, str(ex)[:300], _now(), jid))
        con.commit()
        con.close()


def jobs(pid=None, limit=50):
    con = connect()
    q = "SELECT id, project, name, status, progress, out, created, finished, error, seconds FROM vrenders"
    args = ()
    if pid:
        q += " WHERE project=?"
        args = (pid,)
    rows = con.execute(q + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(zip(("id", "project", "name", "status", "progress", "out", "created", "finished", "error", "seconds"), r))
        live = _jobs.get(d["id"])
        if live and d["status"] in ("wartet", "läuft"):
            d["progress"], d["phase"] = live["progress"], live.get("phase")
        d["exists"] = d["status"] == "fertig" and os.path.exists(d["out"] or "")
        out.append(d)
    return out


def cancel(jid):
    with _lock:
        if jid in _jobs:
            _jobs[jid]["stop"] = True
        if jid in _queue:
            _queue.remove(jid)
            con = connect()
            con.execute("UPDATE vrenders SET status='abgebrochen', finished=? WHERE id=?", (_now(), jid))
            con.commit()
            con.close()


def recover():
    """Beim Start: Aufträge, die beim Beenden noch liefen, als abgebrochen markieren."""
    con = connect()
    with _lock:
        mine = list(_jobs)  # in dieser Sitzung angelegte Aufträge nicht anfassen
    con.execute("UPDATE vrenders SET status='abgebrochen', error='FotoArchiv wurde beendet' "
                "WHERE status IN ('wartet', 'läuft') AND id NOT IN (%s)" % ",".join(str(int(j)) for j in mine or [0]))
    con.commit()
    con.close()
