"""360°-Fotos (Photo Sphere / equirektangular) erkennen.

Kennzeichen ist die Google-"GPano"-XMP-Angabe ProjectionType = equirectangular (GoPro Fusion, Ricoh Theta,
Insta360, Google-Kamera …). Geprüft werden nur Fotos, die mindestens etwa doppelt so breit wie hoch sind (volle Kugel 2:1,
Teilpanoramen mit GPano-Ausschnitt nennen ihre volle Größe). Ergebnis in items.pano: JSON mit Ausschnitt und
Blickrichtung, "" = geprüft, kein 360°-Foto, NULL = noch nicht geprüft.
"""
import json
import re

import common

FIELDS = ("FullPanoWidthPixels", "FullPanoHeightPixels", "CroppedAreaImageWidthPixels",
          "CroppedAreaImageHeightPixels", "CroppedAreaLeftPixels", "CroppedAreaTopPixels",
          "InitialViewHeadingDegrees", "InitialViewPitchDegrees", "PoseHeadingDegrees")


def _gpano(head, name):
    m = re.search(rb'GPano:%s\s*=\s*"([^"]*)"' % name.encode(), head) or \
        re.search(rb'<GPano:%s>\s*([^<]*?)\s*</GPano:%s>' % (name.encode(), name.encode()), head)
    return m.group(1).decode("utf-8", "replace").strip() if m else None


def read(path, width, height):
    """GPano-Angaben einer Datei oder None (kein 360°-Foto)."""
    try:
        with open(path, "rb") as f:
            head = f.read(1024 * 1024)  # XMP steht vorn (JPEG APP1, HEIC meta)
    except OSError:
        return None
    if (_gpano(head, "ProjectionType") or "").lower() != "equirectangular":
        return None
    d = {}
    for k in FIELDS:
        v = _gpano(head, k)
        try:
            d[k] = float(v) if v not in (None, "") else None
        except ValueError:
            d[k] = None
    fw = d["FullPanoWidthPixels"] or width
    fh = d["FullPanoHeightPixels"] or (fw / 2)
    cw = d["CroppedAreaImageWidthPixels"] or width
    ch = d["CroppedAreaImageHeightPixels"] or height
    # Angaben auf die Bildgröße im Katalog umrechnen (manche Programme verkleinern ohne die XMP-Werte anzupassen)
    s = (width / cw) if cw else 1.0
    return {"fw": round(fw * s), "fh": round(fh * s), "cw": width, "ch": height,
            "left": round((d["CroppedAreaLeftPixels"] or 0) * s), "top": round((d["CroppedAreaTopPixels"] or 0) * s),
            "heading": d["InitialViewHeadingDegrees"], "pitch": d["InitialViewPitchDegrees"]}


def detect(con, recheck=False):
    """Noch nicht geprüfte Kandidaten untersuchen. Gibt die Zahl gefundener 360°-Fotos zurück."""
    cond = "" if recheck else " AND pano IS NULL"
    rows = con.execute("SELECT id, path, width, height FROM items WHERE kind IN ('photo','raw') AND COALESCE(hidden,0) != 2 "
                       "AND width > 0 AND height > 0 AND width >= 1.9 * height%s" % cond).fetchall()
    found = 0
    for k, (iid, path, w, h) in enumerate(rows, 1):
        info = read(common.to_abs(path), w, h)
        con.execute("UPDATE items SET pano=? WHERE id=?", (json.dumps(info) if info else "", iid))
        found += 1 if info else 0
        if k % 200 == 0:
            con.commit()
    con.commit()
    return found
