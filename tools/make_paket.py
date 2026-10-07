"""Baut eine leere, weitergebbare FotoArchiv-Version (ohne Katalog, Fotos, Anmeldungen, Einstellungen).

  python tools/make_paket.py [Zielordner]      (Standard: Downloads des Benutzers)
  python tools/make_paket.py --git <Ordner>    Inhalt fürs GitHub-Repository (ohne runtime/, .git bleibt)

Ergebnis: <Ziel>/FotoArchiv/ und <Ziel>/FotoArchiv.zip (Ausführrechte für den Mac bleiben im ZIP erhalten).
"""
import io
import os
import shutil
import stat
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAKET = os.path.join(BASE, "tools", "paket")
# Werkzeuge, die nur für die eigene Platte gedacht sind, bleiben draußen
SKIP_TOOLS = {"transcode_list.py", "transcode_apply.py", "dups_identisch.py", "paket", "__pycache__"}
EXEC = ("FotoArchiv starten (Mac).command", "FotoArchiv.app/Contents/MacOS/FotoArchiv")

CMD_SHORTCUT = r"""@echo off
rem Legt FotoArchiv mit Icon auf den Desktop und ins Startmenue.
setlocal
set "FA_HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$h=$env:FA_HERE; $ws=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))){ $l=$ws.CreateShortcut((Join-Path $d 'FotoArchiv.lnk')); $l.TargetPath=(Join-Path $h 'FotoArchiv starten (Windows).cmd'); $l.WorkingDirectory=$h; $l.IconLocation=(Join-Path $h 'FotoArchiv.ico'); $l.WindowStyle=7; $l.Description='FotoArchiv - Fotos verwalten'; $l.Save() }" || goto fail
echo.
echo FotoArchiv liegt jetzt auf dem Desktop und im Startmenue.
echo Wichtig: Diesen Ordner danach nicht mehr verschieben (sonst die Verknuepfung neu anlegen).
pause
exit /b
:fail
echo Verknuepfung konnte nicht angelegt werden.
pause
"""

MAC_RUN = """#!/bin/bash
# Startet FotoArchiv im Terminal (dort sieht man beim ersten Start die Einrichtung).
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
open -a Terminal "$HERE/FotoArchiv starten (Mac).command"
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>FotoArchiv</string>
  <key>CFBundleDisplayName</key><string>FotoArchiv</string>
  <key>CFBundleIdentifier</key><string>local.fotoarchiv.starter</string>
  <key>CFBundleExecutable</key><string>FotoArchiv</string>
  <key>CFBundleIconFile</key><string>FotoArchiv</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict></plist>
"""

START_NEU = """STARTEN
  Den Ordner "FotoArchiv" an einen festen Platz legen – z. B. "Dokumente" oder auf die externe
  Festplatte mit den Fotos (dann funktioniert alles auch an anderen Computern).

  Windows:  Doppelklick auf "Verknüpfung anlegen (Windows).cmd" – FotoArchiv erscheint mit Icon
            auf dem Desktop und im Startmenü. Oder direkt "FotoArchiv starten (Windows).cmd".
            Fragt Windows nach ("Der Computer wurde durch Windows geschützt"):
            "Weitere Informationen" > "Trotzdem ausführen".
  Mac:      Doppelklick auf "FotoArchiv.app" (oder "FotoArchiv starten (Mac).command").
            Beim allerersten Mal Rechtsklick > "Öffnen", weil die App nicht von Apple signiert ist.

  Beim ersten Start fragt FotoArchiv nach dem Fotoordner (z. B. "Bilder" oder die externe Platte).
  Danach "Bibliothek aktualisieren" – das erste Einlesen dauert je nach Menge einige Stunden
  (Vorschaubilder, Orte, Gesichter); man kann FotoArchiv währenddessen schon benutzen.

  Die Oberfläche öffnet sich im Browser (http://127.0.0.1:8765). Das schwarze
  Fenster muss offen bleiben, solange du FotoArchiv nutzt. Zum Beenden das Fenster
  schließen oder unter "Bibliothek" auf "FotoArchiv beenden" klicken.

  Beim ersten Start auf einem neuen Computer wird einmalig (ca. 1 Minute) die
  Laufzeitumgebung in den Benutzerordner entpackt:
    Windows: %LOCALAPPDATA%\\FotoArchiv      Mac: ~/Library/Application Support/FotoArchiv
  Es wird nichts installiert, keine Admin-Rechte nötig, kein Internet nötig.
  Alles Eigene (Katalog, Vorschaubilder, Personen, Alben) liegt im Ordner FotoArchiv\\data.

"""

