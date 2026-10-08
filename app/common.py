"""Gemeinsame Pfade, Konfiguration und Datenbankzugriff für FotoArchiv.

Alle Pfade zu Fotos werden relativ zur Wurzel der Festplatte gespeichert
(der Ordner, in dem der FotoArchiv-Ordner liegt). So funktioniert der
Katalog unter jedem Laufwerksbuchstaben und auch am Mac unter /Volumes/...
"""
import json
import os
import sqlite3
import threading
import unicodedata

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
MODELS_DIR = os.path.join(BASE_DIR, "models")
STATIC_DIR = os.path.join(APP_DIR, "static")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")


def _library_root():
    """Fotoordner: in data/config.json gewählt ("library_root", relativ zu FotoArchiv oder absolut),
    sonst der Ordner, in dem FotoArchiv liegt (z. B. die Wurzel einer externen Platte)."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            root = json.load(f).get("library_root")
    except (OSError, ValueError):
        root = None
    if root:
        return os.path.normpath(root if os.path.isabs(root) else os.path.join(BASE_DIR, root))
    return os.path.dirname(BASE_DIR)


def root_setting(path):
    """Wert für config.json: auf demselben Laufwerk relativ speichern, damit es portabel bleibt."""
    path = os.path.abspath(path)
    if path == os.path.dirname(BASE_DIR):
        return None
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:  # anderes Laufwerk (Windows)
        return path


LIB_ROOT = _library_root()

# Weitere Fotoordner (NAS, zweite Platte, lokaler Ordner): config.json "sources" = [{"name", "path"}].
# Ihre Fotos stehen im Katalog als "@Name/Unterordner/Datei" – nur hier wird der echte Ort eingesetzt.
# Ändert sich der Ort (anderer Laufwerksbuchstabe, Mac), reicht ein neuer Pfad; der Katalog bleibt gleich.
SOURCE_PREFIX = "@"
SOURCES = {}  # Name -> absoluter Pfad


def _resolve(p):
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(BASE_DIR, p))


def reload_sources():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lst = json.load(f).get("sources") or []
    except (OSError, ValueError):
        lst = []
    SOURCES.clear()
    for s in lst:
        if s.get("name") and s.get("path"):
            SOURCES[s["name"]] = _resolve(s["path"])


reload_sources()


def path_setting(path):
    """Pfad für config.json: auf demselben Laufwerk relativ zu FotoArchiv (portabel), sonst absolut (z. B. NAS)."""
    path = os.path.abspath(path)
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        return path
MYLIO_JSON = os.path.join(DATA_DIR, "mylio.json")
CATALOG_DB = os.path.join(DATA_DIR, "catalog.db")
THUMBS_DB = os.path.join(DATA_DIR, "thumbs.db")
PREVIEWS_DB = os.path.join(DATA_DIR, "previews.db")

PHOTO_EXT = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "tif", "tiff", "bmp", "psd"}
RAW_EXT = {"cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "srw", "pef"}
VIDEO_EXT = {"mov", "mp4", "m4v", "mts", "m2ts", "avi", "3gp", "mkv", "wmv", "mpg", "mpeg", "qt"}
BROWSER_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}

# Ordner auf oberster Ebene, die nie durchsucht werden.
ALWAYS_EXCLUDED = {
    os.path.basename(BASE_DIR).lower(), "$recycle.bin", "system volume information", ".spotlight-v100",
    ".fseventsd", ".temporaryitems", ".trashes", ".documentrevisions-v100", ".claude", "htdocs",
    "fotoarchiv-papierkorb",  # gelöschte Fotos (trash.py)
    "amazon photos",  # Kopien für die Amazon-Photos-App (webshare.py)
}

DEFAULT_CONFIG = {
    # None = automatisch: alle Ordner außer den unten ausgeschlossenen
    "folders": None,
    "excluded_folders": ["!!! Nicht Importieren", "Export", "Transcode", "LivePhotos"],
    "workers": None,
    "hide_duplicates": True,
    "hide_raw_with_jpeg": True,
}


def kind_for_ext(ext):
    if ext in PHOTO_EXT:
        return "photo"
    if ext in RAW_EXT:
        return "raw"
    if ext in VIDEO_EXT:
        return "video"
    return None


def norm(s):
    return unicodedata.normalize("NFC", s)


def to_rel(abspath):
    if SOURCES:
        a = os.path.normcase(abspath)
        for name, root in SOURCES.items():
            r = os.path.normcase(root)
            if a == r or a.startswith(r.rstrip(os.sep) + os.sep):
                rest = os.path.relpath(abspath, root).replace(os.sep, "/")
                return norm(SOURCE_PREFIX + name + ("" if rest == "." else "/" + rest))
    return norm(os.path.relpath(abspath, LIB_ROOT).replace(os.sep, "/"))


def split_source(rel):
    """("Name", "rest") für Pfade weiterer Fotoordner, sonst (None, rel)."""
    if rel.startswith(SOURCE_PREFIX):
        name, _, rest = rel[len(SOURCE_PREFIX):].partition("/")
        return name, rest
    return None, rel


def root_of(rel):
    """Basisordner, in dem rel liegt (Haupt-Fotoordner oder weitere Quelle)."""
    name, _ = split_source(rel)
    if name is None:
        return LIB_ROOT
    # unbekannte Quelle: ein Ort, der sicher nicht existiert (Fotos gelten als "nicht erreichbar", nicht als gelöscht)
    return SOURCES.get(name) or os.path.join(BASE_DIR, "_unbekannte_quelle_", name)


def to_abs(rel):
    name, rest = split_source(rel)
    base = root_of(rel)
    parts = [x for x in rest.split("/") if x] if name is not None else rel.split("/")
    p = os.path.join(base, *parts)
    if not os.path.exists(p):
        alt = os.path.join(base, *unicodedata.normalize("NFD", "/".join(parts)).split("/"))
        if os.path.exists(alt):
            return alt
    return p


def top_level_folders():
    out = []
    try:
        for e in os.scandir(LIB_ROOT):
            if not e.is_dir() or e.name.startswith(".") or e.name.lower() in ALWAYS_EXCLUDED:
                continue
            if e.name.upper().startswith("FOUND."):
                continue
            out.append(norm(e.name))
    except OSError:
        pass
    return sorted(out, key=str.lower)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def included_folders(cfg=None):
    """Oberste Ordner, die eingelesen werden – plus alle weiteren Fotoordner ("@Name"), auch wenn sie gerade
    nicht erreichbar sind: das Einlesen überspringt sie dann, ihre Fotos bleiben aber im Katalog."""
    cfg = cfg or load_config()
    if cfg.get("folders"):
        main = [f for f in cfg["folders"] if os.path.isdir(to_abs(f))]
    else:
        excl = {x.lower() for x in cfg.get("excluded_folders") or []}
        main = [f for f in top_level_folders() if f.lower() not in excl]
    return main + [SOURCE_PREFIX + n for n in SOURCES]


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    xmp_mtime REAL,
    taken TEXT,
    taken_src TEXT,
    width INTEGER,
    height INTEGER,
    duration REAL,
    lat REAL,
    lon REAL,
    place TEXT,
    camera TEXT,
    rating INTEGER DEFAULT 0,
    fav INTEGER DEFAULT 0,
    keywords TEXT,
    caption TEXT,
    proc_ver INTEGER DEFAULT 0,
    has_thumb INTEGER DEFAULT 0,
    dup_of INTEGER,
    raw_of INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS items_proc ON items(proc_ver);
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    cover_face INTEGER,
    hidden INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    x REAL, y REAL, w REAL, h REAL,
    px INTEGER,
    score REAL,
    emb BLOB,
    person_id INTEGER,
    source TEXT,
    sugg_person INTEGER,
    sugg_score REAL,
    rejected TEXT,
    cluster INTEGER
);
CREATE INDEX IF NOT EXISTS faces_item ON faces(item_id);
CREATE INDEX IF NOT EXISTS faces_sugg ON faces(sugg_person);
CREATE INDEX IF NOT EXISTS faces_cluster ON faces(cluster);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, tokenize="unicode61 remove_diacritics 2");
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    source TEXT DEFAULT 'mylio'
);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent INTEGER,
    description TEXT,
    source TEXT DEFAULT 'mylio'
);
CREATE TABLE IF NOT EXISTS album_items (
    album_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    PRIMARY KEY (album_id, item_id)
);
CREATE INDEX IF NOT EXISTS album_items_item ON album_items(item_id);
CREATE TABLE IF NOT EXISTS imports (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT,
    path TEXT,
    tag TEXT,
    album TEXT,
    at TEXT,
    applied INTEGER DEFAULT 0,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS album_removed (album_id INTEGER, item_id INTEGER, PRIMARY KEY (album_id, item_id));
-- vom Nutzer als "kein Duplikat" bestätigt (mark_duplicates fasst sie nicht zusammen)
CREATE TABLE IF NOT EXISTS dup_keep (item_id INTEGER PRIMARY KEY);
-- endgültig gelöscht (trash.purge): Merkmale, damit kein Import sie wieder holt (importer.LibraryIndex)
CREATE TABLE IF NOT EXISTS deleted_files (name TEXT, kind TEXT, size INTEGER, taken TEXT, width INTEGER, height INTEGER,
    phash INTEGER, deleted TEXT);
-- Videoschnitt (video.py): Projekte und Renderliste
CREATE TABLE IF NOT EXISTS vprojects (id INTEGER PRIMARY KEY, name TEXT, data TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS vrenders (id INTEGER PRIMARY KEY, project INTEGER, name TEXT, status TEXT, progress REAL,
    out TEXT, created TEXT, finished TEXT, error TEXT, seconds REAL, data TEXT);
"""

