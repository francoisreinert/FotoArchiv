"""Nicht-destruktive Fotobearbeitung: Einstellungen stehen als JSON in items.edit, die Datei bleibt unverändert.

Dieselben Formeln stehen in static/app.js (Abschnitt „Bearbeiten“) für die Live-Vorschau –
bei Änderungen BEIDE Stellen anpassen, sonst sieht das Ergebnis anders aus als die Vorschau.

Reihenfolge: Spiegeln → Begradigen (mit Zoom, damit keine Ecken fehlen) → Zuschneiden →
Weiß/Schwarz → Belichtung → Kontrast → Tiefen/Lichter → Wärme/Tönung → Sättigung/Dynamik →
Vignette → Schärfe. Regler laufen von -100 bis 100 (Schärfe 0 bis 100), 0 = unverändert.
"""
import json
import math

from PIL import Image, ImageFilter, ImageOps

SLIDERS = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "saturation", "vibrance",
           "temp", "tint", "vignette", "sharpen")


def parse(value):
    """items.edit (Text) → dict oder None (nichts zu tun)."""
    if not value:
        return None
    try:
        e = json.loads(value) if isinstance(value, str) else dict(value)
    except (ValueError, TypeError):
        return None
    return e if is_active(e) else None


def clean(e):
    """Vom Browser kommende Einstellungen prüfen und auf gültige Werte begrenzen."""
    if not e:
        return None
    out = {}
    for k in SLIDERS:
        v = e.get(k)
        if isinstance(v, (int, float)) and v:
            lo = 0 if k == "sharpen" else -100
            out[k] = max(lo, min(100, round(float(v), 1)))
    a = e.get("angle")
    if isinstance(a, (int, float)) and abs(a) >= 0.05:
        out["angle"] = max(-45.0, min(45.0, round(float(a), 2)))
    if e.get("flip"):
        out["flip"] = True
    c = e.get("crop")
    if isinstance(c, (list, tuple)) and len(c) == 4:
        x, y, w, h = (max(0.0, min(1.0, float(v))) for v in c)
        w, h = min(w, 1 - x), min(h, 1 - y)
        if w >= 0.02 and h >= 0.02 and (x > 0.001 or y > 0.001 or w < 0.999 or h < 0.999):
            out["crop"] = [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]
    return out or None


def is_active(e):
    return bool(e) and any(e.get(k) for k in SLIDERS + ("angle", "flip", "crop"))


def zoom_for_angle(w, h, deg):
    """Vergrößerung, damit das gedrehte Bild die ganze Fläche ohne leere Ecken füllt."""
    t = math.radians(abs(deg))
    c, s = math.cos(t), math.sin(t)
    return max((w * c + h * s) / w, (w * s + h * c) / h)


def crop_scale(e):
    """Wie stark der sichtbare Ausschnitt verkleinert ist (für schärfere Vorschaubilder)."""
    if not e:
        return 1.0
    c = e.get("crop") or [0, 0, 1, 1]
    return max(1.0, 1 / max(0.05, min(c[2], c[3])))


GRID = 16  # Zellen entlang der längeren Bildseite für die örtliche Helligkeit (Tiefen/Lichter)
LUMA = (0.2126, 0.7152, 0.0722)


