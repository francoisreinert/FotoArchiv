"""Lesen und Verarbeiten einzelner Medien-Dateien (läuft in Worker-Prozessen).

process(task) liefert Metadaten, Vorschaubild und gefundene Gesichter zurück.
Die Datenbank wird hier nicht angefasst, das macht der Indexer.
"""
import datetime
import io
import os
import re
import xml.etree.ElementTree as ET

from PIL import Image, ImageFile, ImageOps

from common import MODELS_DIR

Image.MAX_IMAGE_PIXELS = 400_000_000
# Fotos mit abgeschnittenem Dateiende trotzdem lesen (fehlender Rest erscheint grau) statt sie auszulassen
ImageFile.LOAD_TRUNCATED_IMAGES = True
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

PROC_VERSION = 3
THUMB_SHORT = 360
THUMB_LONG_MAX = 720
FACE_MAX_SIDE = 800
DECODE_SIDE = 1280
FACE_MIN_PX = 28
FACE_MIN_SCORE = 0.82
CROP_SIZE = 160

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "mwg": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
    "stDim": "http://ns.adobe.com/xap/1.0/sType/Dimensions#",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "MY": "http://ns.mylollc.com/MyloEdit/",
    "iptcExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "MP": "http://ns.microsoft.com/photo/1.2/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "MPRI": "http://ns.microsoft.com/photo/1.2/t/RegionInfo#",
    "MPReg": "http://ns.microsoft.com/photo/1.2/t/Region#",
}


def q(prefix, local):
    return "{%s}%s" % (NS[prefix], local)


# ---------------------------------------------------------------- Datum ----

DT_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")
NAME_DT_RE = re.compile(r"(19[5-9]\d|20[0-4]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?([0-2]\d|3[01])(?:[ _T-]?([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d))?")


