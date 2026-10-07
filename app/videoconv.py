"""Videos, die der Browser nicht abspielen kann (MTS/AVCHD, AVI, MKV, WMV, MPG, 3GP ...), in ein
browsertaugliches MP4 umpacken. H.264-Bild wird unverändert übernommen (schnell), nur der Ton wird
nach AAC umgewandelt; andere Bildformate werden nach H.264 umgerechnet. Ergebnis im Zwischenspeicher.
"""
import os
import threading
import time

import common
from common import connect, to_abs

def _cache_dir():
    # Zwischenspeicher auf der internen Platte des Computers: schneller als auf die USB-Platte zu
    # schreiben, von der gleichzeitig gelesen wird (und er muss nicht mitwandern)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "FotoArchiv", "videocache")
    return os.path.join(os.path.expanduser("~"), "Library", "Caches", "FotoArchiv", "videocache") \
        if os.path.isdir(os.path.expanduser("~/Library")) else os.path.join(common.DATA_DIR, "_videocache")


CACHE = _cache_dir()
MAX_CACHE = 8 * 1024 ** 3        # Zwischenspeicher höchstens 8 GB
BROWSER_EXT = {"mp4", "m4v", "mov", "qt", "webm"}
COPY_VIDEO = {"h264", "hevc"}    # kann der Browser direkt (HEVC nur mit Hardware-Unterstützung)

_jobs = {}   # id -> {"state": "running"|"done"|"error", "progress": 0..1, "error": str}
_lock = threading.Lock()


def cached_path(iid):
    return os.path.join(CACHE, "%d.mp4" % iid)


def status(iid):
    if os.path.exists(cached_path(iid)):
        return {"state": "done", "progress": 1.0}
    with _lock:
        return dict(_jobs.get(iid) or {"state": "none", "progress": 0.0})


def prepare(iid):
    """Umwandlung im Hintergrund starten (falls nicht schon fertig/laufend)."""
    if os.path.exists(cached_path(iid)):
        return status(iid)
    with _lock:
        j = _jobs.get(iid)
        if j and j["state"] == "running":
            return dict(j)
        _jobs[iid] = {"state": "running", "progress": 0.0}
    threading.Thread(target=_run, args=(iid,), daemon=True).start()
    return status(iid)


def _run(iid):
    con = connect()
    try:
        row = con.execute("SELECT path FROM items WHERE id=?", (iid,)).fetchone()
    finally:
        con.close()
    os.makedirs(CACHE, exist_ok=True)
    tmp = cached_path(iid) + ".tmp.mp4"
    try:
        convert(to_abs(row[0]), tmp, lambda p: _jobs[iid].__setitem__("progress", p))
        os.replace(tmp, cached_path(iid))
        _cleanup()
        with _lock:
            _jobs[iid] = {"state": "done", "progress": 1.0}
    except Exception as ex:
        with _lock:
            _jobs[iid] = {"state": "error", "progress": 0.0, "error": str(ex)[:200]}
        try:
            os.remove(tmp)
        except OSError:
            pass


def convert(src, dst, progress=lambda p: None):
    import av

    inp = av.open(src)
    vin = inp.streams.video[0] if inp.streams.video else None
    ain = inp.streams.audio[0] if inp.streams.audio else None
    if vin is None:
        raise ValueError("keine Videospur")
    duration = (inp.duration or 0) / 1_000_000 or None
    out = av.open(dst, "w", format="mp4", options={"movflags": "faststart"})
    copy = vin.codec_context.name in COPY_VIDEO
    if copy:
        vout = out.add_stream_from_template(vin)
    else:
        rate = vin.average_rate or 25
        vout = out.add_stream("libx264", rate=rate)
        vout.width = vin.codec_context.width - vin.codec_context.width % 2
        vout.height = vin.codec_context.height - vin.codec_context.height % 2
        vout.pix_fmt = "yuv420p"
        vout.options = {"crf": "21", "preset": "veryfast"}
    aout = None
    if ain is not None:
        aout = out.add_stream("aac", rate=48000)
        try:
            aout.layout = "stereo"
        except Exception:
            pass
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
        fifo = av.AudioFifo()
        a_next = None
    t_start = time.time()
    streams = [s for s in (vin, ain) if s is not None]
    for packet in inp.demux(*streams):
        if packet.dts is None:
            continue
        if packet.stream is vin:
            if duration and packet.pts is not None and time.time() - t_start > 0.5:
                progress(min(0.99, float(packet.pts * vin.time_base - (vin.start_time or 0) * vin.time_base) / duration))
                t_start = time.time()
            if copy:
                packet.stream = vout
                out.mux(packet)
            else:
                for fr in packet.decode():
                    fr.pts = None
                    for p in vout.encode(fr.reformat(width=vout.width, height=vout.height, format="yuv420p")):
                        out.mux(p)
        elif aout is not None:
            try:
                frames = packet.decode()
            except av.error.InvalidDataError:
                continue
            for fr in frames:
                if a_next is None:
                    # Ton zeitgleich mit dem Bild beginnen lassen (MTS starten nicht bei 0)
                    t0 = float(fr.pts * fr.time_base) if fr.pts is not None else 0.0
                    if copy and vin.start_time is not None:
                        a_next = int(round(t0 * 48000))
                    else:
                        a_next = int(round((t0 - float((vin.start_time or 0) * vin.time_base)) * 48000))
                        a_next = max(0, a_next)
                fr.pts = None
                for r in resampler.resample(fr):
                    fifo.write(r)
                while fifo.samples >= 1024:
                    f = fifo.read(1024)
                    f.pts = a_next
                    a_next += f.samples
                    for p in aout.encode(f):
                        out.mux(p)
    if not copy:
        for p in vout.encode(None):
            out.mux(p)
    if aout is not None:
        if fifo.samples:
            f = fifo.read(fifo.samples)
            f.pts = a_next
            for p in aout.encode(f):
                out.mux(p)
        for p in aout.encode(None):
            out.mux(p)
    out.close()
    inp.close()
    progress(1.0)


def remove_partial():
    """Beim Start: halbfertige Umwandlungen (FotoArchiv wurde mittendrin beendet) löschen."""
    try:
        for f in os.listdir(CACHE):
            if ".tmp" in f:
                os.remove(os.path.join(CACHE, f))
    except OSError:
        pass


def _cleanup():
    """Zwischenspeicher begrenzen: älteste Umwandlungen zuerst löschen."""
    try:
        files = [os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.endswith(".mp4") and ".tmp" not in f]
    except OSError:
        return
    files.sort(key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    while files and total > MAX_CACHE:
        f = files.pop(0)
        total -= os.path.getsize(f)
        try:
            os.remove(f)
        except OSError:
            pass
