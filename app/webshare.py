"""Album als Webseite auf S3 teilen und Ordner für die Amazon-Photos-App füllen.

Webseite: FotoArchiv-Web/<Zufallsname>/ im Sicherungs-Bucket mit index.html (alles in einer Datei, ohne
fremde Skripte), t/<id>.jpg (Vorschau), l/<id>.jpg (groß, Bearbeitung angewendet) und v/<id>.mp4 (Videos, die
Browser abspielen). Private und ausgeblendete Fotos kommen nie hinein.
Zwei Arten:
- "link": alles bleibt privat; index.html und jedes Bild bekommen einen signierten Link (max. 7 Tage, S3-Grenze).
  "Link erneuern" schreibt nur index.html neu.
- "public": dauerhafter Link; braucht eine Bucket-Richtlinie mit Lesezugriff für FotoArchiv-Web/* (wird nur gesetzt,
  wenn der Bucket noch keine hat – sonst Text zum Ergänzen anzeigen).
Amazon Photos hat keine offene Schnittstelle mehr: die Amazon-Photos-App sichert Ordner – FotoArchiv füllt einen
Ordner (Standard: <Bibliothek>/Amazon Photos, vom Einlesen ausgeschlossen) mit Album/Favoriten, nur Neues.
"""
import datetime
import html
import json
import os
import secrets
import shutil
import time

import backup
import common
import share
from common import connect, to_abs

WEB_DIR = "FotoArchiv-Web/"
LINK_SECONDS = 7 * 24 * 3600
VIDEO_WEB = {"mp4", "m4v", "mov"}
SCHEMA = """CREATE TABLE IF NOT EXISTS web_shares (id INTEGER PRIMARY KEY, title TEXT, album_id INTEGER, target TEXT,
    slug TEXT, mode TEXT, created TEXT, expires TEXT, url TEXT, count INTEGER, manifest TEXT);"""
JOB = share.JOB  # gleiche Fortschrittskarte wie der Export


def _con():
    con = connect()
    con.executescript(SCHEMA)
    return con


def album_items(con, album_id):
    import server

    ids = server.album_tree_ids(int(album_id))
    return con.execute(
        "SELECT DISTINCT i.id, i.path, i.kind, i.name, i.taken, i.userrot, i.ext, i.size FROM items i "
        "JOIN album_items ai ON ai.item_id = i.id WHERE ai.album_id IN (%s) AND COALESCE(i.hidden,0) IN (0,3) "
        "AND COALESCE(i.priv_eff,0) = 0 AND i.dup_of IS NULL ORDER BY i.taken, i.name" % ",".join(map(str, ids))
    ).fetchall()


COLS = "i.id, i.path, i.kind, i.name, i.taken, i.userrot, i.ext, i.size"
VISIBLE = "COALESCE(i.hidden,0) IN (0,3) AND COALESCE(i.priv_eff,0) = 0 AND i.dup_of IS NULL"


def source_items(con, src):
    """(Titel, Zeilen) für {"album": id} | {"event": id} | {"ids": [...], "title": "…"} – nie private/ausgeblendete."""
    if src.get("album"):
        title = (con.execute("SELECT name FROM albums WHERE id=?", (int(src["album"]),)).fetchone() or ["Album"])[0]
        return title, album_items(con, src["album"])
    if src.get("event"):
        ev = con.execute("SELECT name, start, end FROM events WHERE id=?", (int(src["event"]),)).fetchone()
        if not ev:
            raise ValueError("Ereignis nicht gefunden")
        return ev[0], con.execute("SELECT %s FROM items i WHERE i.taken >= ? AND i.taken <= ? AND i.raw_of IS NULL AND %s "
                                  "ORDER BY i.taken, i.name" % (COLS, VISIBLE),
                                  (ev[1][:10], ev[2][:10] + " 23:59:59")).fetchall()
    ids = [int(i) for i in src.get("ids") or []]
    rows = {}
    for k in range(0, len(ids), 500):
        ch = ids[k:k + 500]
        for r in con.execute("SELECT %s FROM items i WHERE i.id IN (%s) AND %s" % (COLS, ",".join("?" * len(ch)), VISIBLE), ch):
            rows[r[0]] = r
    return (src.get("title") or "%d Fotos" % len(rows)), sorted(rows.values(), key=lambda r: ((r[4] or ""), r[3]))


