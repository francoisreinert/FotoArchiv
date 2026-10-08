"""Belichtungsreihen (Bracketing) erkennen und als Stapel zusammenfassen.

Eine Belichtungsreihe = mindestens 3 Fotos derselben Kamera im selben Ordner, höchstens 2 s auseinander, die die
Kamera als Auto-Belichtungsreihe kennzeichnet (EXIF ExposureMode = 2) oder die mindestens 3 verschiedene
Belichtungskorrekturen haben (EXIF ExposureBiasValue, z. B. −2 / 0 / +2). Handys bleiben außen vor (sie bracketen
nicht sichtbar; Serienbilder haben gleiche Belichtung).

Gespeichert: items.stack = id des Titelbilds (Aufnahme mit Belichtung am nächsten an 0), items.stack_top = 1 beim
Titelbild; die übrigen Aufnahmen bekommen hidden = 3 ("im Stapel") – so fasst die Zeitleiste sie ohne Mehrkosten
zusammen (hidden steckt in den abdeckenden Indizes). Mitglieder einer Reihe gelten nie als Duplikate voneinander.
"""
import datetime
import re
import threading

import common
from common import connect

PHONES = re.compile(r"^(apple|iphone|ipad|htc|zte|samsung sm-|sm-|google|pixel|huawei|xiaomi|redmi|oneplus|motorola|"
                    r"moto |lge|lg-|nokia|oppo|vivo|realme|honor|asus_|fairphone)", re.I)
GAP = 2.0  # Sekunden zwischen zwei Aufnahmen einer Reihe
JOB = {"running": False, "done": 0, "total": 0, "phase": "", "message": "", "stacks": None}


def _t(s):
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def read_exposure(path):
    """(Belichtungskorrektur in EV oder None, ExposureMode oder -1) aus dem EXIF-Kopf – ohne das Bild zu dekodieren."""
    from PIL import Image

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        with Image.open(path) as im:
            ex = im.getexif().get_ifd(0x8769)
    except Exception:
        return None, -1
    bias = ex.get(0x9204)
    try:
        bias = None if bias is None else round(float(bias), 2)
        if bias != bias or abs(bias) > 20:  # 0/0 (NaN) oder Unsinn = keine Angabe
            bias = None
    except (TypeError, ValueError, ZeroDivisionError):
        bias = None
    mode = ex.get(0xA402)
    return bias, int(mode) if isinstance(mode, int) else -1


def _clusters(rows):
    """Aufeinanderfolgende Aufnahmen (gleiche Kamera + Ordner, <= GAP) mit mindestens 3 Fotos."""
    out, cur = [], []
    for r in rows:
        t = _t(r["taken"])
        if (cur and t and r["camera"] == cur[-1]["camera"] and r["folder"] == cur[-1]["folder"]
                and cur[-1]["_t"] and (t - cur[-1]["_t"]).total_seconds() <= GAP):
            cur.append(dict(r, _t=t))
        else:
            if len(cur) >= 3:
                out.append(cur)
            cur = [dict(r, _t=t)]
    if len(cur) >= 3:
        out.append(cur)
    return out


def _sets(members):
    """Reihen in einer Gruppe: Folgen mit Belichtungsangabe; beginnt das Muster neu (gleiche Korrektur wie die
    erste Aufnahme der laufenden Reihe), fängt eine neue Reihe an (z. B. 0/−2/+2, 0/−2/+2)."""
    sets, cur = [], []

    def close():
        if len(cur) >= 3:
            evs = {m["ev_bias"] for m in cur}
            auto = all(m["exp_mode"] == 2 for m in cur)
            if (auto and len(evs) >= 2) or len(evs) >= 3:
                sets.append(list(cur))

    for m in members:
        if m["exp_mode"] == -2 or (m["ev_bias"] is None and m["exp_mode"] != 2):  # -2 = Reihe von Hand aufgelöst
            close()
            cur = []
            continue
        if len(cur) >= 2 and m["ev_bias"] == cur[0]["ev_bias"]:
            close()
            cur = []
        cur.append(m)
    close()
    return sets


