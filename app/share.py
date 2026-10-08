"""Teilen: Fotos exportieren (Ordner/ZIP, Apple Fotos am Mac) und Diashow als MP4-Video mit Musik.

Die Originale werden dabei nie verändert – es entstehen immer neue Dateien im Export-Ordner.
"""
import datetime
import io
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

import common
from common import connect, to_abs
from importer import Job

JOB = Job()
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
SIZES = {"original": None, "large": 2560, "small": 1600}


def music_dirs():
    cfg = common.load_config()
    dirs = cfg.get("music_dirs") or [os.path.join(common.BASE_DIR, "Musik"), os.path.join(common.LIB_ROOT, "Musik")]
    os.makedirs(os.path.join(common.BASE_DIR, "Musik"), exist_ok=True)
    return [d for d in dirs if os.path.isdir(d)]


def music_list():
    out = []
    for d in music_dirs():
        for dp, dn, fn in os.walk(d):
            for f in sorted(fn):
                if os.path.splitext(f)[1].lower() in AUDIO_EXT and not f.startswith("."):
                    full = os.path.join(dp, f)
                    out.append({"path": full, "name": os.path.splitext(f)[0]})
    return out


def music_allowed(path):
    path = os.path.abspath(path)
    return any(path.startswith(os.path.abspath(d) + os.sep) for d in music_dirs()) and \
        os.path.splitext(path)[1].lower() in AUDIO_EXT


def default_dest():
    home = os.path.expanduser("~")
    for name in ("Desktop", "Schreibtisch"):
        if os.path.isdir(os.path.join(home, name)):
            return os.path.join(home, name, "FotoArchiv Export")
    return os.path.join(home, "FotoArchiv Export")


def _safe(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip()).strip(". ")
    return name or datetime.datetime.now().strftime("Export %Y-%m-%d %H-%M")


def _items(con, ids):
    rows = {}
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        for r in con.execute("SELECT id, path, kind, name, taken, userrot FROM items WHERE id IN (%s)"
                             % ",".join("?" * len(ch)), ch):
            rows[r[0]] = r
    return [rows[i] for i in ids if i in rows]


def reveal(path):
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
    except OSError:
        pass


# ---------------------------------------------------------------- Export ----

def _as_jpeg(path, kind, max_side, userrot):
    """Foto als JPEG (richtig gedreht, Datum/GPS bleiben in den EXIF-Daten)."""
    import media

    im, exif, _x, _s, _o = media.open_image(path, kind, max_side=max_side, userrot=userrot or 0,
                                            edit=common.edit_for(path))
    if max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    kw = {"quality": 90, "optimize": True}
    if exif:
        try:
            exif[0x0112] = 1  # Bild ist bereits gedreht
            kw["exif"] = exif.tobytes()
        except Exception:
            pass
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def export(ids, size="small", as_zip=False, name=None, dest=None, apple_photos=False):
    JOB.reset("Export: " + (name or "%d Fotos" % len(ids)))
    JOB.running = True
    con = connect()
    try:
        items = _items(con, ids)
        JOB.total = len(items)
        name = _safe(name)
        base = dest or default_dest()
        if apple_photos:
            base = tempfile.mkdtemp(prefix="fa_photos_")
        os.makedirs(base, exist_ok=True)
        target = os.path.join(base, name)
        n = 2
        while os.path.exists(target) or os.path.exists(target + ".zip"):
            target = os.path.join(base, "%s (%d)" % (name, n))
            n += 1
        zf = zipfile.ZipFile(target + ".zip", "w", zipfile.ZIP_STORED) if as_zip else None
        if not zf:
            os.makedirs(target)
        used = set()
        written = []
        max_side = SIZES.get(size)
        for iid, rel, kind, fname, taken, userrot in items:
            if JOB.stop:
                break
            JOB.done += 1
            src = to_abs(rel)
            try:
                stem, ext = os.path.splitext(fname)
                # sortierbare Dateinamen nach Aufnahmezeit
                prefix = (taken or "")[:19].replace(":", "-").replace(" ", "_")
                if not stem.startswith((taken or "-")[:10]):
                    stem = (prefix + " " + stem).strip()
                if kind == "video" or (size == "original" and not userrot):
                    data_path, data = src, None
                    out_name = stem + ext
                else:
                    data_path, data = None, _as_jpeg(src, kind, max_side, userrot)
                    out_name = stem + ".jpg"
                k, cand = 1, out_name
                while cand.lower() in used:
                    cand = "%s_%d%s" % (os.path.splitext(out_name)[0], k, os.path.splitext(out_name)[1])
                    k += 1
                used.add(cand.lower())
                if zf:
                    if data is None:
                        zf.write(data_path, cand)
                    else:
                        zf.writestr(cand, data)
                else:
                    out = os.path.join(target, cand)
                    if data is None:
                        shutil.copy2(data_path, out)
                    else:
                        with open(out, "wb") as f:
                            f.write(data)
                    written.append(out)
                JOB.new += 1
                JOB.bytes += len(data) if data is not None else os.path.getsize(src)
            except Exception as ex:
                JOB.errors += 1
                JOB.note("Fehler bei %s: %s" % (fname, ex))
        if zf:
            zf.close()
        result = target + ".zip" if zf else target
        if apple_photos and sys.platform == "darwin" and written:
            JOB.phase = "In Apple Fotos übernehmen"
            _to_apple_photos(written, name)
            JOB.message = "%d Fotos als Album „%s“ in Apple Fotos übernommen" % (len(written), name)
        elif JOB.stop:
            JOB.message = "Abgebrochen – %d von %d Dateien exportiert nach %s" % (JOB.new, JOB.total, result)
        else:
            JOB.message = "%d Dateien exportiert nach %s" % (JOB.new, result)
            reveal(os.path.dirname(result) if zf else result)
        JOB.result = result
        JOB.note(JOB.message)
    except Exception as ex:
        JOB.message = "Fehler: %s" % ex
        JOB.errors += 1
    finally:
        con.close()
        JOB.running = False
        JOB.finished = time.time()
        JOB.phase = ""