def _thumb(iid, path, kind, userrot):
    b = common.THUMBS.get(iid)
    return b if b else share._as_jpeg(to_abs(path), kind, 480, userrot)


def _upload_bytes(c, key, data, ctype):
    c.put(key, data, {"Content-Type": ctype, "Cache-Control": "max-age=86400"})


def _upload_path(c, key, path, ctype):
    size = os.path.getsize(path)
    if size <= backup.SINGLE_MAX:
        with open(path, "rb") as f:
            return _upload_bytes(c, key, f.read(), ctype)
    ps = c.part_size(size)
    uid = c.mp_create(key, {"Content-Type": ctype})
    parts = []
    try:
        with open(path, "rb") as f:
            n = 1
            while True:
                b = f.read(ps)
                if not b:
                    break
                if JOB.stop:
                    raise InterruptedError
                parts.append((n, c.mp_part(key, uid, n, b)))
                JOB.bytes += len(b)
                n += 1
        c.mp_complete(key, uid, parts)
    except BaseException:
        c.mp_abort(key, uid)
        raise


def make_share(src, tid, mode, with_videos=True):
    """src: {"album": id} | {"event": id} | {"ids": [...], "title": "…"}"""
    con = _con()
    try:
        title, rows = source_items(con, src)
        album_id = src.get("album")
        JOB.reset("Webseite: " + title)
        JOB.running = True
        t = backup.target(tid)
        c = backup.client(t)
        if not with_videos:
            rows = [r for r in rows if r[2] != "video"]
        JOB.total = len(rows)
        slug = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
        base = backup.prefix_of(t) + WEB_DIR + slug + "/"
        manifest = []
        for iid, path, kind, name, taken, userrot, ext, size in rows:
            if JOB.stop:
                break
            JOB.done += 1
            try:
                ent = {"id": iid, "d": (taken or "")[:16], "n": name}
                if kind == "video":
                    if ext not in VIDEO_WEB:
                        JOB.existing += 1  # Browser spielt das Format nicht – weggelassen
                        continue
                    _upload_path(c, base + "v/%d.%s" % (iid, "mp4" if ext != "mov" else "mov"), to_abs(path),
                                 "video/mp4" if ext != "mov" else "video/quicktime")
                    ent["v"] = "v/%d.%s" % (iid, "mp4" if ext != "mov" else "mov")
                else:
                    big = share._as_jpeg(to_abs(path), kind, 2048, userrot)
                    _upload_bytes(c, base + "l/%d.jpg" % iid, big, "image/jpeg")
                    ent["l"] = "l/%d.jpg" % iid
                    JOB.bytes += len(big)
                tb = _thumb(iid, path, kind, userrot)
                _upload_bytes(c, base + "t/%d.jpg" % iid, tb, "image/jpeg")
                ent["t"] = "t/%d.jpg" % iid
                manifest.append(ent)
                JOB.new += 1
            except InterruptedError:
                break
            except Exception as ex:  # noqa: BLE001 – einzelnes Foto, weiter
                JOB.errors += 1
                JOB.note("Fehler bei %s: %s" % (name, backup.explain(ex) if hasattr(ex, "code") else ex))
        if JOB.stop:
            JOB.message = "Abgebrochen – hochgeladene Teile bleiben unter %s (in der Liste „Entfernen“)." % base
        now = datetime.datetime.now()
        cur = con.execute("INSERT INTO web_shares(title, album_id, target, slug, mode, created, count, manifest) "
                          "VALUES (?,?,?,?,?,?,?,?)", (title, album_id, tid, slug, mode,
                                                       now.isoformat(timespec="seconds"), len(manifest),
                                                       json.dumps(manifest)))
        con.commit()
        sid = cur.lastrowid
        if not JOB.stop:
            JOB.phase = "Seite schreiben"
            url = publish_index(con, sid)
            JOB.result = url
            JOB.message = "Webseite mit %d Fotos/Videos fertig%s." % (
                len(manifest), (" (%d Videos in nicht abspielbarem Format weggelassen)" % JOB.existing)
                if JOB.existing else "")
        JOB.note(JOB.message)
    except Exception as ex:  # noqa: BLE001
        JOB.errors += 1
        JOB.message = "Fehler: %s" % (backup.explain(ex) if hasattr(ex, "code") else ex)
    finally:
        con.close()
        JOB.running = False
        JOB.finished = time.time()
        JOB.phase = ""