def parse_dt(s):
    if not s:
        return None
    if isinstance(s, bytes):
        s = s.decode("ascii", "ignore")
    m = DT_RE.search(str(s))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
    if not (1900 < y < 2100 and 1 <= mo <= 12 and 1 <= d <= 31) or (y == 1970 and mo == 1 and d == 1):
        return None
    try:
        return datetime.datetime(y, mo, d, min(hh, 23), min(mm, 59), min(ss, 59)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def date_from_name(path):
    name = os.path.basename(path)
    m = NAME_DT_RE.search(name)
    if m:
        try:
            dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                   int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ XMP ----

def _val(el, prefix, local):
    """Wert einer XMP-Eigenschaft: als Attribut oder als Kind-Element."""
    v = el.get(q(prefix, local))
    if v is not None:
        return v
    c = el.find(q(prefix, local))
    if c is None:
        return None
    lis = c.findall(".//" + q("rdf", "li"))
    if lis:
        return [li.text.strip() for li in lis if li.text and li.text.strip()]
    return (c.text or "").strip() or None


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _num(v):
    try:
        return float(_first(v))
    except (TypeError, ValueError):
        return None


def _xmp_gps(v):
    # Formate: "48,8.1234N" oder "48,8,7N" oder "48.135N"
    v = _first(v)
    if not v:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?(?:,(\d+(?:\.\d+)?))?\s*([NSEW])\s*$", v)
    if not m:
        return None
    deg = float(m.group(1)) + float(m.group(2) or 0) / 60 + float(m.group(3) or 0) / 3600
    return -deg if m.group(4) in "SW" else deg


def parse_xmp(data):
    """Liest die für uns interessanten Felder aus einem XMP-Paket."""
    out = {}
    if not data:
        return out
    if isinstance(data, bytes):
        data = data.decode("utf-8", "ignore")
    start = data.find("<x:xmpmeta")
    if start < 0:
        start = data.find("<rdf:RDF")
    end_tag = "</x:xmpmeta>" if "</x:xmpmeta>" in data else "</rdf:RDF>"
    end = data.find(end_tag)
    if start < 0 or end < 0:
        return out
    try:
        root = ET.fromstring(data[start:end + len(end_tag)])
    except ET.ParseError:
        return out
    descs = root.iter(q("rdf", "Description"))
    regions, persons, keywords = [], [], []
    for d in descs:
        for key, prefix, local in (
            ("dt_photoshop", "photoshop", "DateCreated"),
            ("dt_exif", "exif", "DateTimeOriginal"),
            ("dt_xmp", "xmp", "CreateDate"),
            ("rating", "xmp", "Rating"),
            ("flag", "MY", "flag"),
            ("undated", "MY", "Undated"),
            ("gps_lat", "exif", "GPSLatitude"),
            ("gps_lon", "exif", "GPSLongitude"),
            ("orientation", "tiff", "Orientation"),
        ):
            v = _val(d, prefix, local)
            if v is not None and key not in out:
                out[key] = _first(v)
        for prefix, local in (("dc", "subject"),):
            v = _val(d, prefix, local)
            if v:
                keywords += v if isinstance(v, list) else [v]
        for prefix, local in (("dc", "description"), ("dc", "title")):
            v = _first(_val(d, prefix, local))
            if v and "caption" not in out:
                out["caption"] = v
        v = _val(d, "iptcExt", "PersonInImage")
        if v:
            persons += v if isinstance(v, list) else [v]

    # MWG-Regionen (Mylio, Lightroom, digiKam, Picasa ...)
    for reg in root.iter(q("mwg", "Regions")):
        holder = reg
        inner = reg.find(q("rdf", "Description"))
        if inner is not None:
            holder = inner
        dims = holder.find(q("mwg", "AppliedToDimensions"))
        aw = ah = None
        if dims is not None:
            dd = dims.find(q("rdf", "Description"))
            dd = dd if dd is not None else dims
            aw, ah = _num(_val(dd, "stDim", "w")), _num(_val(dd, "stDim", "h"))
        for li in holder.iter(q("rdf", "li")):
            el = li.find(q("rdf", "Description"))
            el = el if el is not None else li
            name = _first(_val(el, "mwg", "Name"))
            rtype = _first(_val(el, "mwg", "Type")) or "Face"
            if rtype.lower() != "face":
                continue
            area = el.find(q("mwg", "Area"))
            if area is None:
                continue
            ad = area.find(q("rdf", "Description"))
            ad = ad if ad is not None else area
            x, y, w, h = (_num(_val(ad, "stArea", k)) for k in "xywh")
            if None in (x, y, w, h):
                continue
            unit = _first(_val(ad, "stArea", "unit")) or "normalized"
            if unit != "normalized" and aw and ah:
                x, w, y, h = x / aw, w / aw, y / ah, h / ah
            regions.append({"name": name.strip() if name else None,
                            "x": x - w / 2, "y": y - h / 2, "w": w, "h": h,
                            "applied": (aw, ah)})
    # Microsoft-Regionen (Windows Fotogalerie)
    for li in root.iter(q("MPRI", "Regions")):
        for r in li.iter(q("rdf", "li")):
            el = r.find(q("rdf", "Description"))
            el = el if el is not None else r
            rect = _first(_val(el, "MPReg", "Rectangle"))
            name = _first(_val(el, "MPReg", "PersonDisplayName"))
            if rect and name:
                try:
                    x, y, w, h = (float(t) for t in rect.split(","))
                    regions.append({"name": name, "x": x, "y": y, "w": w, "h": h, "applied": (None, None)})
                except ValueError:
                    pass
    if regions:
        out["regions"] = regions
    if persons:
        out["persons"] = sorted(set(p.strip() for p in persons if p and p.strip()))
    if keywords:
        out["keywords"] = sorted(set(k.strip() for k in keywords if k and k.strip()))
    return out


def sidecar_paths(path):
    stem, _ = os.path.splitext(path)
    return [stem + ".xmp", stem + ".XMP", path + ".xmp", path + ".XMP"]


def find_sidecar(path):
    for p in sidecar_paths(path):
        if os.path.isfile(p):
            return p
    return None


# ----------------------------------------------------------------- EXIF ----

def _ratio(v):
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            return v[0] / v[1]
        except Exception:
            return None


def exif_info(exif):
    out = {}
    if not exif:
        return out
    try:
        ifd = exif.get_ifd(0x8769)
    except Exception:
        ifd = {}
    out["dt"] = parse_dt(ifd.get(0x9003) or ifd.get(0x9004) or exif.get(0x0132))
    make = (exif.get(0x010F) or "").strip().strip("\x00")
    model = (exif.get(0x0110) or "").strip().strip("\x00")
    if model and make and model.lower().startswith(make.split()[0].lower()):
        out["camera"] = model
    elif make or model:
        out["camera"] = (make + " " + model).strip()
    out["orientation"] = exif.get(0x0112) or 1
    try:
        gps = exif.get_ifd(0x8825)
    except Exception:
        gps = {}
    if gps and 2 in gps and 4 in gps:
        try:
            lat = sum(_ratio(v) / d for v, d in zip(gps[2], (1, 60, 3600)))
            lon = sum(_ratio(v) / d for v, d in zip(gps[4], (1, 60, 3600)))
            if gps.get(1) in ("S", b"S"):
                lat = -lat
            if gps.get(3) in ("W", b"W"):
                lon = -lon
            if (abs(lat) > 0.0001 or abs(lon) > 0.0001) and abs(lat) <= 90 and abs(lon) <= 180:
                out["lat"], out["lon"] = lat, lon
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- Laden ----

def open_image(path, kind, max_side=None, orient_override=None, userrot=0, edit=None):
    """Öffnet ein Foto oder RAW. Gibt (Bild, exif, xmp-bytes, Originalgröße, Ausrichtung) zurück.

    Das Bild ist nach der EXIF-Ausrichtung der Datei gedreht (wie in Mylio und im Browser).
    Die in Mylio/XMP gespeicherte Ausrichtung ist oft veraltet und wird daher nicht benutzt.
    userrot: zusätzliche Drehung in Grad im Uhrzeigersinn (in FotoArchiv vom Benutzer gedreht).
    max_side erlaubt schnelles, verkleinertes Dekodieren von JPEGs.
    edit: Bearbeitung aus items.edit (nur für Anzeige/Export – Erkennung arbeitet mit dem Original).
    """
    if edit:
        import edit as editmod

        edit = editmod.parse(edit)
        if edit and max_side:
            max_side = int(max_side * editmod.crop_scale(edit))  # Ausschnitt soll scharf bleiben
    im, exif, xmp, size, applied = _open_image(path, kind, max_side)
    if orient_override and orient_override in range(1, 9) and orient_override != applied:
        im = reorient(im, applied, orient_override)
        if (applied in (5, 6, 7, 8)) != (orient_override in (5, 6, 7, 8)):
            size = (size[1], size[0])
        applied = orient_override
    if userrot and userrot % 360:
        im = rotate_cw(im, userrot)
        if userrot % 180:
            size = (size[1], size[0])
    if edit:
        im = editmod.apply(im, edit)
    return im, exif, xmp, size, applied


def rotate_cw(im, deg):
    deg %= 360
    return im.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}[deg]) if deg else im


