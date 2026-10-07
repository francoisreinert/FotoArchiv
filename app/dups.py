"""Duplikate prüfen: Gruppen nebeneinander zeigen, die bessere Datei behalten, den Rest in den Papierkorb.

Eine Gruppe = ein behaltenes Foto (dup_of IS NULL) plus alle Einträge mit dup_of = dessen id.
Duplikate haben laut mark_duplicates() immer dieselbe Auflösung; Unterschiede sind Dateigröße
(Kompression), Format, Metadaten und der Ordner. Vor dem Löschen übernimmt die behaltene Datei
Alben, Personen, Favorit, Bewertung, Stichwörter usw. der gelöschten Kopie.
"""
import threading

import trash

# Original-/verlustfreie Formate vor nachträglich erzeugten JPEG/MP4
FMT_RANK = {"tif": 3, "tiff": 3, "png": 3, "psd": 3, "heic": 2, "heif": 2, "dng": 2, "cr2": 2, "cr3": 2,
            "nef": 2, "arw": 2, "mov": 2}
GOOD_DATE = ("exif", "xmp", "mylio", "manual", "video")

_cache = {}
JOB = {"running": False, "stop": False, "done": 0, "total": 0, "count": 0, "errors": [], "finished": None}


def top(folder):
    return folder.split("/", 1)[0]


def groups(con):
    """[(keeper_id, [ids...], taken, kind, private, {Ordner oben})] neueste zuerst (zwischengespeichert)."""
    key = con.execute("SELECT COUNT(*), MAX(id), TOTAL(dup_of) FROM items WHERE dup_of IS NOT NULL "
                      "AND COALESCE(hidden,0) != 2").fetchone()
    if _cache.get("key") == key:
        return _cache["groups"]
    rows = con.execute(
        "SELECT id, dup_of, taken, kind, COALESCE(priv_eff,0), folder FROM items WHERE COALESCE(hidden,0) != 2 "
        "AND (dup_of IS NOT NULL OR id IN (SELECT dup_of FROM items WHERE dup_of IS NOT NULL))").fetchall()
    info = {r[0]: r for r in rows}
    members = {}
    for iid, dup_of, *_ in rows:
        if dup_of is not None and dup_of in info:
            members.setdefault(dup_of, [dup_of]).append(iid)
    out = []
    for kid, ids in members.items():
        k = info[kid]
        out.append((kid, ids, k[2] or "", k[3], any(info[i][4] for i in ids), {top(info[i][5]) for i in ids}))
    out.sort(key=lambda g: g[2], reverse=True)
    _cache.update(key=key, groups=out)
    return out


def details(con, ids):
    ph = ",".join("?" * len(ids))
    cols = ("id", "folder", "name", "ext", "kind", "size", "width", "height", "duration", "taken", "taken_src",
            "camera", "lat", "fav", "rating", "usertags", "priv_eff", "hidden")
    out = {r[0]: dict(zip(cols, r)) for r in con.execute(
        "SELECT %s FROM items WHERE id IN (%s)" % (",".join(cols), ph), ids)}
    albums = dict(con.execute("SELECT item_id, COUNT(*) FROM album_items WHERE item_id IN (%s) GROUP BY item_id" % ph, ids))
    persons = dict(con.execute("SELECT item_id, COUNT(DISTINCT person_id) FROM faces WHERE person_id IS NOT NULL "
                               "AND item_id IN (%s) GROUP BY item_id" % ph, ids))
    raws = {r[0] for r in con.execute("SELECT raw_of FROM items WHERE raw_of IN (%s) AND COALESCE(hidden,0) != 2" % ph, ids)}
    for i, d in out.items():
        d["albums"] = albums.get(i, 0)
        d["persons"] = persons.get(i, 0)
        d["has_raw"] = i in raws
    return out