def detect(con, progress=None):
    """Alle Belichtungsreihen neu bestimmen. Liest EXIF nur für Kandidaten, die noch keine Angabe haben."""
    con.row_factory = None
    cols = ("id", "camera", "folder", "taken", "name", "path", "ev_bias", "exp_mode", "hidden", "dup_of")
    rows = [dict(zip(cols, r)) for r in con.execute(
        "SELECT id, camera, folder, taken, name, path, ev_bias, exp_mode, COALESCE(hidden,0), dup_of FROM items "
        "WHERE kind='photo' AND raw_of IS NULL AND COALESCE(hidden,0) != 2 AND camera IS NOT NULL AND taken IS NOT NULL "
        "ORDER BY camera, folder, taken, name")]
    rows = [r for r in rows if not PHONES.match(r["camera"])]
    groups = _clusters(rows)
    need = [m for g in groups for m in g if m["exp_mode"] is None]
    JOB.update(total=len(need), done=0, phase="Belichtung aus EXIF lesen")
    for k, m in enumerate(need, 1):
        m["ev_bias"], m["exp_mode"] = read_exposure(common.to_abs(m["path"]))
        con.execute("UPDATE items SET ev_bias=?, exp_mode=? WHERE id=?", (m["ev_bias"], m["exp_mode"], m["id"]))
        JOB["done"] = k
        if progress:
            progress(k, len(need))
        if k % 200 == 0:
            con.commit()
    con.commit()
    # Kopien (gleiche Sekunde und gleiche Belichtung, z. B. IMG_1.JPG + IMG_1-1.jpg) gehören nicht in die Reihe:
    # je Sekunde+Belichtung nur eine Aufnahme (bevorzugt die, die selbst kein Duplikat ist, dann der kürzere Name)
    for g in groups:
        best = {}
        for m in g:
            k = (m["taken"][:19], m["ev_bias"], m["exp_mode"])
            if k not in best or (m["dup_of"] is not None, len(m["name"])) < (best[k]["dup_of"] is not None, len(best[k]["name"])):
                best[k] = m
        keep = {m["id"] for m in best.values()}
        g[:] = [m for m in g if m["id"] in keep]
    JOB["phase"] = "Reihen bilden"
    chosen = {r[0] for r in con.execute("SELECT id FROM items WHERE stack_top = 1")}  # von Hand gewählte Titelbilder bleiben
    # alte Stapel auflösen (nur "im Stapel" ausgeblendete zurück; von Hand Ausgeblendetes bleibt ausgeblendet)
    con.execute("UPDATE items SET hidden = CASE WHEN hidden = 3 THEN 0 ELSE hidden END, stack = NULL, stack_top = NULL "
                "WHERE stack IS NOT NULL OR hidden = 3")
    n_sets = n_members = 0
    for g in groups:
        for s in _sets(g):
            # Titelbild: Belichtung am nächsten an 0, bei Gleichstand die mittlere Aufnahme
            mid = len(s) // 2
            top = min(range(len(s)), key=lambda i: (s[i]["id"] not in chosen, abs(s[i]["ev_bias"] or 0), abs(i - mid)))
            top_id = s[top]["id"]
            for i, m in enumerate(s):
                con.execute("UPDATE items SET stack=?, stack_top=?, hidden=CASE WHEN ? AND COALESCE(hidden,0)=0 THEN 3 "
                            "ELSE hidden END WHERE id=?", (top_id, 1 if i == top else 0, 0 if i == top else 1, m["id"]))
            n_sets += 1
            n_members += len(s)
    # Aufnahmen einer Reihe sind keine Duplikate voneinander
    con.execute("UPDATE items SET dup_of = NULL WHERE stack IS NOT NULL AND dup_of IN "
                "(SELECT j.id FROM items j WHERE j.stack = items.stack)")
    con.commit()
    return n_sets, n_members