BLOB_SCHEMA = "CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, data BLOB NOT NULL);"


def connect(path=CATALOG_DB, schema=SCHEMA):
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA cache_size=-65536")
    con.executescript(schema)
    if schema is SCHEMA:
        cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
        for col, typ in (("phash", "INTEGER"), ("orient", "INTEGER"), ("rotfix", "INTEGER"), ("userrot", "INTEGER"),
                         ("hidden", "INTEGER DEFAULT 0"), ("usertaken", "TEXT"), ("usertags", "TEXT"),
                         ("private", "INTEGER DEFAULT 0"), ("priv_eff", "INTEGER DEFAULT 0"),
                         ("trashed", "TEXT"), ("trash_from", "TEXT"), ("trash_side", "TEXT"),
                         ("edit", "TEXT"), ("doc_lines", "INTEGER"), ("doc_area", "REAL"), ("doc_no", "INTEGER DEFAULT 0"),
                         ("ev_bias", "REAL"), ("exp_mode", "INTEGER"), ("stack", "INTEGER"), ("stack_top", "INTEGER"),
                         ("doc_state", "INTEGER"), ("pano", "TEXT")):
            if col not in cols:
                con.execute("ALTER TABLE items ADD COLUMN %s %s" % (col, typ))
        # Abdeckende Indizes: Übersicht, Kalender und Ordner lesen nur den Index statt jede Zeile
        # einzeln von der (USB-)Platte zu holen
        con.execute("CREATE INDEX IF NOT EXISTS items_cov_taken ON items(taken, id, kind, priv_eff, hidden, dup_of, "
                    "raw_of, taken_src, fav, rating, camera, ext, folder)")
        con.execute("CREATE INDEX IF NOT EXISTS items_cov_folder ON items(folder, dup_of, raw_of, hidden, priv_eff, id, "
                    "taken, kind, name)")
        for old in ("items_taken", "items_folder", "faces_person", "faces_cov_person"):  # durch die abdeckenden ersetzt
            con.execute("DROP INDEX IF EXISTS " + old)
        con.execute("CREATE INDEX IF NOT EXISTS items_doc ON items(doc_lines)")  # Dokumente finden (docs.py)
        con.execute("CREATE INDEX IF NOT EXISTS items_stack ON items(stack) WHERE stack IS NOT NULL")  # Belichtungsreihen
        con.execute("CREATE INDEX IF NOT EXISTS items_pano ON items(id) WHERE pano <> ''")  # 360°-Fotos (panos.py)
        con.execute("CREATE INDEX IF NOT EXISTS faces_cov_person2 ON faces(person_id, item_id, source, x, px, score)")
        con.execute("CREATE INDEX IF NOT EXISTS faces_cov_sugg ON faces(sugg_person, person_id, item_id)")
        acols = {r[1] for r in con.execute("PRAGMA table_info(albums)")}
        if "cover" not in acols:
            con.execute("ALTER TABLE albums ADD COLUMN cover INTEGER")
        if "private" not in acols:
            con.execute("ALTER TABLE albums ADD COLUMN private INTEGER DEFAULT 0")
        if "private" not in {r[1] for r in con.execute("PRAGMA table_info(events)")}:
            con.execute("ALTER TABLE events ADD COLUMN private INTEGER DEFAULT 0")
        con.commit()
    return con