def _to_apple_photos(files, album):
    """Mac: Album in der Fotos-App anlegen und die Dateien importieren (danach dort teilbar)."""
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = ('tell application "Photos"\n activate\n set a to make new album named %s\n'
              ' import {%s} into a skip check duplicates true\nend tell' % (
                  q(album), ", ".join("POSIX file " + q(f) for f in files)))
    subprocess.run(["osascript", "-e", script], check=True, timeout=3600)


# ---------------------------------------------------------- Diashow-Video ----

W, H, FPS = 1920, 1080, 25
PROC_SHOW = 3  # erhöhen, wenn sich die Diashow-Erzeugung ändert (alte Fernseher-Diashows neu erzeugen)
FADE = 1.0


def _canvas(im):
    """Foto auf 16:9-Fläche (15 % größer für die Kamerafahrt). Hochkant/Quadrat mit unscharfem Hintergrund."""
    cw, ch = int(W * 1.15), int(H * 1.15)
    ar, car = im.width / im.height, cw / ch
    if abs(ar - car) / car < 0.25:
        return ImageOps.fit(im, (cw, ch), Image.LANCZOS)
    bg = ImageOps.fit(im, (cw // 8, ch // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3))
    bg = ImageEnhance.Brightness(bg.resize((cw, ch), Image.BILINEAR)).enhance(0.55)
    s = min(cw / im.width, ch / im.height)
    fg = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    bg.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2))
    return bg


class _Slide:
    def __init__(self, im, kenburns, rnd):
        self.c = _canvas(im)
        cw, ch = self.c.size
        self.kb = kenburns
        z0, z1 = (1.0, 1 / 1.15) if rnd.random() < 0.5 else (1 / 1.15, 1.0)
        self.z = (z0, z1)
        self.p0 = (rnd.random(), rnd.random())
        self.p1 = (rnd.random(), rnd.random())

    def frame(self, u):
        """u = 0..1 Fortschritt innerhalb der Anzeigezeit."""
        cw, ch = self.c.size
        if not self.kb:
            if not hasattr(self, "_still"):
                self._still = self.c.resize((W, H), Image.LANCZOS)
            return self._still
        u = u * u * (3 - 2 * u)  # weich beschleunigen/abbremsen
        z = self.z[0] + (self.z[1] - self.z[0]) * u
        ww, hh = cw * z, ch * z
        px = self.p0[0] + (self.p1[0] - self.p0[0]) * u
        py = self.p0[1] + (self.p1[1] - self.p0[1]) * u
        x, y = (cw - ww) * px, (ch - hh) * py
        return self.c.resize((W, H), Image.BILINEAR, box=(x, y, x + ww, y + hh))


