"""Private Fotos, Alben und Ereignisse – Schutz in der Oberfläche (nicht auf Dateiebene).

Ein Foto ist effektiv privat (items.priv_eff), wenn es selbst, eines seiner Alben (auch übergeordnete)
oder ein Ereignis, in dessen Zeitraum es fällt, als privat markiert ist. Kopien (Duplikat/RAW) erben das.
Solange nicht entsperrt ist, liefert der Server für solche Fotos nur ein Schloss-Bild.
Das Passwort wird nur als gesalzener PBKDF2-Prüfwert im Katalog gespeichert.
"""
import hashlib
import hmac
import io
import json
import secrets
import threading
import time

TIMEOUT = 30 * 60      # automatisch wieder sperren nach 30 Minuten ohne Nutzung
ITERATIONS = 200_000
COOKIE = "fa_unlock"

_tokens = {}
_lock = threading.Lock()
_lock_img = None
VERSION = [0]


def _get(con):
    row = con.execute("SELECT value FROM meta WHERE key='private_pw'").fetchone()
    return json.loads(row[0]) if row else None


def has_password(con):
    return _get(con) is not None


def _hash(pw, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), iterations).hex()


def check(con, pw):
    d = _get(con)
    return bool(d and pw) and hmac.compare_digest(_hash(pw, d["salt"], d["iter"]), d["hash"])


def set_password(con, new, old=None):
    if has_password(con) and not check(con, old or ""):
        return False
    salt = secrets.token_hex(16)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('private_pw', ?)",
                (json.dumps({"salt": salt, "iter": ITERATIONS, "hash": _hash(new, salt, ITERATIONS)}),))
    con.commit()
    return True


def unlock(con, pw):
    if not check(con, pw):
        time.sleep(1)  # Durchprobieren bremsen
        return None
    token = secrets.token_urlsafe(24)
    with _lock:
        _tokens[token] = time.time()
    return token


def lock_all():
    with _lock:
        _tokens.clear()


def token_ok(token):
    if not token:
        return False
    with _lock:
        t = _tokens.get(token)
        if t is None or time.time() - t > TIMEOUT:
            _tokens.pop(token, None)
            return False
        _tokens[token] = time.time()
        return True


def is_locked(con, token):
    return has_password(con) and not token_ok(token)


# --------------------------------------------------- effektiv private Fotos ----

def private_albums(con):
    """Als privat markierte Alben samt aller Unteralben."""
    children = {}
    for aid, parent in con.execute("SELECT id, parent FROM albums"):
        children.setdefault(parent, []).append(aid)
    stack = [a for (a,) in con.execute("SELECT id FROM albums WHERE private=1")]
    out = set()
    while stack:
        a = stack.pop()
        if a not in out:
            out.add(a)
            stack += children.get(a, [])
    return out


def refresh(con):
    """items.priv_eff neu berechnen (nach jeder Änderung an Markierungen, Alben, Ereignissen, Daten)."""
    ids = {r[0] for r in con.execute("SELECT id FROM items WHERE private=1")}
    for aid in private_albums(con):
        ids |= {r[0] for r in con.execute("SELECT item_id FROM album_items WHERE album_id=?", (aid,))}
    for s, e in con.execute("SELECT start, end FROM events WHERE private=1").fetchall():
        ids |= {r[0] for r in con.execute("SELECT id FROM items WHERE taken >= ? AND taken <= ?",
                                          (s[:10], e[:10] + " 23:59:59"))}
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _priv (id INTEGER PRIMARY KEY)")
    con.execute("DELETE FROM _priv")
    con.executemany("INSERT OR IGNORE INTO _priv(id) VALUES(?)", [(i,) for i in ids])
    # Kopien (Duplikate, RAW zum JPEG) erben die Markierung – in beide Richtungen
    con.execute("INSERT OR IGNORE INTO _priv SELECT id FROM items WHERE dup_of IN (SELECT id FROM _priv) "
                "OR raw_of IN (SELECT id FROM _priv)")
    con.execute("INSERT OR IGNORE INTO _priv SELECT dup_of FROM items WHERE dup_of IS NOT NULL AND id IN (SELECT id FROM _priv)")
    con.execute("INSERT OR IGNORE INTO _priv SELECT raw_of FROM items WHERE raw_of IS NOT NULL AND id IN (SELECT id FROM _priv)")
    con.execute("UPDATE items SET priv_eff=1 WHERE id IN (SELECT id FROM _priv) AND COALESCE(priv_eff,0)=0")
    con.execute("UPDATE items SET priv_eff=0 WHERE priv_eff=1 AND id NOT IN (SELECT id FROM _priv)")
    con.commit()
    VERSION[0] += 1


def lock_image():
    """Neutrales Platzhalterbild mit Schloss."""
    global _lock_img
    if _lock_img is None:
        from PIL import Image, ImageDraw

        s = 360
        im = Image.new("RGB", (s, s), (38, 42, 47))
        d = ImageDraw.Draw(im)
        cx, cy = s // 2, s // 2 + 18
        d.rounded_rectangle((cx - 52, cy - 10, cx + 52, cy + 66), 12, fill=(120, 128, 138))
        d.arc((cx - 36, cy - 76, cx + 36, cy + 6), 180, 360, fill=(120, 128, 138), width=14)
        d.line((cx - 36, cy - 36, cx - 36, cy - 6), fill=(120, 128, 138), width=14)
        d.line((cx + 36, cy - 36, cx + 36, cy - 6), fill=(120, 128, 138), width=14)
        d.ellipse((cx - 9, cy + 12, cx + 9, cy + 30), fill=(38, 42, 47))
        d.rectangle((cx - 4, cy + 24, cx + 4, cy + 46), fill=(38, 42, 47))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        _lock_img = buf.getvalue()
    return _lock_img
