"""Sammelt die Lizenztexte aller mitgelieferten Bestandteile nach tools/paket/Lizenzen/.

Mit der FotoArchiv-Python-Umgebung ausführen (deren site-packages sind die ausgelieferten Pakete):
  python tools/sammle_lizenzen.py
Lädt außerdem GPL-2.0 und LGPL-2.1/3.0 (für FFmpeg, libheif, LibRaw) von gnu.org.
"""
import os
import shutil
import sys
import sysconfig
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "tools", "paket", "Lizenzen")
NAMES = ("licen", "copying", "notice", "authors")

FFMPEG = """FFmpeg (in PyAV und OpenCV enthalten)
=====================================

FotoArchiv liefert FFmpeg unverändert als Teil der Python-Pakete "av" (PyAV) und
"opencv-python-headless" mit (Ordner runtime/ im Release-ZIP).

- PyAV-Build: FFmpeg mit libx264, libx265 u. a. -> GNU General Public License Version 2 oder später
  (Text: GPL-2.0.txt in diesem Ordner). Die genauen Quellen und Build-Skripte der mitgelieferten Fassung:
  https://github.com/PyAV-Org/pyav-ffmpeg (Releases) und https://github.com/PyAV-Org/PyAV
- OpenCV-Build: FFmpeg unter GNU Lesser General Public License 2.1 (LGPL-2.1.txt);
  Quellen: https://github.com/opencv/opencv-python, https://ffmpeg.org/download.html
- libheif/libde265 (in pillow_heif): LGPL-3.0 (LGPL-3.0.txt), https://github.com/strukturag/libheif
- LibRaw (in rawpy): LGPL-2.1 oder CDDL-1.0, https://github.com/LibRaw/LibRaw

Angebot: Wer den Quelltext genau dieser Fassungen nicht unter den Links findet, bekommt ihn auf
Anfrage über die Issues des FotoArchiv-Projekts (mindestens drei Jahre ab Veröffentlichung).
"""

GEONAMES = """GeoNames (models/cities.txt, models/countries.txt)
==================================================

Orts- und Länderdaten von GeoNames (https://www.geonames.org), lizenziert unter
Creative Commons Namensnennung 4.0 International (CC BY 4.0):
https://creativecommons.org/licenses/by/4.0/deed.de
Die Daten wurden für FotoArchiv auf Name, Land und Koordinaten gekürzt.
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    pk = os.path.join(OUT, "python-pakete")
    if os.path.isdir(pk):
        shutil.rmtree(pk)
    site = sysconfig.get_paths()["purelib"]
    n = 0
    for d in sorted(os.listdir(site)):
        if not d.endswith(".dist-info"):
            continue
        name = d[:-len(".dist-info")]
        for root, _dirs, files in os.walk(os.path.join(site, d)):
            for f in files:
                if any(k in f.lower() for k in NAMES):
                    rel = os.path.relpath(os.path.join(root, f), os.path.join(site, d))
                    dst = os.path.join(pk, name, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(os.path.join(root, f), dst)
                    n += 1
    py_lic = os.path.join(os.path.dirname(sys.executable), "LICENSE.txt")
    if not os.path.exists(py_lic):
        py_lic = os.path.join(sysconfig.get_paths()["stdlib"], "LICENSE.txt")
    shutil.copy2(py_lic, os.path.join(OUT, "Python_LICENSE.txt"))
    for name, url in (("GPL-2.0.txt", "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt"),
                      ("LGPL-2.1.txt", "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt"),
                      ("LGPL-3.0.txt", "https://www.gnu.org/licenses/lgpl-3.0.txt")):
        if os.path.exists(os.path.join(OUT, name)):
            continue  # schon da (z. B. per curl geladen, wenn Python keine Verbindung bekommt)
        with urllib.request.urlopen(url, timeout=30) as r, open(os.path.join(OUT, name), "wb") as f:
            f.write(r.read())
    with open(os.path.join(OUT, "FFmpeg_und_Codecs.txt"), "w", encoding="utf-8") as f:
        f.write(FFMPEG)
    with open(os.path.join(OUT, "GeoNames_CC-BY-4.0.txt"), "w", encoding="utf-8") as f:
        f.write(GEONAMES)
    print("%d Lizenzdateien aus Python-Paketen, dazu Python, GPL/LGPL, FFmpeg, GeoNames -> %s" % (n, OUT))


if __name__ == "__main__":
    main()
