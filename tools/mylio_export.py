"""Liest den Mylio-Katalog (Mylo.mylodb) aus und speichert alles Wichtige in data/mylio.json.

Übernommen werden: Ereignisse (z. B. "Portugal 2018 (Peniche)"), Alben, benannte Gesichter,
Bewertungen, Markierungen, Titel/Beschreibungen und Stichwörter.
Der Mylio-Katalog wird nur gelesen (aus einer Kopie), nie verändert.

Aufruf:  python mylio_export.py [Pfad\\zu\\Mylo.mylodb]
Ohne Pfad wird der neueste Katalog im Benutzerordner gesucht (.Mylio_Catalog*).
"""
import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "mylio.json")


def find_catalog():
    home = os.path.expanduser("~")
    cands = glob.glob(os.path.join(home, ".Mylio_Catalog*", "Mylo.mylodb"))
    cands += glob.glob(os.path.join(home, "Library", "Application Support", "Mylio*", "Mylo.mylodb"))
    cands += glob.glob(os.path.join(home, ".Mylio_Catalog*", "*", "Mylo.mylodb"))
    if not cands:
        return None
    return max(cands, key=lambda p: os.path.getsize(p) + os.path.getmtime(p))


def decode_text(b):
    # Mylio speichert manche Namen in Windows-Kodierung statt UTF-8
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def ms_to_wall(ms):
    """Mylio speichert die Ortszeit der Aufnahme so, als wäre sie UTC – also ohne Zeitzonen-Umrechnung lesen."""
    if not ms or ms < 0:
        return None
    try:
        return (datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else find_catalog()
    if not src or not os.path.isfile(src):
        print("Kein Mylio-Katalog gefunden. Pfad zu Mylo.mylodb als Parameter angeben.")
        return 1
    print("Mylio-Katalog:", src)
    tmp = tempfile.mkdtemp(prefix="mylio_")
    try:
        # Kopie lesen, damit ein laufendes Mylio nicht gestört wird
        for ext in ("", "-wal"):
            if os.path.exists(src + ext):
                shutil.copy2(src + ext, os.path.join(tmp, "Mylo.mylodb" + ext))
        con = sqlite3.connect(os.path.join(tmp, "Mylo.mylodb"))
        con.text_factory = decode_text
        data = export(con)
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print("Gespeichert:", OUT)
    print("  %d Ereignisse, %d Alben, %d Fotos mit Daten, %d benannte Gesichter" % (
        len(data["events"]), len(data["albums"]), len(data["media"]),
        sum(len(m.get("faces", [])) for m in data["media"].values())))
    return 0


def export(con):
    hx = lambda b: b.hex().upper() if isinstance(b, (bytes, bytearray)) else (b or "")

    # Ordnerpfade relativ zur Festplatte: Wurzelordner "D:\Bilder" -> "Bilder"
    folders = {hx(h): (name, hx(parent), root) for h, name, parent, root in con.execute(
        "SELECT UniqueHash, Name, ParentFolderHash, LocalRootOrTemporaryPath FROM Folder")}
    paths = {}

    def folder_path(h, depth=0):
        if h in paths:
            return paths[h]
        if h not in folders or depth > 50:
            return None
        name, parent, root = folders[h]
        if root:
            # Nur Ordner, die direkt auf einem Laufwerk liegen (z. B. D:\Bilder), sind auf der Platte
            r = root.replace("\\", "/").rstrip("/")
            parts = r.split("/")
            p = parts[-1] if len(parts) == 2 and (parts[0].endswith(":") or parts[0] == "") else None
            if p is None and r.startswith("/Volumes/"):
                p = "/".join(parts[3:]) or None
        elif parent and parent in folders:
            pp = folder_path(parent, depth + 1)
            p = pp + "/" + name if pp else None
        else:
            p = None
        paths[h] = p
        return p

    persons = {}
    for h, first, last, nick in con.execute("SELECT UniqueHash, FirstName, LastName, Nickname FROM Party"):
        name = (nick or " ".join(x for x in (first, last) if x) or "").strip()
        if name:
            persons[hx(h)] = name

    media = {}
    key_of = {}
    for (h, folder_h, stem, orient, rating, flagged, caption, title, kw, dc, undated, rs, re_, lat, lon) in con.execute(
            "SELECT UniqueHash, ContainingFolderHash, FileNameNoExt, Orientation, StarRating, IsFlagged, Caption, "
            "Title, KeywordsStr, DateCreated, Undated, DateRangeStart, DateRangeEnd, GpsLat, GpsLong FROM Media"):
        fp = folder_path(hx(folder_h))
        if not fp or not stem:
            continue
        key = fp + "/" + stem.lower()
        key_of[hx(h)] = key
        m = {}
        if rating and rating > 0:
            m["r"] = rating
        if flagged:
            m["f"] = 1
        text = " ".join(x for x in (title, caption) if x and x.strip())
        if text:
            m["c"] = text.strip()
        if kw and kw.strip():
            m["k"] = kw.strip()
        # Datum: d = Kameradatum (zur Kontrolle bei gleichen Dateinamen), t/te = gültiges Datum bzw.
        # Zeitraum inkl. deiner Korrekturen in Mylio, u = in Mylio als undatiert geführt
        d, t, te = ms_to_wall(dc), ms_to_wall(rs), ms_to_wall(re_)
        if d:
            m["d"] = d
        if undated:
            m["u"] = 1
        elif t:
            m["t"] = t
            if te and rs and re_ and re_ - rs > 3_600_000:
                m["te"] = te
        if (lat or lon) and lat is not None and lon is not None:
            m["g"] = [round(lat, 6), round(lon, 6)]
        if m:
            media.setdefault(key, {}).update(m)

    for mh, ph, x, y, w, h, ignore in con.execute(
            "SELECT MediaHash, PersonHash, TopLeftX, TopLeftY, Width, Height, Ignore FROM FaceRectangle "
            "WHERE length(PersonHash) > 0"):
        key = key_of.get(hx(mh))
        name = persons.get(hx(ph))
        if not key or not name or ignore or w is None:
            continue
        media.setdefault(key, {}).setdefault("faces", []).append(
            [name, round(x, 5), round(y, 5), round(w, 5), round(h, 5)])

    events = []
    for name, start, end, parent in con.execute(
            "SELECT Name, StartDateTime, EndDateTime, ParentEventHash FROM Event ORDER BY StartDateTime"):
        s, e = ms_to_wall(start), ms_to_wall(end)
        if name and s:
            events.append({"name": name, "start": s, "end": e or s})

    albums = []
    album_ids = {}
    rows = con.execute("SELECT UniqueHash, Name, ParentAlbumHash, Description FROM Album").fetchall()
    for h, name, parent, desc in rows:
        album_ids[hx(h)] = len(albums)
        albums.append({"name": name, "parent": hx(parent), "description": desc or "", "items": []})
    for a in albums:
        a["parent"] = album_ids.get(a["parent"])
    for media_h, album_h in con.execute("SELECT SourceResourceHash, TargetResourceHash FROM MediaAlbumLink"):
        a = album_ids.get(hx(album_h))
        key = key_of.get(hx(media_h))
        if a is None or key is None:
            # Richtung der Verknüpfung ist bei Mylio nicht dokumentiert -> beide prüfen
            a = album_ids.get(hx(media_h))
            key = key_of.get(hx(album_h))
        if a is not None and key:
            albums[a]["items"].append(key)
    return {"exported": datetime.datetime.now().isoformat(timespec="seconds"), "events": events,
            "albums": albums, "media": media}


if __name__ == "__main__":
    sys.exit(main())