def quality(m, prefer=None):
    """Sortierschlüssel: größer = besser."""
    meta = (m["taken_src"] in GOOD_DATE) + (m["lat"] is not None) + bool(m["camera"])
    org = m["albums"] + m["persons"] + (m["fav"] or 0) + (m["rating"] or 0) + bool(m["usertags"])
    return ((m["width"] or 0) * (m["height"] or 0), m["has_raw"], FMT_RANK.get(m["ext"], 1), m["size"] or 0,
            bool(prefer) and top(m["folder"]) == prefer, meta, org, -len(m["name"]), -m["id"])


def reason(best, other, prefer=None):
    """Kurzer Grund, warum best besser ist als other."""
    if (best["width"] or 0) * (best["height"] or 0) > (other["width"] or 0) * (other["height"] or 0):
        return "höhere Auflösung"
    if best["has_raw"] and not other["has_raw"]:
        return "mit RAW-Datei daneben"
    if FMT_RANK.get(best["ext"], 1) > FMT_RANK.get(other["ext"], 1):
        return "Originalformat (%s statt %s)" % (best["ext"].upper(), other["ext"].upper())
    if (best["size"] or 0) > (other["size"] or 0):
        return "weniger komprimiert (größere Datei)"
    if prefer and top(best["folder"]) == prefer and top(other["folder"]) != prefer:
        return "identisch – liegt im bevorzugten Ordner „%s“" % prefer
    q1, q2 = quality(best), quality(other)
    if q1[5] > q2[5]:
        return "mehr Angaben (Datum/GPS/Kamera)"
    if q1[6] > q2[6]:
        return "identisch – in Alben/mit Personen"
    if q1[7] > q2[7]:
        return "identisch – ursprünglicher Dateiname"
    return "identisch – älterer Eintrag"


def suggest(members, prefer=None):
    ranked = sorted(members, key=lambda m: quality(m, prefer), reverse=True)
    return ranked[0], ranked[1:]


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0


def merge_into(con, keep, rem):
    """Katalogangaben der Kopie rem auf keep übertragen (nichts geht verloren)."""
    con.execute("INSERT OR IGNORE INTO album_items(album_id, item_id) SELECT album_id, ? FROM album_items WHERE item_id=?",
                (keep, rem))
    cols = ("fav", "rating", "usertags", "caption", "keywords", "private", "usertaken", "lat", "lon", "place", "userrot")
    k = dict(zip(cols, con.execute("SELECT %s FROM items WHERE id=?" % ",".join(cols), (keep,)).fetchone()))
    r = dict(zip(cols, con.execute("SELECT %s FROM items WHERE id=?" % ",".join(cols), (rem,)).fetchone()))
    upd = {"fav": max(k["fav"] or 0, r["fav"] or 0), "rating": max(k["rating"] or 0, r["rating"] or 0),
           "private": max(k["private"] or 0, r["private"] or 0)}
    tags = [t for t in (k["usertags"] or "").split(", ") if t]
    for t in (r["usertags"] or "").split(", "):
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    upd["usertags"] = ", ".join(tags) or None
    for c in ("caption", "keywords"):
        if not k[c] and r[c]:
            upd[c] = r[c]
    if k["lat"] is None and r["lat"] is not None:
        upd.update(lat=r["lat"], lon=r["lon"], place=r["place"])
    if not k["usertaken"] and r["usertaken"]:
        upd.update(usertaken=r["usertaken"], taken=r["usertaken"], taken_src="manual")
    if not k["userrot"] and r["userrot"]:
        upd["userrot"] = r["userrot"]
    con.execute("UPDATE items SET %s WHERE id=?" % ",".join("%s=?" % c for c in upd), list(upd.values()) + [keep])
    # Personen: Namen auf den passenden Gesichtsrahmen übertragen (gleiches Bild = gleiche Lage)
    have = {p for (p,) in con.execute("SELECT person_id FROM faces WHERE item_id=? AND person_id IS NOT NULL "
                                      "AND source IN ('mylio','manual')", (keep,))}
    kfaces = con.execute("SELECT id, x, y, w, h, person_id, source FROM faces WHERE item_id=? AND x IS NOT NULL",
                         (keep,)).fetchall()
    for pid, src, x, y, w, h in con.execute(
            "SELECT person_id, source, x, y, w, h FROM faces WHERE item_id=? AND person_id IS NOT NULL "
            "AND source IN ('mylio','manual')", (rem,)).fetchall():
        if pid in have:
            continue
        best = None
        if x is not None:
            cands = [(_iou((x, y, w, h), f[1:5]), f) for f in kfaces if f[6] not in ("mylio", "manual")]
            cands = [c for c in cands if c[0] > 0.4]
            best = max(cands, key=lambda c: c[0])[1] if cands else None
        if best:
            con.execute("UPDATE faces SET person_id=?, source=?, sugg_person=NULL, sugg_score=NULL, cluster=NULL "
                        "WHERE id=?", (pid, src, best[0]))
            kfaces = [f for f in kfaces if f[0] != best[0]]
        else:
            con.execute("INSERT INTO faces(item_id, person_id, source) VALUES(?, ?, 'manual')", (keep, pid))
        have.add(pid)