_ORIENT_OPS = {2: [Image.FLIP_LEFT_RIGHT], 3: [Image.ROTATE_180], 4: [Image.FLIP_TOP_BOTTOM],
               5: [Image.TRANSPOSE], 6: [Image.ROTATE_270], 7: [Image.TRANSVERSE], 8: [Image.ROTATE_90]}
_ORIENT_UNDO = {2: [Image.FLIP_LEFT_RIGHT], 3: [Image.ROTATE_180], 4: [Image.FLIP_TOP_BOTTOM],
                5: [Image.TRANSPOSE], 6: [Image.ROTATE_90], 7: [Image.TRANSVERSE], 8: [Image.ROTATE_270]}


def reorient(im, applied, wanted):
    """Bild, das nach Ausrichtung 'applied' gedreht ist, auf Ausrichtung 'wanted' umstellen."""
    for op in _ORIENT_UNDO.get(applied, []):
        im = im.transpose(op)
    for op in _ORIENT_OPS.get(wanted, []):
        im = im.transpose(op)
    return im


def _open_image(path, kind, max_side=None):
    xmp = None
    exif = None
    if kind == "raw":
        import rawpy

        try:
            with Image.open(path) as tif:
                exif = tif.getexif()
        except Exception:
            exif = None
        with rawpy.imread(path) as raw:
            try:
                th = raw.extract_thumb()
                if th.format == rawpy.ThumbFormat.JPEG:
                    im = Image.open(io.BytesIO(th.data))
                    if max_side:
                        im.draft("RGB", (max_side, max_side))
                    im.load()
                else:
                    im = Image.fromarray(th.data)
            except Exception:
                im = Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
            size = (raw.sizes.width, raw.sizes.height)
        im = _fast_reduce(im, max_side)
        orient = (exif.get(0x0112) if exif else 1) or 1
        if orient in (5, 6, 7, 8):
            size = (size[1], size[0])
        im = _apply_orientation(im, orient, size)
        if im.mode != "RGB":
            im = im.convert("RGB")
        return im, exif, None, size, orient

    im = Image.open(path)
    exif = im.getexif()
    # HEIC: libheif dreht schon selbst, die Ursprungs-Ausrichtung steht in info
    region_orient = im.info.get("original_orientation") or exif.get(0x0112) or 1
    xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    size = im.size
    orient = exif.get(0x0112) or 1
    if im.format == "JPEG" and max_side:
        im.draft("RGB", (max_side, max_side))
    if getattr(im, "n_frames", 1) > 1 and im.format != "PSD":  # PSD: ohne seek = Gesamtbild statt Ebene
        im.seek(0)
    im.load()
    if im.format == "JPEG" and "xmp" in im.info:
        xmp = im.info["xmp"]
    im = _fast_reduce(im, max_side)
    if orient in (5, 6, 7, 8):
        size = (size[1], size[0])
    im = ImageOps.exif_transpose(im) if orient != 1 else im
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        try:
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            im = bg
        except Exception:
            im = im.convert("RGB")
    elif im.mode == "L":
        im = im.convert("RGB")
    return im, exif, xmp, size, region_orient


