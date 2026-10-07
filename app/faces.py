"""Personenerkennung: Vorschläge aus bekannten Gesichtern und Gruppierung unbekannter Gesichter.

Bekannte Gesichter = aus Mylio übernommen ('mylio') oder von Hand bestätigt ('manual').
Nur diese dienen als Vorbild. Sehr sichere Treffer werden automatisch zugeordnet ('auto'),
unsichere landen als Vorschlag zur Bestätigung bei der Person.
"""
import numpy as np

AUTO_THRESHOLD = 0.55     # ab hier automatisch zuordnen
AUTO_MARGIN = 0.06        # Abstand zur zweitbesten Person
SUGGEST_THRESHOLD = 0.42  # ab hier als Vorschlag anzeigen
CLUSTER_THRESHOLD = 0.50  # Ähnlichkeit für Gruppen unbekannter Gesichter
MIN_PX_AUTO = 40
MIN_PX_CLUSTER = 48
TOP_K = 3

LABELED = ("mylio", "manual")


def _load(con):
    rows = con.execute(
        "SELECT f.id, f.person_id, f.source, f.rejected, f.px, f.score, f.emb, i.dup_of IS NOT NULL "
        "FROM faces f JOIN items i ON i.id = f.item_id WHERE f.emb IS NOT NULL AND COALESCE(i.hidden,0) != 2").fetchall()
    if not rows:
        return None
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    pid = np.array([r[1] if r[1] is not None and r[2] in LABELED else -1 for r in rows], dtype=np.int64)
    src = [r[2] for r in rows]
    rej = [set(int(x) for x in r[3].split(",") if x) if r[3] else None for r in rows]
    px = np.array([r[4] or 0 for r in rows], dtype=np.float32)
    score = np.array([r[5] or 0 for r in rows], dtype=np.float32)
    emb = np.frombuffer(b"".join(r[6] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    dup = np.array([bool(r[7]) for r in rows])
    return ids, pid, src, rej, px, score, emb, dup


def person_scores(emb, lab_emb, lab_pid, persons):
    """Für jede Zeile in emb: Ähnlichkeit zu jeder Person (Mittel der TOP_K besten Vorbilder)."""
    out = np.full((len(emb), len(persons)), -1.0, dtype=np.float32)
    cols = [np.where(lab_pid == p)[0] for p in persons]
    for start in range(0, len(emb), 2048):
        sims = emb[start:start + 2048] @ lab_emb.T
        for j, c in enumerate(cols):
            if len(c) == 0:
                continue
            s = sims[:, c]
            k = min(TOP_K, s.shape[1])
            top = np.partition(s, s.shape[1] - k, axis=1)[:, -k:] if s.shape[1] > k else s
            out[start:start + 2048, j] = top.mean(axis=1)
    return out


def recompute(con, progress=None):
    if progress:
        progress.set_phase("Personen erkennen")
    con.execute("UPDATE faces SET person_id=NULL, source='det' WHERE source='auto'")
    con.execute("UPDATE faces SET sugg_person=NULL, sugg_score=NULL, cluster=NULL")
    data = _load(con)
    if data is None:
        con.commit()
        return
    ids, pid, src, rej, px, score, emb, dup = data
    hidden = {r[0] for r in con.execute("SELECT id FROM persons WHERE hidden=1")}
    lab = (pid >= 0) & ~np.isin(pid, list(hidden) or [-2])
    persons = sorted(set(pid[lab].tolist()))
    free = np.array([pid[i] < 0 and src[i] not in ("ignored", "manual") for i in range(len(ids))])
    auto, sugg = [], []
    assigned = np.zeros(len(ids), dtype=bool)
    if persons and free.any():
        idx = np.where(free)[0]
        sc = person_scores(emb[idx], emb[lab], pid[lab], persons)
        for row, i in enumerate(idx):
            s = sc[row]
            if rej[i]:
                for j, p in enumerate(persons):
                    if p in rej[i]:
                        s[j] = -1
            order = np.argsort(-s)
            best = order[0]
            second = s[order[1]] if len(order) > 1 else -1
            if s[best] >= AUTO_THRESHOLD and s[best] - second >= AUTO_MARGIN and px[i] >= MIN_PX_AUTO:
                auto.append((persons[best], float(s[best]), int(ids[i])))
                assigned[i] = True
            elif s[best] >= SUGGEST_THRESHOLD:
                sugg.append((persons[best], float(s[best]), int(ids[i])))
                assigned[i] = True
    con.executemany("UPDATE faces SET person_id=?, source='auto', sugg_score=? WHERE id=?", auto)
    con.executemany("UPDATE faces SET sugg_person=?, sugg_score=? WHERE id=?", sugg)
    con.commit()

    # Unbekannte Gesichter gruppieren
    if progress:
        progress.set_phase("Unbekannte Gesichter gruppieren")
    cand = np.where(free & ~assigned & ~dup & (px >= MIN_PX_CLUSTER) & (score >= 0.88))[0]
    cand = cand[np.argsort(-(score[cand] * np.minimum(px[cand], 200)))]
    clusters = cluster(emb[cand])
    upd = [(int(c), int(ids[cand[k]])) for k, c in enumerate(clusters) if c >= 0]
    con.executemany("UPDATE faces SET cluster=? WHERE id=?", upd)
    _covers(con)
    con.commit()
    return {"auto": len(auto), "suggestions": len(sugg), "clustered": len(upd)}


def cluster(emb, min_size=3):
    """Einfache, schnelle Leader-Gruppierung. Gibt Gruppennummer je Zeile (-1 = keine)."""
    n = len(emb)
    labels = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return labels
    cap = 1024
    cent = np.zeros((cap, emb.shape[1]), dtype=np.float32)
    sums = np.zeros_like(cent)
    count = np.zeros(cap, dtype=np.int64)
    nc = 0
    for start in range(0, n, 512):
        batch = emb[start:start + 512]
        if nc:
            sims = batch @ cent[:nc].T
            best = sims.argmax(axis=1)
            ok = sims[np.arange(len(batch)), best] >= CLUSTER_THRESHOLD
        else:
            best = np.zeros(len(batch), dtype=np.int64)
            ok = np.zeros(len(batch), dtype=bool)
        for k in range(len(batch)):
            i = start + k
            if ok[k]:
                c = best[k]
            else:
                # innerhalb des Stapels neu entstandene Gruppen prüfen
                c = -1
                if nc:
                    s = cent[:nc] @ batch[k]
                    j = int(s.argmax())
                    if s[j] >= CLUSTER_THRESHOLD:
                        c = j
                if c < 0:
                    if nc == cap:
                        cap *= 2
                        cent = np.resize(cent, (cap, emb.shape[1]))
                        sums = np.resize(sums, (cap, emb.shape[1]))
                        count = np.resize(count, cap)
                    c = nc
                    nc += 1
                    sums[c] = 0
                    count[c] = 0
            labels[i] = c
            sums[c] += batch[k]
            count[c] += 1
            cent[c] = sums[c] / max(1e-6, np.linalg.norm(sums[c]))
    sizes = np.bincount(labels, minlength=nc)
    keep = sizes >= min_size
    # Gruppen nach Größe neu nummerieren (1 = größte)
    order = np.argsort(-sizes)
    remap = np.full(nc, -1, dtype=np.int64)
    num = 1
    for c in order:
        if keep[c]:
            remap[c] = num
            num += 1
    return remap[labels]


def _covers(con):
    for (pid,) in con.execute("SELECT id FROM persons WHERE cover_face IS NULL OR cover_face NOT IN "
                              "(SELECT id FROM faces WHERE person_id = persons.id)").fetchall():
        row = con.execute("SELECT id FROM faces WHERE person_id=? AND x IS NOT NULL AND source IN ('mylio','manual') "
                          "ORDER BY (score IS NOT NULL) DESC, MIN(px, 250) * COALESCE(score, 0.5) DESC LIMIT 1",
                          (pid,)).fetchone()
        con.execute("UPDATE persons SET cover_face=? WHERE id=?", (row[0] if row else None, pid))


def calibrate(con):
    """Prüft die Schwellen an den Mylio-Markierungen (jede 5. als Test). Nur zur Kontrolle."""
    data = _load(con)
    ids, pid, src, rej, px, score, emb, dup = data
    lab = np.where(pid >= 0)[0]
    test = lab[::5]
    train = np.setdiff1d(lab, test)
    persons = sorted(set(pid[train].tolist()))
    sc = person_scores(emb[test], emb[train], pid[train], persons)
    best = sc.argmax(axis=1)
    bs = sc[np.arange(len(test)), best]
    srt = np.sort(sc, axis=1)
    margin = srt[:, -1] - srt[:, -2] if sc.shape[1] > 1 else bs
    truth = pid[test]
    pred = np.array(persons)[best]
    for t in (0.35, 0.40, 0.42, 0.45, 0.50, 0.55, 0.60):
        m = bs >= t
        ma = m & (margin >= AUTO_MARGIN) & (px[test] >= MIN_PX_AUTO)
        print("Schwelle %.2f: Abdeckung %.1f%%, richtig %.2f%% | auto: Abdeckung %.1f%%, richtig %.2f%%" % (
            t, 100 * m.mean(), 100 * (pred[m] == truth[m]).mean() if m.any() else 0,
            100 * ma.mean(), 100 * (pred[ma] == truth[ma]).mean() if ma.any() else 0))