MYLIO_NEU = """MYLIO ÜBERNEHMEN (nur wer bisher Mylio benutzt hat)
  Personen, Gesichter, Ereignisse, Alben, Bewertungen und Beschreibungen lassen sich aus Mylio
  übernehmen, solange Mylio noch auf dem Computer ist: tools\\mylio_export.py mit der
  FotoArchiv-Python-Umgebung ausführen (findet den Mylio-Katalog selbst), danach
  "Bibliothek aktualisieren". Mylio wird dabei nur gelesen.
"""


def liesmich():
    s = io.open(os.path.join(BASE, "LIESMICH.txt"), encoding="utf-8").read()
    s = s.replace("\r\n", "\n")
    head, rest = s.split("STARTEN\n", 1)
    rest = rest[rest.index("\nFUNKTIONEN\n") + 1:]
    s = ("FotoArchiv – Fotoverwaltung im Browser, ohne Cloud und ohne Abo\n"
         "===============================================================\n\n" + START_NEU + rest)
    a = s.index("MYLIO-DATEN\n")
    b = s.index("\n\n", a)
    s = s[:a] + MYLIO_NEU.rstrip("\n") + s[b:]
    for old, new in [
        ("z. B. dieselbe\n  Datei in Bilder und in iCloud FotosNina.", "z. B. dieselbe\n  Datei in zwei verschiedenen Ordnern."),
        ('  - Diesen Ordner "FotoArchiv" nicht umbenennen oder verschieben; er muss neben\n'
         '    "Bilder" usw. im Hauptordner der Festplatte liegen.\n',
         '  - Den Ordner "FotoArchiv" nach dem ersten Einlesen nicht mehr verschieben (Verknüpfungen\n'
         '    zeigen dorthin). Liegt er auf derselben Platte wie die Fotos, darf die Platte an jedem\n'
         '    Computer einen anderen Laufwerksbuchstaben haben.\n'),
        ("Ordner \"FotoArchiv-Papierkorb\" im Hauptordner der Festplatte", "Ordner \"FotoArchiv-Papierkorb\" im Fotoordner"),
        ("Neue Dateien landen in Bilder\\<Jahr>\\", "Neue Dateien landen im Fotoordner unter Bilder\\<Jahr>\\"),
        ("Jahre x Monate wie in Mylio, mit Ereignisnamen (z. B. \"Portugal 2018 (Peniche)\")",
         "Jahre x Monate, mit Ereignisnamen (z. B. \"Sommerurlaub 2024\")"),
        ("Alle Ereignisse und Alben aus Mylio. Ereignisse sind Zeiträume",
         "Eigene Alben (auch Unteralben) und Ereignisse. Ereignisse sind Zeiträume"),
        ("Die Ordnerstruktur der Festplatte", "Die Ordnerstruktur des Fotoordners"),
        ("  Personen     Alle Personen aus Mylio wurden übernommen. Neue Fotos werden automatisch erkannt.\n",
         "  Personen     Gesichter werden beim Einlesen in allen Fotos gefunden. So geht's los:\n"
         "               - \"Unbekannte Gesichter benennen\": ähnliche Gesichter sind schon gruppiert –\n"
         "                 einmal den Namen eintippen, danach \"Erkennung aktualisieren\"\n"
         "               - FotoArchiv erkennt die Person dann in allen anderen Fotos selbst\n"),
        ("Stern im Betrachter (Taste F); Mylio-Bewertungen ab 4 Sternen zählen auch",
         "Stern im Betrachter (Taste F); Bewertungen ab 4 Sternen (z. B. aus Lightroom) zählen auch"),
    ]:
        assert old in s, old[:40]
        s = s.replace(old, new)
    return s.replace("\n", "\r\n")