def _fast_reduce(im, max_side):
    """HEIC/PNG/RAW-Vorschauen lassen sich nicht verkleinert dekodieren: schnell per Mittelwert
    verkleinern, statt alle weiteren Schritte mit 12+ Megapixeln zu rechnen."""
    if not max_side or max(im.size) < 2 * max_side:
        return im
    try:
        return im.reduce(max(im.size) // max_side)
    except (ValueError, NotImplementedError):
        return im


def _apply_orientation(im, orient, target_size):
    # Eingebettete RAW-Vorschauen sind teils schon gedreht.
    if orient in (5, 6, 7, 8) and (im.width > im.height) == (target_size[0] > target_size[1]):
        return im
    for op in _ORIENT_OPS.get(orient, []):
        im = im.transpose(op)
    return im


def file_exif_date(path, kind):
    """Aufnahmedatum direkt aus der Datei (schnell, ohne das Bild zu dekodieren)."""
    try:
        with Image.open(path) as im:
            return exif_info(im.getexif()).get("dt")
    except Exception:
        return None


def _dt_seconds(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


def make_thumb(im):
    w, h = im.size
    s = THUMB_SHORT / min(w, h)
    if max(w, h) * s > THUMB_LONG_MAX:
        s = THUMB_LONG_MAX / max(w, h)
    if s < 1:
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BICUBIC, reducing_gap=2.0)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()


def dhash(im):
    """64-Bit-Differenz-Hash zum Erkennen gleicher Bilder (auch nach Neu-Speichern)."""
    g = im.convert("L").resize((9, 8), Image.BILINEAR)
    px = list(g.getdata())
    v = 0
    for r in range(8):
        for c in range(8):
            v = (v << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return v - (1 << 64) if v >= (1 << 63) else v


def make_preview(path, kind, max_side=2560, userrot=0, edit=None):
    if kind == "video":
        im = rotate_cw(video_frame(path)[0], userrot or 0)
    else:
        im = open_image(path, kind, max_side=max_side, userrot=userrot or 0, edit=edit)[0]
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


# ---------------------------------------------------------------- Video ----

def _iso6709(s):
    m = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", s or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def video_frame(path):
    import av

    meta = {}
    with av.open(path) as c:
        md = {k.lower(): v for k, v in (c.metadata or {}).items()}
        vs = c.streams.video[0] if c.streams.video else None
        if vs is None:
            raise ValueError("keine Videospur")
        for s in c.streams:
            md.update({k.lower(): v for k, v in (s.metadata or {}).items() if k.lower() not in md})
        if c.duration:
            meta["duration"] = c.duration / 1_000_000
        elif vs.duration and vs.time_base:
            meta["duration"] = float(vs.duration * vs.time_base)
        apple_dt = md.get("com.apple.quicktime.creationdate")
        if apple_dt:
            meta["dt"] = parse_dt(apple_dt)
        elif md.get("creation_time"):
            try:
                t = datetime.datetime.fromisoformat(md["creation_time"].replace("Z", "+00:00"))
                if t.tzinfo:
                    t = t.astimezone()
                meta["dt"] = parse_dt(t.strftime("%Y-%m-%d %H:%M:%S"))
            except ValueError:
                meta["dt"] = parse_dt(md["creation_time"])
        loc = _iso6709(md.get("com.apple.quicktime.location.iso6709") or md.get("location"))
        if loc and (abs(loc[0]) > 0.0001 or abs(loc[1]) > 0.0001):
            meta["lat"], meta["lon"] = loc
        model = md.get("com.apple.quicktime.model")
        if model:
            meta["camera"] = model
        target = min(1.0, (meta.get("duration") or 0) * 0.1)
        if target > 0 and vs.time_base:
            try:
                c.seek(int(target / vs.time_base), stream=vs, any_frame=False, backward=True)
            except Exception:
                pass
        frame = None
        for frame in c.decode(vs):
            break
        if frame is None:
            raise ValueError("kein Videobild")
        im = frame.to_image()
        rot = getattr(frame, "rotation", 0) or 0
        if rot:
            im = im.rotate(rot, expand=True)
        meta["size"] = im.size
    return im.convert("RGB"), meta


# ------------------------------------------------------------- Gesichter ----

_det = None
_rec = None


def _models():
    global _det, _rec
    if _det is None:
        import cv2

        cv2.setNumThreads(1)
        _det = cv2.FaceDetectorYN.create(
            os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx"), "", (320, 320), FACE_MIN_SCORE, 0.3, 5000)
        _rec = cv2.FaceRecognizerSF.create(
            os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx"), "")
    return _det, _rec


def _detect(bgr):
    det, _ = _models()
    h, w = bgr.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(bgr)
    return [] if faces is None else list(faces)


def _embed(bgr, face_row):
    import numpy as np

    _, rec = _models()
    aligned = rec.alignCrop(bgr, face_row)
    f = rec.feature(aligned).flatten().astype("float32")
    n = float(np.linalg.norm(f))
    return (f / n).tobytes() if n > 0 else None


def _crop(im, x, y, w, h):
    cx, cy, s = x + w / 2, y + h / 2, max(w, h) * 1.5
    box = (int(cx - s / 2), int(cy - s / 2), int(cx + s / 2), int(cy + s / 2))
    c = im.crop(box).resize((CROP_SIZE, CROP_SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    c.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _iou(a, b):
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _match(region, det):
    """Ähnlichkeit zwischen Mylio-Region und erkanntem Gesicht (0 = passt nicht)."""
    iou = _iou(region, det)
    rcx, rcy = region[0] + region[2] / 2, region[1] + region[3] / 2
    dcx, dcy = det[0] + det[2] / 2, det[1] + det[3] / 2
    inside = abs(rcx - dcx) < region[2] / 2 and abs(rcy - dcy) < region[3] / 2
    ratio = (det[2] * det[3]) / max(1e-9, region[2] * region[3])
    if inside and 0.15 < ratio < 6:
        return max(iou, 0.2)
    return iou if iou >= 0.2 else 0.0


def _orient_rect(r, orient):
    """Rechteck aus Sicht der gespeicherten Pixel in angezeigte Ausrichtung umrechnen."""
    x, y, w, h = r
    if orient == 2:
        return (1 - x - w, y, w, h)
    if orient == 3:
        return (1 - x - w, 1 - y - h, w, h)
    if orient == 4:
        return (x, 1 - y - h, w, h)
    if orient == 5:
        return (y, x, h, w)
    if orient == 6:
        return (1 - y - h, x, h, w)
    if orient == 7:
        return (1 - y - h, 1 - x - w, h, w)
    if orient == 8:
        return (y, 1 - x - w, h, w)
    return r


def find_faces(im, regions, orient):
    """Erkennt Gesichter und ordnet vorhandene Namens-Regionen (Mylio) zu."""
    import numpy as np

    scale = min(1.0, FACE_MAX_SIDE / max(im.size))
    small = im if scale >= 1 else im.resize((round(im.width * scale), round(im.height * scale)), Image.BILINEAR)
    bgr = np.ascontiguousarray(np.asarray(small)[:, :, ::-1])
    W, H = small.size
    dets = []
    for row in _detect(bgr):
        x, y, w, h = (float(v) for v in row[:4])
        if min(w, h) < FACE_MIN_PX * min(1.0, W / 640):
            continue
        dets.append({"row": row, "rect": (x / W, y / H, w / W, h / H), "score": float(row[14])})

    # Für jede Namens-Region die passende Ausrichtung finden.
    named = []
    for r in regions or []:
        if not r.get("name"):
            continue
        raw = (r["x"], r["y"], r["w"], r["h"])
        cands = [raw]
        if orient and orient != 1:
            cands.append(_orient_rect(raw, orient))
        best = max(cands, key=lambda c: max([_match(c, d["rect"]) for d in dets] or [0]))
        if max([_match(best, d["rect"]) for d in dets] or [0]) == 0 and len(cands) > 1:
            aw, ah = r.get("applied") or (None, None)
            if aw and ah and (aw > ah) != (im.width > im.height):
                best = cands[1]
            else:
                best = cands[0]
        named.append((r["name"], best))

    used = set()
    out = []
    for name, rect in named:
        scored = sorted(((_match(rect, d["rect"]), i) for i, d in enumerate(dets) if i not in used), reverse=True)
        if scored and scored[0][0] > 0:
            i = scored[0][1]
            used.add(i)
            d = dets[i]
            out.append(_face_out(im, small, bgr, d["row"], d["rect"], d["score"], name, "mylio"))
            continue
        # Gesicht wurde nicht automatisch gefunden: im Ausschnitt nochmal suchen.
        out.append(_region_only(im, rect, name))
    for i, d in enumerate(dets):
        if i not in used:
            out.append(_face_out(im, small, bgr, d["row"], d["rect"], d["score"], None, "det"))
    return out


def _face_out(im, small, bgr, row, rect, score, name, source):
    x, y, w, h = rect
    return {
        "x": x, "y": y, "w": w, "h": h, "score": score, "name": name, "source": source,
        "px": int(min(w * im.width, h * im.height)),
        "emb": _embed(bgr, row),
        "crop": _crop(im, x * im.width, y * im.height, w * im.width, h * im.height),
    }


def _region_only(im, rect, name):
    import numpy as np

    x, y, w, h = rect
    X, Y, Wd, Hd = x * im.width, y * im.height, w * im.width, h * im.height
    face = {"x": x, "y": y, "w": w, "h": h, "score": None, "name": name, "source": "mylio",
            "px": int(min(Wd, Hd)), "emb": None, "crop": None}
    if Wd < 8 or Hd < 8:
        return face
    s = max(Wd, Hd) * 2.2
    cx, cy = X + Wd / 2, Y + Hd / 2
    box = (int(max(0, cx - s / 2)), int(max(0, cy - s / 2)), int(min(im.width, cx + s / 2)), int(min(im.height, cy + s / 2)))
    crop = im.crop(box)
    k = 320 / max(crop.size)
    crop = crop.resize((max(1, round(crop.width * k)), max(1, round(crop.height * k))), Image.BILINEAR)
    cbgr = np.ascontiguousarray(np.asarray(crop)[:, :, ::-1])
    rows = _detect(cbgr)
    if rows:
        ccx, ccy = crop.width / 2, crop.height / 2
        row = min(rows, key=lambda r: (r[0] + r[2] / 2 - ccx) ** 2 + (r[1] + r[3] / 2 - ccy) ** 2)
        face["emb"] = _embed(cbgr, row)
        face["score"] = float(row[14])
    face["crop"] = _crop(im, X, Y, Wd, Hd)
    return face


# ------------------------------------------------------------- Hauptteil ----

def process(task):
    """task: dict(id, path, kind, faces, mylio). Ergebnis als dict, Fehler als 'error'.

    mylio: Daten aus dem Mylio-Katalog (o = Drehung, faces = [[Name, x, y, w, h], ...])."""
    path, kind = task["path"], task["kind"]
    my = task.get("mylio") or {}
    res = {"id": task["id"], "meta": {}, "faces": None, "thumb": None}
    meta = res["meta"]
    try:
        xmp = {}
        sc = find_sidecar(path)
        if sc:
            with open(sc, "rb") as f:
                xmp = parse_xmp(f.read())
        if task.get("shared") and (xmp or my) and kind != "video":
            # Mehrere Dateien mit gleichem Namen: XMP/Mylio-Daten nur übernehmen, wenn das Datum passt
            own = file_exif_date(path, kind)
            ref = parse_dt(xmp.get("dt_exif")) or parse_dt(my.get("d"))
            if not own or not ref or abs(_dt_seconds(own) - _dt_seconds(ref)) > 120:
                xmp, my = {}, {}
        if kind == "video":
            im, vm = video_frame(path)
            meta.update({k: v for k, v in vm.items() if k != "size"})
            meta["width"], meta["height"] = vm["size"]
            ex = {}
            orient = 1
        else:
            userrot = task.get("userrot") or 0
            im, exif, emb_xmp, size, orient = open_image(path, kind, max_side=DECODE_SIDE, userrot=userrot)
            ex = exif_info(exif)
            meta["orient"] = orient
            # Vom Benutzer gedreht: Browser würde das Original falsch zeigen, daher Vorschau nutzen
            meta["rotfix"] = 1 if userrot % 360 else 0
            if emb_xmp:
                inner = parse_xmp(emb_xmp)
                for k, v in inner.items():
                    xmp.setdefault(k, v)
            meta["width"], meta["height"] = size
            if ex.get("camera"):
                meta["camera"] = ex["camera"]
        # Datum: Mylio-Korrektur > XMP > EXIF > Video > Dateiname
        dt, src = None, None
        for cand, s in ((xmp.get("dt_photoshop"), "xmp"), (xmp.get("dt_exif"), "xmp"),
                        (ex.get("dt"), "exif"), (meta.get("dt"), "video"), (xmp.get("dt_xmp"), "xmp"),
                        (date_from_name(path), "name")):
            dt = parse_dt(cand)
            if dt:
                src = s
                # Nur-Datum aus photoshop:DateCreated mit Uhrzeit aus EXIF ergänzen
                if s == "xmp" and cand == xmp.get("dt_photoshop") and len(str(cand)) <= 10:
                    other = ex.get("dt") or parse_dt(xmp.get("dt_exif"))
                    if other and other[:10] == dt[:10]:
                        dt = other
                break
        if xmp.get("undated") == "true":
            dt, src = None, "undated"
        elif not dt:
            dt = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
            src = "file"
        meta["taken"], meta["taken_src"] = dt, src
        meta.pop("dt", None)
        if "lat" in ex:
            meta["lat"], meta["lon"] = ex["lat"], ex["lon"]
        elif xmp.get("gps_lat") and xmp.get("gps_lon"):
            la, lo = _xmp_gps(xmp["gps_lat"]), _xmp_gps(xmp["gps_lon"])
            if la is not None and lo is not None:
                meta["lat"], meta["lon"] = la, lo
        try:
            meta["rating"] = int(float(xmp.get("rating") or 0))
        except ValueError:
            meta["rating"] = 0
        meta["flag"] = 1 if xmp.get("flag") == "true" else 0
        meta["keywords"] = ", ".join(xmp.get("keywords") or []) or None
        meta["caption"] = xmp.get("caption")
        if my.get("faces"):
            # Mylio-Katalog ist vollständiger als die XMP-Dateien
            xmp["regions"] = [{"name": n, "x": x, "y": y, "w": w, "h": h, "applied": (None, None)}
                              for n, x, y, w, h in my["faces"]]
        res["thumb"] = make_thumb(im)
        meta["phash"] = dhash(im)
        if task.get("faces") and kind != "video":
            res["faces"] = find_faces(im, xmp.get("regions"), orient)
        else:
            res["faces"] = [{"name": r["name"], "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"],
                             "score": None, "source": "mylio", "px": 0, "emb": None, "crop": None}
                            for r in xmp.get("regions") or [] if r.get("name")]
        tagged = {f["name"] for f in res["faces"] if f.get("name")}
        for p in xmp.get("persons") or []:
            if p not in tagged:
                res["faces"].append({"name": p, "x": None, "y": None, "w": None, "h": None, "score": None,
                                     "source": "mylio", "px": 0, "emb": None, "crop": None})
    except Exception as e:  # beschädigte Dateien sollen den Lauf nicht stoppen
        res["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    return res
