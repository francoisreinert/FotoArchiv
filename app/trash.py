"""Papierkorb: Fotos, Videos und Ordner löschen und wiederherstellen.

Gelöschte Dateien werden nur verschoben – nach <Platte>/FotoArchiv-Papierkorb/<gleicher Pfad>.
Das geht auf derselben Platte sofort und braucht keinen Zusatzplatz. Der Katalog-Eintrag bleibt
erhalten (hidden = 2), damit Alben, Personen, Datum usw. beim Wiederherstellen wieder da sind.
Erst „Endgültig löschen“ entfernt die Dateien und den Eintrag.
"""
import datetime
import json
import os

import common
from common import PREVIEWS, split_source, to_abs

TRASH_NAME = "FotoArchiv-Papierkorb"
TRASHED = 2  # Wert in items.hidden
SIDECAR_EXT = ("xmp", "aae")
# Kleinkram, der einen Ordner sonst am Verschwinden hindert
JUNK = {"thumbs.db", "desktop.ini", ".ds_store"}
JUNK_EXT = {"thm", "lrv"}


def trash_roots():
    """Ein Papierkorb je Fotoordner (Haupt-Fotoordner und weitere Quellen) – Löschen verschiebt nur auf derselben Platte."""
    return [os.path.join(common.LIB_ROOT, TRASH_NAME)] + [os.path.join(r, TRASH_NAME) for r in common.SOURCES.values()]


def trash_rel(rel):
    """Katalogpfad → Pfad im Papierkorb desselben Fotoordners ("@NAS/x.jpg" → "@NAS/FotoArchiv-Papierkorb/x.jpg")."""
    name, rest = split_source(rel)
    return (common.SOURCE_PREFIX + name + "/" if name is not None else "") + TRASH_NAME + "/" + rest


def in_trash(rel):
    return split_source(rel)[1].startswith(TRASH_NAME + "/")


def _chunks(ids, n=500):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def with_copies(con, ids):
    """Ausgewählte Fotos plus ihre ausgeblendeten Duplikate und RAW-Partner (gelten als dasselbe Foto)."""
    out, todo = set(ids), list(ids)
    while todo:
        ch, todo = todo[:500], todo[500:]
        ph = ",".join("?" * len(ch))
        new = [r[0] for r in con.execute("SELECT id FROM items WHERE (dup_of IN (%s) OR raw_of IN (%s)) "
                                         "AND COALESCE(hidden,0) != 2" % (ph, ph), ch + ch) if r[0] not in out]
        out.update(new)
        todo += new
    return sorted(out)


def _free_target(rel):
    """Pfad im Papierkorb; liegt dort schon etwas gleichen Namens, wird hochgezählt."""
    base, dot, ext = rel.rpartition(".")
    if not dot:
        base, ext = rel, ""
    cand, n = rel, 2
    while os.path.exists(to_abs(cand)):
        cand = "%s_%d%s" % (base, n, "." + ext if dot else "")
        n += 1
    return cand


def _sidecars(con, path, keep_ids):
    """Begleitdateien (XMP/AAE) eines Fotos. IMG_1.xmp gehört allen IMG_1.*-Dateien im Ordner und
    wandert nur mit, wenn keine davon im Katalog bleibt."""
    full = to_abs(path)
    d, name = os.path.split(full)
    stem = name.rsplit(".", 1)[0].lower()
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    try:
        files = os.listdir(d)
    except OSError:
        return []
    others = [r for r in con.execute("SELECT id, name FROM items WHERE folder=? AND COALESCE(hidden,0) != 2", (folder,))
              if r[0] not in keep_ids and r[1].rsplit(".", 1)[0].lower() == stem]
    out = []
    for f in files:
        low = f.lower()
        if any(low == name.lower() + "." + e for e in SIDECAR_EXT):
            out.append(f)
        elif not others and any(low == stem + "." + e for e in SIDECAR_EXT):
            out.append(f)
    return [(folder + "/" if folder else "") + common.norm(f) for f in out]


def _move(src_rel, dst_rel):
    src, dst = to_abs(src_rel), to_abs(dst_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)


def move_to_trash(con, ids):
    """Fotos/Videos in den Papierkorb verschieben. Gibt (Anzahl, Fehlerliste) zurück."""
    ids = list(ids)
    idset = set(ids)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    done, errors = 0, []
    for ch in _chunks(ids):
        rows = con.execute("SELECT id, path FROM items WHERE COALESCE(hidden,0) != 2 AND id IN (%s)"
                           % ",".join("?" * len(ch)), ch).fetchall()
        for iid, path in rows:
            side = []
            try:
                dst = _free_target(trash_rel(path))
                if os.path.exists(to_abs(path)):
                    _move(path, dst)
                for s in _sidecars(con, path, idset):
                    sd = _free_target(trash_rel(s))
                    try:
                        _move(s, sd)
                        side.append([sd, s])
                    except OSError:
                        pass
            except OSError as ex:
                errors.append("%s: %s" % (path, ex.strerror or ex))
                continue
            con.execute("UPDATE items SET path=?, hidden=2, trashed=?, trash_from=?, trash_side=?, dup_of=NULL, "
                        "raw_of=NULL WHERE id=?", (dst, now, path, json.dumps(side) if side else None, iid))
            done += 1
        con.commit()
    return done, errors