def build(target, zip_it=True):
    out = os.path.join(target, "FotoArchiv")
    if os.path.exists(out):
        shutil.rmtree(out)
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(os.path.join(BASE, "app"), os.path.join(out, "app"), ignore=ign)
    shutil.copytree(os.path.join(BASE, "models"), os.path.join(out, "models"))
    shutil.copytree(os.path.join(BASE, "runtime"), os.path.join(out, "runtime"))
    os.makedirs(os.path.join(out, "tools"))
    for f in os.listdir(os.path.join(BASE, "tools")):
        if f not in SKIP_TOOLS and f.endswith(".py"):
            shutil.copy2(os.path.join(BASE, "tools", f), os.path.join(out, "tools", f))
    os.makedirs(os.path.join(out, "Musik"))
    for f, nl in (("FotoArchiv starten (Windows).cmd", "\r\n"), ("FotoArchiv starten (Mac).command", "\n")):
        txt = io.open(os.path.join(BASE, f), encoding="utf-8", newline="").read().replace("\r\n", "\n")
        with open(os.path.join(out, f), "w", encoding="utf-8", newline="") as fh:
            fh.write(txt.replace("\n", nl))  # Windows-Starter brauchen CRLF, Mac LF
    shutil.copy2(os.path.join(PAKET, "FotoArchiv.ico"), out)
    with open(os.path.join(out, "Verknüpfung anlegen (Windows).cmd"), "w", encoding="cp850", newline="\r\n") as f:
        f.write(CMD_SHORTCUT)
    app = os.path.join(out, "FotoArchiv.app", "Contents")
    os.makedirs(os.path.join(app, "MacOS"))
    os.makedirs(os.path.join(app, "Resources"))
    with open(os.path.join(app, "Info.plist"), "w", encoding="utf-8", newline="\n") as f:
        f.write(PLIST)
    with open(os.path.join(app, "MacOS", "FotoArchiv"), "w", encoding="utf-8", newline="\n") as f:
        f.write(MAC_RUN)
    shutil.copy2(os.path.join(PAKET, "FotoArchiv.icns"), os.path.join(app, "Resources", "FotoArchiv.icns"))
    with open(os.path.join(out, "LIESMICH.txt"), "w", encoding="utf-8", newline="") as f:
        f.write(liesmich())
    for f in ("README.md", "DRITTANBIETER.md", "LICENSE"):
        shutil.copy2(os.path.join(PAKET, f), os.path.join(out, f))
    shutil.copytree(os.path.join(PAKET, "Lizenzen"), os.path.join(out, "Lizenzen"))
    shutil.copytree(os.path.join(PAKET, "screenshots"), os.path.join(out, "screenshots"))
    for p in EXEC:
        full = os.path.join(out, *p.split("/"))
        os.chmod(full, os.stat(full).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Keine persönlichen Daten: Prüfen statt hoffen
    assert not os.path.exists(os.path.join(out, "data")), "data/ darf nicht ins Paket"
    if not zip_it:
        return out, None
    zpath = os.path.join(target, "FotoArchiv.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d, _dirs, files in os.walk(out):
            for f in files:
                full = os.path.join(d, f)
                rel = os.path.relpath(full, target).replace(os.sep, "/")
                info = zipfile.ZipInfo.from_file(full, rel)
                mode = 0o755 if any(rel.endswith(e) for e in EXEC) else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.create_system = 3  # Unix, damit macOS die Rechte übernimmt
                info.compress_type = zipfile.ZIP_STORED if f.endswith((".gz", ".whl", ".onnx")) else zipfile.ZIP_DEFLATED
                with open(full, "rb") as src, z.open(info, "w") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    return out, zpath


GITIGNORE = """# Persönliches – gehört nie ins Repository
data/
Musik/*
!Musik/.gitkeep
# Laufzeit (zu groß für Git, liegt im Release-ZIP; neu bauen mit tools/build_runtime.py)
runtime/*
!runtime/VERSION
__pycache__/
*.pyc
.DS_Store
Thumbs.db
"""


def export_git(repo):
    """Repository-Inhalt erneuern: alles außer .git ersetzen, Laufzeit weglassen."""
    import tempfile

    os.makedirs(repo, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        out, _z = build(tmp, zip_it=False)
        for e in os.listdir(repo):
            if e != ".git":
                p = os.path.join(repo, e)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        for e in os.listdir(out):
            src = os.path.join(out, e)
            if e == "runtime":
                os.makedirs(os.path.join(repo, "runtime"))
                shutil.copy2(os.path.join(src, "VERSION"), os.path.join(repo, "runtime", "VERSION"))
            elif os.path.isdir(src):
                shutil.copytree(src, os.path.join(repo, e))
            else:
                shutil.copy2(src, os.path.join(repo, e))
    open(os.path.join(repo, "Musik", ".gitkeep"), "w").close()
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GITIGNORE)
    # .gitattributes: Windows-Starter brauchen CRLF, Mac-Skripte LF
    with open(os.path.join(repo, ".gitattributes"), "w", encoding="utf-8", newline="\n") as f:
        f.write("*.cmd text eol=crlf\nLIESMICH.txt text eol=crlf\n*.command text eol=lf\n"
                "FotoArchiv.app/Contents/MacOS/* text eol=lf\n*.py text eol=lf\n*.js text eol=lf\n")
    return repo


if __name__ == "__main__":
    if sys.argv[1:2] == ["--git"]:
        print("Repository-Inhalt:", export_git(sys.argv[2]))
        sys.exit()
    tgt = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), "Downloads")
    o, zp = build(tgt)
    print("Ordner:", o)
    print("ZIP:   ", zp, "(%.0f MB)" % (os.path.getsize(zp) / 1e6))
