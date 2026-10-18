"""„Beiseite“: Fotos, die nicht in Zeitleiste, Fotos und Kalender gehören (items.hidden = 5), aber in Alben, Ordnern,
Personen und der Suche bleiben – mit eigener Zeitleiste und eigenem Kalender (#/beiseite).
Ordner-Regel: config "aside_folders" (Katalogpfade, gilt mit Unterordnern); apply() legt auch später Neues dort beiseite.
Belichtungsreihen wandern immer als Ganzes (hidden 3 = im Stapel)."""
import common

ASIDE = 5


def folders():
    return [f for f in (common.load_config().get("aside_folders") or []) if f]


def _under(paths):
    """SQL-Bedingung: Ordner gleich oder darunter ('/' + 1 = '0')."""
    conds, args = [], []
    for f in paths:
        conds.append("(folder = ? OR (folder >= ? AND folder < ?))")
        args += [f, f + "/", f + "0"]
    return "(" + " OR ".join(conds) + ")", args


def rule_for(path):
    """Ordner-Regel, die für diesen Ordner gilt (er selbst oder ein Elternordner), sonst None."""
    for f in folders():
        if path == f or path.startswith(f + "/"):
            return f
    return None


def _with_stacks(ids):
    q = ",".join("?" * len(ids))
    return "(id IN (%s) OR stack IN (SELECT stack FROM items WHERE id IN (%s) AND stack IS NOT NULL))" % (q, q), ids + ids


def put(con, ids):
    """Beiseitelegen (aus der Zeitleiste). Dokumente, Ausgeblendetes und Papierkorb bleiben, wie sie sind."""
    n = 0
    for k in range(0, len(ids), 400):
        cond, args = _with_stacks(list(ids[k:k + 400]))
        n += con.execute("UPDATE items SET hidden=%d WHERE COALESCE(hidden,0) IN (0,3) AND %s" % (ASIDE, cond), args).rowcount
    con.commit()
    return n


_BACK = "hidden = CASE WHEN stack IS NOT NULL AND COALESCE(stack_top,0) = 0 THEN 3 ELSE 0 END"


def back(con, ids):
    """Zurück in die Zeitleiste (Reihen wieder zugeklappt)."""
    n = 0
    for k in range(0, len(ids), 400):
        cond, args = _with_stacks(list(ids[k:k + 400]))
        n += con.execute("UPDATE items SET %s WHERE hidden=%d AND %s" % (_BACK, ASIDE, cond), args).rowcount
    con.commit()
    return n


def apply(con):
    """Ordner-Regeln anwenden (nach dem Einlesen/Import). Gibt die Zahl neu beiseitegelegter Fotos zurück."""
    fs = folders()
    if not fs:
        return 0
    cond, args = _under(fs)
    n = con.execute("UPDATE items SET hidden=%d WHERE COALESCE(hidden,0) IN (0,3) AND %s" % (ASIDE, cond), args).rowcount
    con.commit()
    return n


def set_folder(con, path, on):
    """Ordner-Regel setzen/aufheben. Aufheben holt die Fotos des Ordners zurück in die Zeitleiste
    (auch einzeln beiseitegelegte darin), außer sie fallen unter eine andere Regel."""
    if not path:
        raise ValueError("Die ganze Bibliothek kann nicht beiseitegelegt werden")
    cfg = common.load_config()
    fs = [f for f in (cfg.get("aside_folders") or []) if f and f != path]
    if on:
        fs = [f for f in fs if not f.startswith(path + "/")] + [path]  # Unterordner-Regeln gehen in der neuen auf
    cfg["aside_folders"] = sorted(fs)
    common.save_config(cfg)
    if on:
        return apply(con)
    cond, args = _under([path])
    keep = [f for f in fs if f.startswith(path + "/")]
    if keep:
        kc, ka = _under(keep)
        cond, args = cond + " AND NOT " + kc, args + ka
    n = con.execute("UPDATE items SET %s WHERE hidden=%d AND %s" % (_BACK, ASIDE, cond), args).rowcount
    con.commit()
    return n


def count(con):
    return con.execute("SELECT COUNT(*) FROM items WHERE hidden=%d AND dup_of IS NULL AND raw_of IS NULL" % ASIDE).fetchone()[0]
