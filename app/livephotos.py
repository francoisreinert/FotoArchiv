"""Live-Photo-Videos erkennen: die kurze Bewegung, die das iPhone zu einem Foto aufnimmt.

Apple schreibt in diese Videos die Kennung „com.apple.quicktime.content.identifier“ (dieselbe wie im Foto);
normale kurze Clips haben sie nicht. Ergebnis in items.live: 1 = Live Photo, 0 = normales Video, NULL = noch nicht geprüft.
Geprüft werden nur kurze MOV/MP4 (≤ MAX_DUR s), längere sind sofort 0.
"""
import threading

import common

MAX_DUR = 6.0
KEY = "com.apple.quicktime.content.identifier"
RUN = {"running": False, "done": 0, "total": 0}
_lock = threading.Lock()


def is_live(path):
    import av

    try:
        with av.open(common.to_abs(path)) as f:
            return 1 if f.metadata.get(KEY) else 0
    except Exception:
        return 0


def scan(con=None):
    """Alle noch ungeprüften Videos prüfen (fortsetzbar, ~0,05 s je kurzem Video)."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        con = con or common.connect()
        con.execute("UPDATE items SET live=0 WHERE kind='video' AND live IS NULL AND hidden IS NOT 2 "
                    "AND (COALESCE(duration, 99) > ? OR lower(ext) NOT IN ('mov','mp4'))", (MAX_DUR,))
        con.commit()
        todo = con.execute("SELECT id, path FROM items WHERE kind='video' AND live IS NULL AND hidden IS NOT 2").fetchall()
        RUN.update(running=True, done=0, total=len(todo))
        batch = []
        for iid, path in todo:
            batch.append((is_live(path), iid))
            RUN["done"] += 1
            if len(batch) >= 200:
                con.executemany("UPDATE items SET live=? WHERE id=?", batch)
                con.commit()
                batch = []
        if batch:
            con.executemany("UPDATE items SET live=? WHERE id=?", batch)
            con.commit()
        return True
    finally:
        RUN["running"] = False
        _lock.release()


def start():
    threading.Thread(target=scan, daemon=True).start()