class _Audio:
    """Musik nacheinander (in Schleife) als AAC in die Videodatei schreiben, am Ende ausblenden."""

    def __init__(self, out, files, total_s):
        import av

        self.av = av
        self.files = list(files)
        self.rate = 44100
        try:
            self.stream = out.add_stream("aac", rate=self.rate, layout="stereo")
        except TypeError:
            self.stream = out.add_stream("aac", rate=self.rate)
        self.out = out
        self.total = int(total_s * self.rate)
        self.fade_from = max(0, self.total - 3 * self.rate)
        self.written = 0
        self.fifo = av.AudioFifo()
        self.resampler = av.AudioResampler(format="fltp", layout="stereo", rate=self.rate)
        self.gen = self._frames()
        self.done = False

    def _frames(self):
        av = self.av
        while True:
            any_ok = False
            for f in self.files:
                try:
                    with av.open(f) as inp:
                        for fr in inp.decode(audio=0):
                            fr.pts = None
                            for r in self.resampler.resample(fr):
                                any_ok = True
                                yield r
                except Exception:
                    continue
            if not any_ok:
                return

    def fill(self, until_s):
        import numpy as np

        av = self.av
        target = min(self.total, int(until_s * self.rate))
        while not self.done and self.written < target:
            while self.fifo.samples < 1024:
                try:
                    self.fifo.write(next(self.gen))
                except StopIteration:
                    self.done = True
                    break
            if self.fifo.samples == 0:
                break
            fr = self.fifo.read(min(1024, self.fifo.samples))
            arr = fr.to_ndarray().astype("float32")
            n = arr.shape[1]
            n = min(n, self.total - self.written)
            arr = arr[:, :n]
            if self.written + n > self.fade_from:
                idx = np.arange(self.written, self.written + n)
                gain = np.clip((self.total - idx) / max(1, self.total - self.fade_from), 0, 1)
                arr = arr * gain
            nf = av.AudioFrame.from_ndarray(np.ascontiguousarray(arr), format="fltp", layout="stereo")
            nf.sample_rate = self.rate
            nf.pts = self.written
            self.written += n
            for p in self.stream.encode(nf):
                self.out.mux(p)
            if self.written >= self.total:
                self.done = True

    def close(self):
        for p in self.stream.encode(None):
            self.out.mux(p)
        self.gen.close()  # Musikdatei freigeben


def _video_stream(out):
    import av

    for codec, opts in (("libx264", {"crf": "20", "preset": "veryfast"}), ("h264_videotoolbox", {}),
                        ("h264_mf", {}), ("mpeg4", {})):
        try:
            av.codec.Codec(codec, "w")
        except Exception:
            continue
        s = out.add_stream(codec, rate=FPS)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        if opts:
            s.options = opts
        else:
            s.bit_rate = 10_000_000
        return s
    raise RuntimeError("Kein Video-Encoder verfügbar")


class _Silence:
    """Tonspur ohne Musik: Stille für Fotos, Originalton für Video-Clips."""

    def __init__(self, out):
        import av

        self.av = av
        self.rate = 44100
        try:
            self.stream = out.add_stream("aac", rate=self.rate, layout="stereo")
        except TypeError:
            self.stream = out.add_stream("aac", rate=self.rate)
        self.out = out
        self.written = 0
        self.fifo = av.AudioFifo()
        self.resampler = av.AudioResampler(format="fltp", layout="stereo", rate=self.rate)

    def _push(self, arr):
        import numpy as np

        av = self.av
        f = av.AudioFrame.from_ndarray(np.ascontiguousarray(arr.astype("float32")), format="fltp", layout="stereo")
        f.sample_rate = self.rate
        f.pts = None
        self.fifo.write(f)
        while self.fifo.samples >= 1024:
            fr = self.fifo.read(1024)
            fr.pts = self.written
            self.written += fr.samples
            for p in self.stream.encode(fr):
                self.out.mux(p)

    def silence_until(self, t):
        import numpy as np

        n = int(t * self.rate) - self.written - self.fifo.samples
        while n > 0:
            k = min(n, 44100)
            self._push(np.zeros((2, k)))
            n -= k

    def clip(self, path, start_t, dur):
        """Ton eines Videos ab start_t (Sekunden in der Diashow) für dur Sekunden."""
        import numpy as np

        self.silence_until(start_t)
        need = int(dur * self.rate)
        got = 0
        try:
            with self.av.open(path) as inp:
                if inp.streams.audio:
                    for fr in inp.decode(audio=0):
                        fr.pts = None
                        for r in self.resampler.resample(fr):
                            arr = r.to_ndarray()
                            take = min(arr.shape[1], need - got)
                            if take <= 0:
                                break
                            self._push(arr[:, :take])
                            got += take
                        if got >= need:
                            break
        except Exception:
            pass
        self.silence_until(start_t + dur)

    def close(self, t):
        import numpy as np

        self.silence_until(t)
        if self.fifo.samples:
            fr = self.fifo.read(self.fifo.samples)
            fr.pts = self.written
            self.written += fr.samples
            for p in self.stream.encode(fr):
                self.out.mux(p)
        for p in self.stream.encode(None):
            self.out.mux(p)