def publish_index(con, sid):
    """index.html (neu) schreiben; im Link-Modus mit frisch signierten Adressen. Gibt den Link zurück."""
    title, tid, slug, mode, created, manifest = con.execute(
        "SELECT title, target, slug, mode, created, manifest FROM web_shares WHERE id=?", (sid,)).fetchone()
    t = backup.target(tid)
    c = backup.client(t)
    base = backup.prefix_of(t) + WEB_DIR + slug + "/"
    items = json.loads(manifest)
    if mode == "link":
        def u(rel):
            return c.presign(base + rel, LINK_SECONDS)
        items = [{k: (u(v) if k in ("t", "l", "v") else v) for k, v in e.items()} for e in items]
    page = render_html(title, items)
    _upload_bytes(c, base + "index.html", page.encode("utf-8"), "text/html; charset=utf-8")
    if mode == "link":
        url = c.presign(base + "index.html", LINK_SECONDS)
        expires = (datetime.datetime.now() + datetime.timedelta(seconds=LINK_SECONDS)).isoformat(timespec="minutes")
    else:
        url, expires = c.public_url(base + "index.html"), None
    con.execute("UPDATE web_shares SET url=?, expires=? WHERE id=?", (url, expires, sid))
    con.commit()
    return url


def list_shares():
    con = _con()
    try:
        return [dict(zip(("id", "title", "album_id", "target", "mode", "created", "expires", "url", "count"), r))
                for r in con.execute("SELECT id, title, album_id, target, mode, created, expires, url, count "
                                     "FROM web_shares ORDER BY id DESC")]
    finally:
        con.close()


def renew(sid):
    con = _con()
    try:
        return publish_index(con, int(sid))
    finally:
        con.close()


def remove(sid):
    """Webseite im Bucket löschen (nur unter FotoArchiv-Web/<Name>/) und aus der Liste nehmen."""
    con = _con()
    try:
        row = con.execute("SELECT target, slug FROM web_shares WHERE id=?", (int(sid),)).fetchone()
        if not row:
            return 0
        n = 0
        try:
            t = backup.target(row[0])
            c = backup.client(t)
            base = backup.prefix_of(t) + WEB_DIR + row[1] + "/"
            assert row[1] and "/" not in row[1]
            for x in list(c.list(base)):
                if x[0] != "prefix" and x[0].startswith(base):
                    c.delete(x[0])
                    n += 1
        except KeyError:
            pass  # Ziel gibt es nicht mehr
        con.execute("DELETE FROM web_shares WHERE id=?", (int(sid),))
        con.commit()
        return n
    finally:
        con.close()


def policy_text(t):
    res = "arn:aws:s3:::%s/%s%s*" % (t["bucket"], backup.prefix_of(t), WEB_DIR)
    return json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "FotoArchivWebLesen", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
        "Resource": res}]}, indent=2)


def public_setup(tid, apply=False):
    """Öffentlichen Lesezugriff nur für FotoArchiv-Web/* prüfen und – falls der Bucket noch keine Richtlinie
    hat und apply=True – einrichten. Eine bestehende Richtlinie wird nie überschrieben."""
    t = backup.target(tid)
    c = backup.client(t)
    pol = policy_text(t)
    existing = c.get_policy()
    if apply and existing is None:
        c.put_policy(pol)
        existing = pol
    return {"policy": pol, "existing": existing, "has_ours": bool(existing and WEB_DIR in existing)}


