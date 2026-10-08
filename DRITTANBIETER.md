# Verwendete fremde Software und Daten

FotoArchiv selbst steht unter der GPL-3.0 (siehe `LICENSE`). Es bringt die folgenden Bestandteile anderer
Projekte mit; sie stehen unter ihren eigenen Lizenzen, die Rechte liegen bei den jeweiligen Autorinnen und
Autoren. **Die vollständigen Lizenztexte liegen im Ordner `Lizenzen/`** (Python-Pakete unter
`Lizenzen/python-pakete/`, FFmpeg-Hinweis mit Quelltext-Angebot in `Lizenzen/FFmpeg_und_Codecs.txt`).

## Daten und Modelle (Ordner `models/`)

| Bestandteil | Lizenz | Quelle |
|---|---|---|
| Orts- und Länderdaten (`cities.txt`, `countries.txt`) | CC BY 4.0 – **Daten von GeoNames** | https://www.geonames.org |
| Gesichtsfinder YuNet (`face_detection_yunet_2023mar.onnx`) | MIT | https://github.com/opencv/opencv_zoo |
| Gesichtserkennung SFace (`face_recognition_sface_2021dec.onnx`) | Apache-2.0 | https://github.com/opencv/opencv_zoo |
| Textdetektion PP-OCRv3 (`text_detection_ppocr.onnx`, für „Dokumente finden“) | Apache-2.0 (© PaddlePaddle Authors) | https://github.com/opencv/opencv_zoo |

## Laufzeit (Ordner `runtime/`, nur im fertigen ZIP)

Python 3.12 aus *python-build-standalone* (Python Software Foundation License; enthält u. a. OpenSSL –
Apache-2.0, SQLite – Public Domain, zlib) – https://github.com/astral-sh/python-build-standalone

Python-Pakete (Angaben aus den Paketdaten):

| Paket | Version | Lizenz |
|---|---|---|
| av (PyAV) | 19.0.1 | BSD-3-Clause – enthält **FFmpeg** mit x264/x265, libvpx, dav1d, SVT-AV1, opus u. a. (FFmpeg-Build unter **GPL-2.0-or-later**, Quelltext: https://ffmpeg.org, https://github.com/PyAV-Org/pyav-ffmpeg) |
| opencv-python-headless | 4.10.0.84 | Apache-2.0 (enthält FFmpeg, LGPL-2.1) |
| numpy | 2.5.3 | BSD-3-Clause u. a. |
| pillow | 12.3.0 | MIT-CMU |
| pillow_heif | 1.8.0 | BSD-3-Clause (enthält libheif/libde265, LGPL-3.0) |
| rawpy | 0.27.1 | MIT (enthält LibRaw, LGPL-2.1 / CDDL-1.0) |
| icloudpd | 1.32.3 | MIT |
| samsungtvws | 3.0.6 | LGPL-3.0 |
| segno | 1.6.6 | BSD-3-Clause |
| requests 2.32.3, urllib3 1.26.20, idna 3.20, charset-normalizer 3.5.2 | | Apache-2.0 / MIT / BSD-3-Clause / MIT |
| certifi | 2025.4.26 | MPL-2.0 |
| keyring 25.6.0, keyrings.alt 5.0.2, jaraco.* , more-itertools, six, schema, tzlocal, pytz, piexif, blinker | | MIT |
| Flask 3.1.1, Werkzeug 3.1.9, Jinja2 3.1.6, MarkupSafe 3.0.4, click 8.5.0, itsdangerous 2.2.0, colorama, srp, pywin32-ctypes | | BSD-3-Clause |
| multidict, propcache, yarl, websocket-client, tzdata | | Apache-2.0 |
| tqdm | 4.67.1 | MIT / MPL-2.0 |
| waitress | 3.0.2 | Zope Public License 2.1 |
| typing_extensions | 4.14.0 | PSF-2.0 |
| pip | 26.2.1 | MIT |

Die Pakete werden unverändert weitergegeben; ihre Lizenztexte liegen nach der Einrichtung im jeweiligen
`*.dist-info`-Ordner der Laufzeit (`%LOCALAPPDATA%\FotoArchiv\…\site-packages`).

## Im Browser (nicht mitgeliefert, wird bei Bedarf geladen)

Karte: Leaflet (BSD-2-Clause) und Leaflet.markercluster (MIT) über unpkg.com;
Kartenbilder © OpenStreetMap-Mitwirkende (ODbL).