def _clip_frames(path, userrot, max_s=120):
    """Bilder eines Videos im 25-fps-Raster, auf 1920x1080 eingepasst (schwarzer Rand)."""
    import av

    with av.open(path) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        t_next = 0.0
        for fr in c.decode(vs):
            t = float(fr.pts * vs.time_base) if fr.pts is not None else t_next
            if t < 0:
                continue
            im = fr.to_image()
            rot = (getattr(fr, "rotation", 0) or 0)
            if rot:
                im = im.rotate(rot, expand=True)
            if userrot:
                from media import rotate_cw

                im = rotate_cw(im, userrot)
            start = None
            while t_next <= t + 1e-6:
                if start is None:
                    start = t_next
                    s_ = min(W / im.width, H / im.height)
                    fg = im.resize((max(1, int(im.width * s_)), max(1, int(im.height * s_))), Image.BILINEAR)
                    canvas = Image.new("RGB", (W, H))
                    canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
                yield canvas
                t_next += 1 / FPS
                if t_next > max_s:
                    return


def render_show(items, out_path, seconds, kenburns, music, clips, job, max_clip_s=120):
    """Diashow als MP4 rendern. items: (id, pfad, art, name, datum, userrot).
    Fotos mit Überblendung (und Kamerafahrt), Videos als Clips (wenn clips=True).
    Gibt die Anzahl der Einzelbilder zurück."""
    import av
    import media

    seconds = max(2.0, min(30.0, float(seconds)))
    photos = [r for r in items if r[2] != "video"]
    vids = [r for r in items if r[2] == "video"] if clips else []
    # Gesamtlänge für die Musik schätzen (Clip-Längen aus dem Katalog)
    est = len(photos) * seconds + FADE
    if vids:
        con = connect()
        try:
            for r in vids:
                d = con.execute("SELECT duration FROM items WHERE id=?", (r[0],)).fetchone()
                est += min(max_clip_s, (d and d[0]) or 10)
        finally:
            con.close()
    seq = [r for r in items if r[2] != "video" or clips]
    job.total = len(seq)
    out = av.open(out_path, "w", format="mp4", options={"movflags": "faststart"})
    vs = _video_stream(out)
    music = [m for m in music if music_allowed(m)]
    audio = _Audio(out, music, est) if music else _Silence(out)
    rnd = random.Random(42)

    def load(row):
        iid, rel, kind, fname, taken, userrot = row
        try:
            im = media.open_image(to_abs(rel), kind, max_side=2600, userrot=userrot or 0,
                                  edit=common.edit_for(to_abs(rel)))[0]
        except Exception as ex:
            job.note("Übersprungen %s: %s" % (fname, ex))
            job.errors += 1
            return None
        return _Slide(im, kenburns, rnd)

    frame_no = 0
    per = int(seconds * FPS)
    fade_n = int(FADE * FPS)

    job.timeline = []  # Startzeit (Sekunden) jedes Fotos/Videos – für Vor/Zurück auf dem Fernseher

    def emit(img, key=False):
        nonlocal frame_no
        vf = av.VideoFrame.from_image(img)
        vf.pts = frame_no
        if key:
            # Schlüsselbild am Beginn jedes Fotos: dorthin kann der Fernseher genau springen
            job.timeline.append(round(frame_no / FPS, 2))
            try:
                vf.pict_type = av.video.frame.PictureType.I
            except AttributeError:
                vf.pict_type = "I"
        frame_no += 1
        for p in vs.encode(vf):
            out.mux(p)
        if music:
            audio.fill(frame_no / FPS + 1)

    k = 0
    pending = None  # bereits geladenes nächstes Foto
    faded_in = False
    while k < len(seq) and not job.stop:
        row = seq[k]
        if row[2] == "video":
            t0 = frame_no / FPS
            n0 = frame_no
            try:
                first = True
                for img in _clip_frames(to_abs(row[1]), row[5] or 0, max_clip_s):
                    if job.stop:
                        break
                    emit(img, key=first)
                    first = False
            except Exception as ex:
                job.note("Video übersprungen %s: %s" % (row[3], ex))
                job.errors += 1
            if not music:
                audio.clip(to_abs(row[1]), t0, (frame_no - n0) / FPS)
            job.done += 1
            k += 1
            faded_in = False
            continue
        cur = pending or load(row)
        pending = None
        if cur is None:
            job.done += 1
            k += 1
            continue
        # nächstes Element: Foto -> Überblendung, Video/Ende -> harter Schnitt
        nxt = None
        if k + 1 < len(seq) and seq[k + 1][2] != "video":
            nxt = pending = load(seq[k + 1])
        start = fade_n if faded_in else 0
        end = per + (0 if nxt is not None else fade_n)
        for f in range(start, end):
            if job.stop:
                break
            if (not kenburns and f not in (start, end - 1) and (f - start) % (FPS // 2)
                    and not (nxt is not None and f >= per - fade_n)):
                # Standbild: nur alle halbe Sekunde speichern (variable Bildrate wie bei Handy-Videos) –
                # spart fast die ganze Rechenzeit. Größere Lücken verträgt der Samsung nach einem Sprung nicht.
                frame_no += 1
                continue
            u = f / (per + fade_n)
            img = cur.frame(u)
            if nxt is not None and f >= per - fade_n:
                a = (f - (per - fade_n)) / fade_n
                img = Image.blend(img, nxt.frame((f - (per - fade_n)) / (per + fade_n)), a)
            emit(img, key=(f == start))
        faded_in = nxt is not None
        job.done += 1
        k += 1
    for p in vs.encode(None):
        out.mux(p)
    if music:
        audio.total = min(audio.total, int(frame_no / FPS * audio.rate))
        audio.fade_from = max(0, audio.total - 3 * audio.rate)
        audio.fill(frame_no / FPS)
        audio.close()
    else:
        audio.close(frame_no / FPS)
    out.close()
    return frame_no


def slideshow_video(ids, seconds=5.0, kenburns=True, music=(), name=None, shuffle=False, tv=False):
    import av

    JOB.reset("Diashow-Video: " + (name or "%d Fotos" % len(ids)))
    JOB.running = True
    con = connect()
    out_path = None
    try:
        items = [r for r in _items(con, ids) if r[2] != "video"]
        if shuffle:
            random.shuffle(items)
        if not items:
            JOB.message = "Keine Fotos ausgewählt (Videos werden in der Diashow übersprungen)."
            return
        JOB.total = len(items)
        base = default_dest()
        os.makedirs(base, exist_ok=True)
        out_path = os.path.join(base, _safe(name or "Diashow") + ".mp4")
        n = 2
        while os.path.exists(out_path):
            out_path = os.path.join(base, "%s (%d).mp4" % (_safe(name or "Diashow"), n))
            n += 1
        frame_no = render_show(items, out_path, seconds, kenburns, music, False, JOB)
        JOB.new = JOB.done
        JOB.bytes = os.path.getsize(out_path)
        JOB.result = out_path
        JOB.message = "Video gespeichert: %s (%d:%02d min)" % (out_path, frame_no // FPS // 60, frame_no // FPS % 60)
        JOB.note(JOB.message)
        if tv:
            try:
                import dlna

                dlna.play_file(out_path, name or "Diashow")
                JOB.message += " – läuft jetzt auf dem Fernseher"
            except Exception as ex:
                JOB.message += " – Fernseher: %s" % ex
        else:
            reveal(os.path.dirname(out_path))
    except Exception as ex:
        import traceback

        traceback.print_exc()
        JOB.message = "Fehler: %s" % ex
        JOB.errors += 1
    finally:
        con.close()
        JOB.running = False
        JOB.finished = time.time()
        JOB.phase = ""


def start(fn, *args, **kw):
    if JOB.running:
        return False
    JOB.running = True
    JOB.result = None
    threading.Thread(target=fn, args=args, kwargs=kw, daemon=True).start()
    return True
