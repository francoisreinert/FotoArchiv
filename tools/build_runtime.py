"""Baut die portablen Laufzeitumgebungen für FotoArchiv (einmalig, braucht Internet).

Legt pro Plattform ein Python-Archiv und einen Wheel-Ordner unter ../runtime ab,
außerdem die Gesichtsmodelle (../models) und die Ortsdatenbank (../models/cities.txt).
"""
import json, os, subprocess, sys, urllib.request, zipfile, io, shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT = os.path.join(BASE, "runtime")
MODELS = os.path.join(BASE, "models")
PYVER = "3.12"
PACKAGES = ["pillow", "pillow-heif", "numpy", "opencv-python-headless==4.10.0.84", "rawpy", "av"]
# icloudpd: immer das reine Python-Paket (das Windows-Paket enthält nur ein fertiges Programm ohne Bibliothek)
ICLOUDPD = "icloudpd==1.32.3"
# Samsung The Frame (Kunstmodus) und QR-Codes fürs Teilen aufs Handy
EXTRA = ["samsungtvws==3.0.6", "websocket-client==1.9.2", "yarl==1.25.1", "multidict==7.0.0", "propcache==0.5.4",
         "idna==3.20", "segno==1.6.6"]
TARGETS = {
    "win-x64": ("x86_64-pc-windows-msvc", ["win_amd64"]),
    "mac-arm64": ("aarch64-apple-darwin", ["macosx_12_0_arm64"]),
    "mac-x64": ("x86_64-apple-darwin", ["macosx_12_0_x86_64"]),
}
MODEL_URLS = {
    "face_detection_yunet_2023mar.onnx": "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "text_detection_ppocr.onnx": "https://github.com/opencv/opencv_zoo/raw/main/models/text_detection_ppocr/text_detection_en_ppocrv3_2023may.onnx",
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FotoArchiv-build"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def main():
    os.makedirs(RT, exist_ok=True)
    os.makedirs(MODELS, exist_ok=True)
    rel = json.loads(get("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"))
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    for key, (triple, platforms) in TARGETS.items():
        name = next(n for n in assets if n.startswith("cpython-" + PYVER + ".") and n.endswith(triple + "-install_only.tar.gz"))
        dest = os.path.join(RT, f"python-{key}.tar.gz")
        print("Python", key, name)
        with open(dest, "wb") as f:
            f.write(get(assets[name]))
        wheels = os.path.join(RT, f"wheels-{key}")
        shutil.rmtree(wheels, ignore_errors=True)
        cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--python-version", PYVER,
               "--implementation", "cp", "-d", wheels]
        for p in platforms:
            cmd += ["--platform", p]
        subprocess.check_call(cmd + PACKAGES)
        # Abhängigkeiten von icloudpd für diese Plattform, das Paket selbst plattformunabhängig
        # Für die Mac-Plattform liefert PyPI das reine Python-Paket, das überall läuft
        subprocess.check_call([sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
                               "--python-version", PYVER, "--platform", "macosx_12_0_arm64", "-d", wheels, ICLOUDPD])
        for w in os.listdir(wheels):
            if w.startswith("icloudpd-") and not w.endswith("-none-any.whl"):
                os.remove(os.path.join(wheels, w))
        deps = ["requests==2.32.3", "schema==0.7.7", "tqdm==4.67.1", "piexif==1.1.3", "urllib3==1.26.20",
                "typing_extensions==4.14.0", "Flask==3.1.1", "waitress==3.0.2", "tzlocal==5.3.1", "pytz==2025.2",
                "certifi==2025.4.26", "keyring==25.6.0", "keyrings-alt==5.0.2", "srp==1.0.22"]
        subprocess.check_call(cmd + deps)
        subprocess.check_call(cmd + ["--no-deps"] + EXTRA)
    with open(os.path.join(RT, "VERSION"), "w") as f:
        f.write(rel["tag_name"] + "-1\n")
    for fn, url in MODEL_URLS.items():
        print("Modell", fn)
        with open(os.path.join(MODELS, fn), "wb") as f:
            f.write(get(url))
    print("Orte (GeoNames cities1000)")
    z = zipfile.ZipFile(io.BytesIO(get("https://download.geonames.org/export/dump/cities1000.zip")))
    rows = []
    for line in z.read("cities1000.txt").decode("utf-8").splitlines():
        c = line.split("\t")
        # name, lat, lon, Land, Bundesland/Region
        rows.append("\t".join([c[1], c[4], c[5], c[8], c[10]]))
    with open(os.path.join(MODELS, "cities.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(rows))
    info = get("https://download.geonames.org/export/dump/countryInfo.txt").decode("utf-8")
    with open(os.path.join(MODELS, "countries.txt"), "w", encoding="utf-8") as f:
        for line in info.splitlines():
            if line and not line.startswith("#"):
                c = line.split("\t")
                f.write(c[0] + "\t" + c[4] + "\n")
    print("fertig")


if __name__ == "__main__":
    main()