class BlobStore:
    """Viele kleine Bilder in einer einzigen SQLite-Datei (spart Platz auf exFAT)."""

    def __init__(self, path):
        self.path = path
        self.local = threading.local()

    def con(self):
        c = getattr(self.local, "con", None)
        if c is None:
            c = self.local.con = connect(self.path, BLOB_SCHEMA)
        return c

    def get(self, key):
        row = self.con().execute("SELECT data FROM blobs WHERE id=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key, data, commit=True):
        self.con().execute("INSERT OR REPLACE INTO blobs(id, data) VALUES(?, ?)", (key, data))
        if commit:
            self.con().commit()

    def put_many(self, rows):
        c = self.con()
        c.executemany("INSERT OR REPLACE INTO blobs(id, data) VALUES(?, ?)", rows)
        c.commit()

    def delete(self, keys):
        c = self.con()
        c.executemany("DELETE FROM blobs WHERE id=?", [(k,) for k in keys])
        c.commit()


_edit_local = threading.local()


def edit_for(path):
    """Bearbeitung (items.edit) zu einer Datei – für alle Stellen, die Fotos zum Anzeigen/Exportieren öffnen."""
    c = getattr(_edit_local, "con", None)
    if c is None:
        c = _edit_local.con = connect()
    try:
        row = c.execute("SELECT edit FROM items WHERE path=?", (to_rel(path),)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


# Gesichts-Ausschnitte bekommen eigene Schlüssel im Thumbnail-Speicher.
FACE_KEY_OFFSET = 1 << 40

THUMBS = BlobStore(THUMBS_DB)
PREVIEWS = BlobStore(PREVIEWS_DB)