def lum_grid(src):
    """Mittlere Helligkeit (0..1) je Rasterzelle eines uint8- oder float-Bildes (h, w, 3)."""
    import numpy as np

    h, w = src.shape[:2]
    gx = max(1, round(GRID * w / max(w, h)))
    gy = max(1, round(GRID * h / max(w, h)))
    xs = np.array([i * w // gx for i in range(gx)])
    ys = np.array([j * h // gy for j in range(gy)])
    wts = np.array(LUMA, dtype=np.float32) / (255.0 if src.dtype == np.uint8 else 1.0)
    sums = np.zeros((gy, gx), dtype=np.float64)
    step = max(1, 2_000_000 // max(1, w * 3))
    for y in range(0, h, step):
        lum = src[y:y + step].astype(np.float32) @ wts
        cols = np.add.reduceat(lum, xs, axis=1)
        rows = np.searchsorted(ys, np.arange(y, y + lum.shape[0]), side="right") - 1
        np.add.at(sums, rows, cols)
    xe = np.append(xs, w)
    ye = np.append(ys, h)
    counts = np.outer(np.diff(ye), np.diff(xe))
    return sums / counts


def _pre_tone(v, e):
    """Weiß/Schwarz, Belichtung und Kontrast – auf Raster-Helligkeiten (wie auf die Pixel)."""
    g = lambda k: (e.get(k) or 0) / 100.0
    blacks, whites = g("blacks"), g("whites")
    if blacks or whites:
        bp, wp = -blacks * 0.15, 1 - whites * 0.15
        v = (v - bp) / max(1e-3, wp - bp)
    if e.get("exposure"):
        v = v * 2.0 ** (g("exposure") * 2)
    if e.get("contrast"):
        v = (v - 0.5) * (1 + g("contrast")) + 0.5
    return v


def _interp(n, cells):
    """Für jede Pixelzeile/-spalte: untere Zelle, obere Zelle, Gewicht (bilinear zwischen Zellmitten)."""
    import numpy as np

    f = (np.arange(n, dtype=np.float32) + 0.5) / n * cells - 0.5
    i0 = np.clip(np.floor(f), 0, cells - 1).astype(np.int64)
    i1 = np.minimum(i0 + 1, cells - 1)
    t = np.clip(f - i0, 0, 1).astype(np.float32)
    return i0, i1, t


def _smooth(a, b, x):
    import numpy as np

    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def geometry(im, e):
    if e.get("flip"):
        im = ImageOps.mirror(im)
    w, h = im.size
    ang = e.get("angle") or 0
    s = 1.0
    if ang:
        s = zoom_for_angle(w, h, ang)
        # Positiver Winkel = im Uhrzeigersinn (wie in der Oberfläche); PIL dreht gegen den Uhrzeigersinn
        im = im.rotate(-ang, resample=Image.BICUBIC, expand=False)
    cx, cy, cw, ch = e.get("crop") or (0, 0, 1, 1)
    x0 = (0.5 + (cx - 0.5) / s) * w
    y0 = (0.5 + (cy - 0.5) / s) * h
    x1 = x0 + cw / s * w
    y1 = y0 + ch / s * h
    box = (max(0, round(x0)), max(0, round(y0)), min(w, round(x1)), min(h, round(y1)))
    if box != (0, 0, w, h):
        im = im.crop(box)
    return im


def tone(arr, e, y0=0, height=None, grid=None):
    """arr: float32 (h, w, 3) in 0..1, wird verändert. y0/height für streifenweise Verarbeitung,
    grid = lum_grid() des ganzen (unbearbeiteten) Bildes für Tiefen/Lichter."""
    import numpy as np

    g = lambda k: (e.get(k) or 0) / 100.0
    sh, hl = g("shadows"), g("highlights")
    if (sh or hl) and grid is None:
        grid = lum_grid(arr)
    blacks, whites = g("blacks"), g("whites")
    if blacks or whites:
        bp, wp = -blacks * 0.15, 1 - whites * 0.15
        arr -= bp
        arr /= max(1e-3, wp - bp)
    if e.get("exposure"):
        arr *= 2.0 ** (g("exposure") * 2)
    if e.get("contrast"):
        arr -= 0.5
        arr *= 1 + g("contrast")
        arr += 0.5
    if sh or hl:
        # Umgebungshelligkeit (Raster, bilinear) entscheidet, was Tiefe und was Licht ist
        gb = np.clip(_pre_tone(grid, e), 0, 1).astype(np.float32)
        h, w = arr.shape[:2]
        full_h = height or h
        gy, gx = gb.shape
        r0, r1, rt = _interp(full_h, gy)
        c0, c1, ct = _interp(w, gx)
        r0, r1, rt = r0[y0:y0 + h], r1[y0:y0 + h], rt[y0:y0 + h][:, None]
        top = gb[r0][:, c0] * (1 - ct) + gb[r0][:, c1] * ct
        bot = gb[r1][:, c0] * (1 - ct) + gb[r1][:, c1] * ct
        lb = top * (1 - rt) + bot * rt
        lum = np.clip(arr @ np.array(LUMA, dtype=np.float32), 1e-4, 1)
        ln = lum
        if sh:
            k = 1 + 1.5 * abs(sh) * (1 - _smooth(0.0, 0.6, lb))
            ln = ln ** (1 / k) if sh > 0 else ln ** k
        if hl:
            k = 1 + 1.5 * abs(hl) * _smooth(0.4, 1.0, lb)
            ln = 1 - (1 - ln) ** (k if hl > 0 else 1 / k)
        # Helligkeit voll, Farbe beim Aufhellen nur mit der Wurzel des Faktors – sonst wirkt es grell
        f = ln / lum
        cf = np.where(f > 1, np.sqrt(f), f)[..., None]
        lum3 = lum[..., None]
        arr -= lum3
        arr *= cf
        arr += ln[..., None]
        # Kanal über 1: Sättigung zurücknehmen statt abschneiden – sonst kippt der Farbton (Rot wird Pink)
        m = arr.max(axis=2)
        over = m > 1
        if over.any():
            lnn = ln[..., None]
            s = np.where(over, (1 - ln) / np.maximum(m - ln, 1e-6), 1)[..., None]
            arr -= lnn
            arr *= s
            arr += lnn
    t, ti = g("temp"), g("tint")
    if t or ti:
        arr[..., 0] *= 1 + t * 0.12
        arr[..., 2] *= 1 - t * 0.12
        arr[..., 1] *= 1 - ti * 0.10
    sat, vib = g("saturation"), g("vibrance")
    if sat or vib:
        np.clip(arr, 0, 1, out=arr)
        lum = (arr @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))[..., None]
        f = 1 + sat
        if vib:
            cur = arr.max(axis=2, keepdims=True) - arr.min(axis=2, keepdims=True)
            f = f * (1 + vib * (1 - cur))
        arr -= lum
        arr *= f
        arr += lum
    vig = g("vignette")
    if vig:
        h, w = arr.shape[:2]
        full_h = height or h
        yy = ((np.arange(y0, y0 + h, dtype=np.float32) + 0.5) / full_h - 0.5)[:, None]
        xx = ((np.arange(w, dtype=np.float32) + 0.5) / w - 0.5)[None, :]
        r2 = (xx * xx + yy * yy) / 0.5
        arr *= (1 + vig * 0.8 * r2 ** 1.5)[..., None]
    np.clip(arr, 0, 1, out=arr)
    return arr


def apply(im, e):
    """Bearbeitung auf ein (bereits richtig gedrehtes) Bild anwenden. Gibt ein RGB-Bild zurück."""
    import numpy as np

    e = parse(e) if not isinstance(e, dict) else e
    if not e:
        return im
    im = geometry(im.convert("RGB"), e)
    if any(e.get(k) for k in SLIDERS if k != "sharpen"):
        w, h = im.size
        src = np.asarray(im)
        out = np.empty_like(src)
        grid = lum_grid(src) if (e.get("shadows") or e.get("highlights")) else None
        step = max(1, 4_000_000 // max(1, w * 3))  # streifenweise: wenig Speicher auch bei 50 MP
        for y in range(0, h, step):
            a = src[y:y + step].astype(np.float32) / 255.0
            tone(a, e, y, h, grid)
            out[y:y + step] = (a * 255.0 + 0.5).astype(np.uint8)
        im = Image.fromarray(out, "RGB")
    if e.get("sharpen"):
        r = max(1.0, max(im.size) / 1600)
        im = im.filter(ImageFilter.UnsharpMask(radius=r, percent=int(e["sharpen"] * 1.5), threshold=2))
    return im