def run_bg(after=None):
    if JOB["running"]:
        return False
    JOB.update(running=True, done=0, total=0, phase="", message="", stacks=None)

    def work():
        con = connect()
        try:
            n, m = detect(con)
            JOB["stacks"] = n
            JOB["message"] = "%d Belichtungsreihen mit %d Fotos gefunden" % (n, m)
        except Exception as ex:  # noqa: BLE001 – in der Oberfläche zeigen
            JOB["message"] = "Fehler: %s" % ex
        finally:
            con.close()
            JOB["running"] = False
            if after:
                after()
    threading.Thread(target=work, daemon=True).start()
    return True


def repair(con):
    """Nach Papierkorb/Ausblenden: Reihen ohne sichtbares Titelbild bekommen ein neues; bleibt nur ein Foto übrig,
    ist es keine Reihe mehr."""
    bad = [r[0] for r in con.execute(
        "SELECT DISTINCT s.stack FROM items s WHERE s.stack IS NOT NULL AND s.hidden = 3 AND NOT EXISTS "
        "(SELECT 1 FROM items t WHERE t.stack = s.stack AND t.stack_top = 1 AND COALESCE(t.hidden,0) = 0)")]
    for sid in bad:
        rows = con.execute("SELECT id, ev_bias FROM items WHERE stack=? AND hidden=3 ORDER BY taken, name",
                           (sid,)).fetchall()
        if len(rows) == 1:
            con.execute("UPDATE items SET hidden=0, stack=NULL, stack_top=NULL WHERE id=?", (rows[0][0],))
            continue
        mid = len(rows) // 2
        top = rows[min(range(len(rows)), key=lambda i: (abs(rows[i][1] or 0), abs(i - mid)))][0]
        con.execute("UPDATE items SET stack_top = (id = ?) WHERE stack = ?", (top, sid))
        con.execute("UPDATE items SET hidden = 0 WHERE id = ?", (top,))
    con.commit()
    return len(bad)


def set_top(con, iid):
    """Anderes Foto der Reihe als Titelbild zeigen."""
    row = con.execute("SELECT stack FROM items WHERE id=? AND stack IS NOT NULL AND COALESCE(hidden,0) IN (0,3)",
                      (iid,)).fetchone()
    if not row:
        return False
    con.execute("UPDATE items SET hidden = CASE WHEN id = ? THEN 0 WHEN hidden IN (0,3) THEN 3 ELSE hidden END, "
                "stack_top = (id = ?) WHERE stack = ?", (iid, iid, row[0]))
    con.commit()
    return True


def unstack(con, iid):
    """Reihe auflösen (alle Fotos einzeln zeigen); die Erkennung legt sie nicht wieder an."""
    row = con.execute("SELECT stack FROM items WHERE id=?", (iid,)).fetchone()
    if not row or row[0] is None:
        return 0
    ids = [r[0] for r in con.execute("SELECT id FROM items WHERE stack=?", (row[0],))]
    con.execute("UPDATE items SET hidden = CASE WHEN hidden = 3 THEN 0 ELSE hidden END, stack = NULL, stack_top = NULL, "
                "exp_mode = -2 WHERE stack = ?", (row[0],))
    con.commit()
    return len(ids)


def members(con, iid):
    row = con.execute("SELECT stack FROM items WHERE id=?", (iid,)).fetchone()
    if not row or row[0] is None:
        return []
    return [{"id": r[0], "ev": r[1], "top": bool(r[2]), "name": r[3]} for r in con.execute(
        "SELECT id, ev_bias, stack_top, name FROM items WHERE stack=? AND COALESCE(hidden,0) IN (0,3) "
        "ORDER BY taken, name", (row[0],))]


def sizes(con):
    """{Titelbild-id: Anzahl Fotos} aller Reihen (klein, über den Teilindex items_stack)."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT MAX(CASE WHEN stack_top = 1 THEN id END), COUNT(*) FROM items WHERE stack IS NOT NULL "
        "AND COALESCE(hidden,0) IN (0,3) GROUP BY stack") if r[0]}