def resolve(con, keep, remove):
    """keep behalten, remove (Kopien derselben Gruppe) in den Papierkorb. Gibt (Anzahl, Fehler) zurück."""
    ids = [keep] + list(remove)
    rows = dict(con.execute("SELECT id, dup_of FROM items WHERE COALESCE(hidden,0) != 2 AND id IN (%s)"
                            % ",".join("?" * len(ids)), ids).fetchall())
    if keep not in rows or any(r not in rows for r in remove):
        return 0, ["Gruppe hat sich geändert – bitte neu laden"]
    root = rows[keep] or keep
    if any((rows[r] or r) != root for r in remove):
        return 0, ["Dateien gehören nicht zur selben Gruppe"]
    for r in remove:
        merge_into(con, keep, r)
    # Restliche Gruppenmitglieder zeigen jetzt auf das behaltene Foto
    con.execute("UPDATE items SET dup_of=? WHERE (dup_of=? OR id=?) AND id != ? AND COALESCE(hidden,0) != 2",
                (keep, root, root, keep))
    con.execute("UPDATE items SET dup_of=NULL WHERE id=?", (keep,))
    con.commit()
    n, errors = trash.move_to_trash(con, list(remove))
    if errors:  # was nicht verschoben werden konnte, bleibt ein Duplikat des behaltenen Fotos
        con.execute("UPDATE items SET dup_of=? WHERE id IN (%s) AND COALESCE(hidden,0) != 2"
                    % ",".join("?" * len(remove)), [keep] + list(remove))
        con.commit()
    return n, errors


def keep_all(con, ids):
    """„Kein Duplikat“: alle behalten; das nächste Einlesen fasst sie nicht wieder zusammen."""
    con.executemany("INSERT OR IGNORE INTO dup_keep(item_id) VALUES(?)", [(i,) for i in ids])
    con.execute("UPDATE items SET dup_of=NULL WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
    con.commit()


def run_all(con_factory, prefer, kind, folder, skip_private, after):
    """Alle Vorschläge übernehmen (im Hintergrund, mit Fortschritt in JOB)."""
    def work():
        con = con_factory()
        try:
            todo = [g for g in groups(con) if (not kind or g[3] == kind or (kind == "photo" and g[3] == "raw"))
                    and (not folder or folder in g[5]) and not (skip_private and g[4])]
            JOB.update(total=len(todo))
            for g in todo:
                if JOB["stop"]:
                    break
                members = list(details(con, g[1]).values())
                if len(members) > 1:
                    best, rest = suggest(members, prefer)
                    n, errors = resolve(con, best["id"], [m["id"] for m in rest])
                    JOB["count"] += n
                    JOB["errors"] += errors[:3]
                JOB["done"] += 1
        except Exception as ex:  # noqa: BLE001 – im Fortschritt anzeigen statt still abbrechen
            JOB["errors"].append(str(ex))
        finally:
            JOB["running"] = False
            JOB["finished"] = True
            after(con)

    if JOB["running"]:
        return False
    JOB.update(running=True, stop=False, done=0, total=0, count=0, errors=[], finished=None)
    threading.Thread(target=work, daemon=True).start()
    return True