def render_html(title, items):
    data = json.dumps({"title": title, "items": items}, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("{{TITLE}}", html.escape(title)).replace("{{DATA}}", data)


_TEMPLATE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{{TITLE}}</title>
<style>
:root{--bg:#111315;--fg:#e8e6e3;--mut:#9aa0a6;--ac:#e0a040}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:22px 16px 8px;max-width:1400px;margin:auto}h1{margin:0;font-size:24px}header p{margin:4px 0 0;color:var(--mut)}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:4px;padding:12px 16px 40px;max-width:1400px;margin:auto}
.g a{position:relative;aspect-ratio:1;display:block;background:#1d2023;border-radius:3px;overflow:hidden}
.g img{width:100%;height:100%;object-fit:cover;display:block}.g a:hover img{filter:brightness(1.12)}
.g .v{position:absolute;right:6px;bottom:4px;font-size:12px;background:rgba(0,0,0,.6);border-radius:4px;padding:0 5px;color:#fff}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.95);display:none;align-items:center;justify-content:center;z-index:9}
#lb.on{display:flex}#lb img,#lb video{max-width:100vw;max-height:100vh;object-fit:contain}
#lb button{position:absolute;background:rgba(0,0,0,.5);color:#fff;border:0;font-size:28px;width:52px;height:52px;border-radius:26px;cursor:pointer}
#lb .x{top:12px;right:12px}#lb .p{left:12px;top:50%}#lb .n{right:12px;top:50%}
#lb .i{position:absolute;left:0;right:0;bottom:0;padding:10px 16px;color:var(--mut);font-size:13px;display:flex;gap:16px;justify-content:center}
#lb .i a{color:var(--ac)}footer{color:var(--mut);font-size:12px;text-align:center;padding:0 0 30px}
</style></head><body>
<header><h1>{{TITLE}}</h1><p id="sub"></p></header><div class="g" id="g"></div>
<div id="lb"><button class="x" title="Schließen (Esc)">×</button><button class="p" title="Zurück (←)">‹</button>
<button class="n" title="Weiter (→)">›</button><div id="m"></div><div class="i"><span id="cap"></span><a id="dl" download>Herunterladen</a></div></div>
<footer>Geteilt mit FotoArchiv</footer>
<script>
const D={{DATA}};const it=D.items;let cur=-1;
const fmt=d=>{if(!d)return"";const t=new Date(d.replace(" ","T"));return isNaN(t)?d:t.toLocaleDateString("de-DE",{day:"numeric",month:"long",year:"numeric"})};
const dates=it.map(x=>x.d).filter(Boolean).sort();
document.getElementById("sub").textContent=it.length+" Fotos und Videos"+(dates.length?" · "+fmt(dates[0])+(fmt(dates[0])!==fmt(dates[dates.length-1])?" – "+fmt(dates[dates.length-1]):""):"");
const g=document.getElementById("g");
it.forEach((x,i)=>{const a=document.createElement("a");a.href="#";a.innerHTML='<img loading="lazy" alt="" src="'+x.t+'">'+(x.v?'<span class="v">▶</span>':"");a.onclick=e=>{e.preventDefault();show(i)};g.appendChild(a)});
const lb=document.getElementById("lb"),m=document.getElementById("m");
function show(i){if(i<0||i>=it.length)return;cur=i;const x=it[i];m.innerHTML=x.v?'<video controls autoplay playsinline src="'+x.v+'"></video>':'<img alt="" src="'+x.l+'">';
document.getElementById("cap").textContent=fmt(x.d)+" · "+(i+1)+"/"+it.length;const dl=document.getElementById("dl");dl.href=x.v||x.l;dl.setAttribute("download",x.n||"");lb.classList.add("on");
if(!x.v&&it[i+1]&&it[i+1].l){new Image().src=it[i+1].l}}
function hide(){lb.classList.remove("on");m.innerHTML="";cur=-1}
lb.querySelector(".x").onclick=hide;lb.querySelector(".p").onclick=()=>show(cur-1);lb.querySelector(".n").onclick=()=>show(cur+1);
document.addEventListener("keydown",e=>{if(cur<0)return;if(e.key==="Escape")hide();if(e.key==="ArrowLeft")show(cur-1);if(e.key==="ArrowRight")show(cur+1)});
let sx=null;lb.addEventListener("touchstart",e=>{sx=e.touches[0].clientX});lb.addEventListener("touchend",e=>{if(sx==null)return;const dx=e.changedTouches[0].clientX-sx;if(Math.abs(dx)>50)show(cur+(dx<0?1:-1));sx=null});
</script></body></html>
"""


# ---------------------------------------------------------------- Amazon Photos ----

AMAZON_DIR = "Amazon Photos"


def amazon_dest():
    return os.path.join(common.LIB_ROOT, AMAZON_DIR)


def amazon_overview():
    """Oberste Ordner mit Zahl der Fotos/Videos – zum Auswählen in der Amazon-Photos-App."""
    con = connect()
    try:
        out = {}
        for folder, kind, n, size in con.execute(
                "SELECT folder, kind, COUNT(*), COALESCE(SUM(size),0) FROM items WHERE COALESCE(hidden,0) != 2 "
                "AND dup_of IS NULL GROUP BY folder, kind"):
            top = (folder or "").split("/")[0] or "(oberste Ebene)"
            d = out.setdefault(top, {"folder": top, "photos": 0, "videos": 0, "photo_bytes": 0, "video_bytes": 0})
            if kind == "video":
                d["videos"] += n
                d["video_bytes"] += size
            else:
                d["photos"] += n
                d["photo_bytes"] += size
        rows = sorted(out.values(), key=lambda d: -d["photos"])
        for d in rows:
            d["path"] = to_abs(d["folder"]) if not d["folder"].startswith("(") else common.LIB_ROOT
        return {"folders": rows, "dest": amazon_dest()}
    finally:
        con.close()


def amazon_sync(album_id=None, fav=False, videos=False, dest=None):
    """Album oder Favoriten in den Amazon-Ordner kopieren (Unterordner je Auswahl); nur Neues/Geändertes.
    Bearbeitete Fotos als JPEG mit Bearbeitung, sonst das Original."""
    con = connect()
    try:
        if album_id:
            name = (con.execute("SELECT name FROM albums WHERE id=?", (int(album_id),)).fetchone() or ["Album"])[0]
            rows = [r[:6] for r in album_items(con, album_id)]
        else:
            name = "Favoriten"
            rows = con.execute("SELECT id, path, kind, name, taken, userrot FROM items WHERE (fav=1 OR rating>=4) "
                               "AND COALESCE(hidden,0) IN (0,3) AND COALESCE(priv_eff,0)=0 AND dup_of IS NULL "
                               "AND raw_of IS NULL ORDER BY taken").fetchall()
        if not videos:
            rows = [r for r in rows if r[2] != "video"]
        JOB.reset("Amazon Photos: " + name)
        JOB.running = True
        JOB.total = len(rows)
        target = os.path.join(dest or amazon_dest(), share._safe(name))
        os.makedirs(target, exist_ok=True)
        used = set()
        for iid, path, kind, fname, taken, userrot in rows:
            if JOB.stop:
                break
            JOB.done += 1
            src = to_abs(path)
            try:
                edit = common.edit_for(path)
                stem, ext = os.path.splitext(fname)
                prefix = (taken or "")[:10]
                out_name = (prefix + " " + stem if prefix and not stem.startswith(prefix) else stem) + (
                    ".jpg" if (edit or userrot) and kind != "video" else ext)
                k, cand = 2, out_name
                while cand.lower() in used:
                    cand = "%s_%d%s" % (os.path.splitext(out_name)[0], k, os.path.splitext(out_name)[1])
                    k += 1
                used.add(cand.lower())
                out = os.path.join(target, cand)
                if (edit or userrot) and kind != "video":
                    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
                        JOB.existing += 1
                        continue
                    data = share._as_jpeg(src, kind, None, userrot)
                    with open(out, "wb") as f:
                        f.write(data)
                    JOB.bytes += len(data)
                else:
                    if os.path.exists(out) and os.path.getsize(out) == os.path.getsize(src):
                        JOB.existing += 1
                        continue
                    shutil.copy2(src, out)
                    JOB.bytes += os.path.getsize(src)
                JOB.new += 1
            except Exception as ex:  # noqa: BLE001
                JOB.errors += 1
                JOB.note("Fehler bei %s: %s" % (fname, ex))
        JOB.result = target
        JOB.message = "%d neu nach „%s“ kopiert, %d waren schon da – die Amazon-Photos-App lädt sie hoch." % (
            JOB.new, target, JOB.existing)
        JOB.note(JOB.message)
    except Exception as ex:  # noqa: BLE001
        JOB.errors += 1
        JOB.message = "Fehler: %s" % ex
    finally:
        con.close()
        JOB.running = False
        JOB.finished = time.time()