def restore(con, ids):
    """Aus dem Papierkorb an den alten Platz zurücklegen."""
    done, errors = 0, []
    for ch in _chunks(list(ids)):
        rows = con.execute("SELECT id, path, trash_from, trash_side FROM items WHERE hidden=2 AND id IN (%s)"
                           % ",".join("?" * len(ch)), ch).fetchall()
        for iid, path, orig, side in rows:
            if os.path.exists(to_abs(orig)):
                errors.append("%s: dort liegt schon eine Datei" % orig)
                continue
            if con.execute("SELECT 1 FROM items WHERE path=? AND id != ?", (orig, iid)).fetchone():
                errors.append("%s: schon im Katalog" % orig)
                continue
            try:
                if os.path.exists(to_abs(path)):
                    _move(path, orig)
            except OSError as ex:
                errors.append("%s: %s" % (orig, ex.strerror or ex))
                continue
            for sd, s in json.loads(side or "[]"):
                try:
                    if not os.path.exists(to_abs(s)):
                        _move(sd, s)
                except OSError:
                    pass
            con.execute("UPDATE items SET path=?, hidden=0, trashed=NULL, trash_from=NULL, trash_side=NULL WHERE id=?",
                        (orig, iid))
            done += 1
        con.commit()
    for r in trash_roots():
        _cleanup_dirs(r)
    return done, errors


def purge(con, ids=None):
    """Endgültig löschen (ids=None: ganzen Papierkorb leeren)."""
    import indexer

    if ids is None:
        ids = [r[0] for r in con.execute("SELECT id FROM items WHERE hidden=2")]
    gone, errors = [], []
    for ch in _chunks(list(ids)):
        for iid, path, side in con.execute("SELECT id, path, trash_side FROM items WHERE hidden=2 AND id IN (%s)"
                                           % ",".join("?" * len(ch)), ch).fetchall():
            if not in_trash(path):  # Sicherheitsnetz: nie außerhalb eines Papierkorbs löschen
                continue
            try:
                if os.path.exists(to_abs(path)):
                    os.remove(to_abs(path))
            except OSError as ex:
                errors.append("%s: %s" % (path, ex.strerror or ex))
                continue
            for sd, _s in json.loads(side or "[]"):
                try:
                    os.remove(to_abs(sd))
                except OSError:
                    pass
            gone.append(iid)
    for ch in _chunks(gone):
        ph = ",".join("?" * len(ch))
        con.execute("DELETE FROM album_items WHERE item_id IN (%s)" % ph, ch)
        con.execute("DELETE FROM album_removed WHERE item_id IN (%s)" % ph, ch)
    indexer.remove_items(con, gone)
    PREVIEWS.delete(gone)
    if not con.execute("SELECT 1 FROM items WHERE hidden=2 LIMIT 1").fetchone():
        for r in trash_roots():
            _purge_leftovers(r)  # z. B. Begleitkram aus gelöschten Ordnern
    for r in trash_roots():
        _cleanup_dirs(r)
    return len(gone), errors


def trash_folder(con, folder):
    """Ordner samt Unterordnern löschen: alle Fotos/Videos darin (auch ausgeblendete, Duplikate, RAW)
    in den Papierkorb. Andere Dateien bleiben liegen und werden gemeldet."""
    like = folder.replace("%", "\\%").replace("_", "\\_") + "/%"
    ids = [r[0] for r in con.execute("SELECT id FROM items WHERE (folder=? OR folder LIKE ? ESCAPE '\\') "
                                     "AND COALESCE(hidden,0) != 2", (folder, like))]
    done, errors = move_to_trash(con, ids)
    root = to_abs(folder)
    others = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            low = f.lower()
            rel = common.to_rel(os.path.join(dirpath, f))
            if low in JUNK or low.startswith("._") or low.rsplit(".", 1)[-1] in JUNK_EXT:
                try:
                    _move(rel, _free_target(trash_rel(rel)))
                except OSError:
                    others.append(rel)
            else:
                others.append(rel)
    _cleanup_dirs(root, keep_root=False)
    return done, errors, others


def _cleanup_dirs(root, keep_root=True):
    """Leere Ordner entfernen (von unten nach oben)."""
    if not os.path.isdir(root):
        return
    for dirpath, dirs, files in os.walk(root, topdown=False):
        if dirpath == root and keep_root and os.path.basename(root) != TRASH_NAME:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def _purge_leftovers(root):
    if not os.path.isdir(root):
        return
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                os.remove(os.path.join(dirpath, f))
            except OSError:
                pass


def stats(con):
    n, size = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM items WHERE hidden=2").fetchone()
    return {"count": n, "size": size}
