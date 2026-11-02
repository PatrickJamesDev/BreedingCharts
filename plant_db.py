"""
plant_db.py — Global plant database.

Performance architecture (v2.0.0 — SQLite row store)
─────────────────────────────────────────────────────
The previous implementation kept every plant in one JSON object and rewrote
that *entire* file on every upsert (and the entire images JSON on every image
change).  That is fine for a few dozen plants but becomes a serious stall for
larger collections: renaming one plant rewrote every plant's metadata, and
opening the edit dialog for one plant parsed every plant's full-resolution
images.

This module now uses a small SQLite database (`plant_database.sqlite3` in the
data directory) with one row per plant and one row per image.  Writes touch
only the changed plant.  Reads still use the same in-memory caches so the hot
paths (`get_all`, `has_image`, card rendering) stay allocation-light:

_meta_cache : plant dicts WITHOUT 'images', but WITH 'thumb' (small JPEG) —
              always in RAM.  Loaded from SQLite once, then kept in sync.
_has_image_set : set of plant IDs with images — always in RAM.
_img_store  : full-resolution base64 image strings — LAZILY loaded per plant,
              EVICTED after use.  Opening the edit dialog for one plant reads
              only that plant's image rows instead of parsing a giant file.

RAM profile
  startup        : ~1-5 MB  (metadata + thumbnails only, zero full-res strings)
  first grid show: ~0 MB extra — thumbnails were already in _meta_cache
  edit dialog     : ~400 KB spike per plant (images reloaded for that plant)

Migration
─────────
On first use, if the SQLite database is empty and the old JSON files exist,
they are imported automatically.  The JSON files are left untouched as a
backup; the app no longer rewrites them.  Use the existing Import/Export Data
Bundle commands (which call export_all_for_bundle/import_from_dict) to move
data between machines or to keep a JSON snapshot.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import sys
import threading
import uuid
from typing import Dict, Iterable, List, Optional, Set

from models import PlantNode

log = logging.getLogger(__name__)


# ── File locations (legacy JSON paths kept for one-time migration) ─────────────

def _get_db_file() -> str:
    import database as _db
    return os.path.join(_db.get_data_dir(), "plant_database.json")


def _get_img_file() -> str:
    """Legacy separate image-only JSON."""
    import database as _db
    return os.path.join(_db.get_data_dir(), "plant_images.json")


def _old_db_file() -> str:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(os.path.dirname(sys.executable), "plant_database.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "plant_database.json")


def _sqlite_path() -> str:
    import database as _db
    return os.path.join(_db.get_data_dir(), "plant_database.sqlite3")


# ── Connection + caches ───────────────────────────────────────────────────────

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_db_path: str = ""

_meta_cache:    Optional[Dict[str, dict]] = None   # metadata, no images
_has_image_set: Set[str]                 = set()   # IDs with images on disk
_img_store:     Dict[str, List[str]]     = {}      # lazily loaded, evict after decode

_dirty_meta:    Set[str] = set()
_dirty_images:  Set[str] = set()
_meta_flush_timer: Optional[threading.Timer] = None

# Monotonically increasing counter bumped on every metadata mutation.  Views
# cache their last-built revision so an unrelated autosave (e.g. dragging one
# node) doesn't force an O(N) rebuild of a grid/sheet that didn't change.
_revision: int = 0


def _bump_revision_locked() -> None:
    """Record a metadata mutation.  Caller must hold _lock."""
    global _revision
    _revision += 1


def get_revision() -> int:
    """Current metadata revision.  Cheap enough to call on every refresh."""
    return _revision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
    id          TEXT PRIMARY KEY,
    meta_json   TEXT NOT NULL,
    name        TEXT,
    species     TEXT,
    cultivar    TEXT,
    search_text TEXT,
    has_image   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plants_name     ON plants(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_plants_species  ON plants(species COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_plants_cultivar ON plants(cultivar COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_plants_has_image ON plants(has_image);

CREATE TABLE IF NOT EXISTS images (
    plant_id TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    data     TEXT NOT NULL,
    PRIMARY KEY (plant_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_images_plant ON images(plant_id);
"""


def _search_text(meta: dict) -> str:
    """Lowercase haystack matching the previous search() semantics."""
    tags = meta.get("tags") or []
    if not isinstance(tags, (list, tuple)):
        tags = [tags]
    parts = [str(meta.get("name") or ""),
             str(meta.get("species") or ""),
             str(meta.get("cultivar") or "")]
    parts.extend(str(t) for t in tags)
    return " ".join(parts).lower()


def _meta_json(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=False, separators=(",", ":"))


def _ensure_conn() -> sqlite3.Connection:
    """Open (and if necessary create/migrate) the SQLite database."""
    global _conn, _db_path
    if _conn is not None:
        return _conn
    path = _sqlite_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32000")   # ~32 MB page cache
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:
        conn.close()
        raise
    _conn = conn
    _db_path = path
    _migrate_from_json_if_needed(conn)
    return conn


def _migrate_from_json_if_needed(conn: sqlite3.Connection) -> bool:
    """One-time import of the old JSON files when the SQLite store is empty."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM plants").fetchone()[0]
    except Exception:
        count = 0
    if count > 0:
        return False

    full_data: Dict[str, dict] = {}
    db_file = _get_db_file()
    if os.path.isfile(db_file):
        try:
            with open(db_file, "r", encoding="utf-8") as fh:
                full_data = json.load(fh)
        except Exception as e:
            log.error("plant_db migration: could not read %s: %s", db_file, e)

    # Legacy database next to the executable/script (merged only if missing).
    old_file = _old_db_file()
    if old_file != db_file and os.path.isfile(old_file):
        try:
            with open(old_file, "r", encoding="utf-8") as fh:
                old_data = json.load(fh)
            missing = {k: v for k, v in old_data.items() if k not in full_data}
            if missing:
                full_data.update(missing)
                log.info("plant_db migration: merged %d plant(s) from legacy file",
                         len(missing))
        except Exception as e:
            log.error("plant_db migration: legacy merge failed: %s", e)

    img_data: Dict[str, list] = {}
    img_file = _get_img_file()
    if os.path.isfile(img_file):
        try:
            with open(img_file, "r", encoding="utf-8") as fh:
                img_data = json.load(fh)
        except Exception as e:
            log.warning("plant_db migration: could not read %s: %s", img_file, e)

    if not full_data and not img_data:
        return False

    imported = 0
    try:
        with conn:
            for pid, d in full_data.items():
                if not isinstance(d, dict):
                    continue
                meta = {k: v for k, v in d.items()
                        if k not in ("images", "image_data")}
                meta["id"] = pid
                # Prefer the split images file when it has this plant.
                if pid in img_data:
                    imgs = img_data.get(pid) or []
                else:
                    imgs = d.get("images") or (
                        [d["image_data"]] if d.get("image_data") else [])
                has = 1 if imgs else 0
                conn.execute(
                    "INSERT OR REPLACE INTO plants"
                    "(id,meta_json,name,species,cultivar,search_text,has_image)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (pid, _meta_json(meta),
                     meta.get("name") or "", meta.get("species") or "",
                     meta.get("cultivar") or "", _search_text(meta), has))
                if imgs:
                    conn.executemany(
                        "INSERT OR REPLACE INTO images(plant_id,idx,data)"
                        " VALUES (?,?,?)",
                        [(pid, i, str(b)) for i, b in enumerate(imgs)])
                imported += 1

            # Images whose metadata row is absent are still preserved so
            # get_images()/exports don't silently lose them.  They simply
            # won't appear in get_all() (there is no plant to display).
            for pid, imgs in img_data.items():
                if pid in full_data or not imgs:
                    continue
                conn.executemany(
                    "INSERT OR REPLACE INTO images(plant_id,idx,data)"
                    " VALUES (?,?,?)",
                    [(pid, i, str(b)) for i, b in enumerate(imgs)])
    except Exception as e:
        log.error("plant_db migration: failed: %s", e)
        return False

    log.info("plant_db: migrated %d plant(s) from JSON to SQLite (%s)",
             imported, _db_path)
    return True


# ── Loading ───────────────────────────────────────────────────────────────────

def _load_raw() -> Dict[str, dict]:
    """Populate _meta_cache and _has_image_set; return _meta_cache."""
    global _meta_cache, _has_image_set
    with _lock:
        if _meta_cache is not None:
            return _meta_cache

        try:
            conn = _ensure_conn()
        except Exception as e:
            log.error("plant_db: could not open SQLite store: %s", e)
            _meta_cache = {}
            _has_image_set = set()
            return _meta_cache

        meta_cache: Dict[str, dict] = {}
        has_images: Set[str] = set()
        try:
            for pid, meta_json in conn.execute("SELECT id, meta_json FROM plants"):
                try:
                    d = json.loads(meta_json)
                    d["id"] = pid
                    d.pop("images", None)
                    d.pop("image_data", None)
                    meta_cache[pid] = d
                except Exception as e:
                    log.warning("plant_db: skipping bad row %s: %s", pid, e)
            for (pid,) in conn.execute("SELECT DISTINCT plant_id FROM images"):
                has_images.add(pid)
        except Exception as e:
            log.error("plant_db: load error: %s", e)

        _meta_cache = meta_cache
        _has_image_set = has_images
        log.info("plant_db: loaded %d plant(s) (%d with images) from SQLite",
                 len(meta_cache), len(has_images))
        return _meta_cache


def _load_all_images_map_locked() -> Dict[str, List[str]]:
    """Load every image row grouped by plant.  Only for explicit bulk exports."""
    conn = _ensure_conn()
    out: Dict[str, List[str]] = {}
    try:
        rows = conn.execute(
            "SELECT plant_id, data FROM images ORDER BY plant_id, idx")
        for pid, data in rows:
            out.setdefault(pid, []).append(data)
    except Exception as e:
        log.error("plant_db: bulk image load failed: %s", e)
    return out


def _reload_plant_images(plant_id: str) -> List[str]:
    """Read one plant's full-resolution images from SQLite (no whole-file parse)."""
    with _lock:
        _load_raw()
        if plant_id not in _has_image_set:
            return []
        try:
            conn = _ensure_conn()
            rows = conn.execute(
                "SELECT data FROM images WHERE plant_id=? ORDER BY idx",
                (plant_id,)).fetchall()
            imgs = [r[0] for r in rows]
            if imgs:
                _img_store[plant_id] = imgs
            return imgs
        except Exception as e:
            log.error("plant_db: _reload_plant_images failed for %s: %s",
                      plant_id, e)
            return []


# ── Saving (dirty-row flushing) ───────────────────────────────────────────────

def _flush_meta_locked() -> None:
    """Write only dirty metadata rows.  Caller must hold _lock."""
    global _dirty_meta
    if _conn is None or not _dirty_meta:
        return
    dirty = _dirty_meta
    _dirty_meta = set()
    try:
        with _conn:
            for pid in dirty:
                meta = (_meta_cache or {}).get(pid)
                if meta is None:
                    _conn.execute("DELETE FROM plants WHERE id=?", (pid,))
                else:
                    _conn.execute(
                        "INSERT OR REPLACE INTO plants"
                        "(id,meta_json,name,species,cultivar,search_text,has_image)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (pid, _meta_json(meta),
                         meta.get("name") or "", meta.get("species") or "",
                         meta.get("cultivar") or "", _search_text(meta),
                         1 if pid in _has_image_set else 0))
    except Exception as e:
        log.error("plant_db: meta save error: %s", e)
        _dirty_meta.update(dirty)


def _flush_images_locked() -> None:
    """Write only dirty image sets.  Caller must hold _lock."""
    global _dirty_images
    if _conn is None or not _dirty_images:
        return
    dirty = _dirty_images
    _dirty_images = set()
    try:
        with _conn:
            for pid in dirty:
                _conn.execute("DELETE FROM images WHERE plant_id=?", (pid,))
                imgs = _img_store.get(pid) or []
                if imgs:
                    _conn.executemany(
                        "INSERT INTO images(plant_id,idx,data) VALUES (?,?,?)",
                        [(pid, i, str(b)) for i, b in enumerate(imgs)])
                _conn.execute("UPDATE plants SET has_image=? WHERE id=?",
                              (1 if imgs else 0, pid))
    except Exception as e:
        log.error("plant_db: image save error: %s", e)
        _dirty_images.update(dirty)


def _flush_meta() -> None:
    with _lock:
        _flush_meta_locked()


def _flush_images() -> None:
    with _lock:
        _flush_images_locked()


def _cancel_meta_flush_locked() -> None:
    global _meta_flush_timer
    if _meta_flush_timer is not None:
        _meta_flush_timer.cancel()
        _meta_flush_timer = None


def _schedule_meta_flush_locked() -> None:
    """Coalesce many thumbnail-only metadata writes into one commit.

    Called from MiniPlantCard when a legacy thumbnail is generated.  A large
    collection can generate thousands of these while scrolling; deferring the
    row updates briefly turns that into a handful of transactions instead of
    one commit per card.  Thumbnails are regenerable, so a hard crash losing
    the last few hundred milliseconds of them is harmless.
    """
    global _meta_flush_timer
    _cancel_meta_flush_locked()
    timer = threading.Timer(0.4, _flush_meta_safe)
    timer.daemon = True
    _meta_flush_timer = timer
    timer.start()


def _flush_meta_safe() -> None:
    global _meta_flush_timer
    with _lock:
        _meta_flush_timer = None
    _flush_meta()


def _shutdown() -> None:
    """Flush pending writes and close the SQLite connection at process exit."""
    global _conn
    with _lock:
        _cancel_meta_flush_locked()
        _flush_meta_locked()
        _flush_images_locked()
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


atexit.register(_shutdown)


def reset_cache() -> None:
    """Invalidate all caches and close the current database connection.

    Call when the data directory changes.  Pending writes to the *old*
    database are flushed first.
    """
    global _meta_cache, _has_image_set, _img_store
    global _dirty_meta, _dirty_images, _conn, _db_path
    with _lock:
        _cancel_meta_flush_locked()
        _flush_meta_locked()
        _flush_images_locked()
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        _db_path = ""
        _meta_cache = None
        _has_image_set = set()
        _img_store = {}
        _dirty_meta = set()
        _dirty_images = set()
    log.debug("plant_db: cache invalidated")


# ── Public API ────────────────────────────────────────────────────────────────

def has_image(plant_id: str) -> bool:
    """True if this plant has at least one image — fast, no image data loaded."""
    _load_raw()
    with _lock:
        return plant_id in _has_image_set


def has_node(plant_id: str) -> bool:
    """True if this plant ID exists in the database."""
    _load_raw()
    with _lock:
        return plant_id in (_meta_cache or {})


def get_images(plant_id: str) -> List[str]:
    """Return image list (base64 strings) for a plant.

    In-memory cache first; otherwise read only this plant's rows from SQLite.
    """
    with _lock:
        _load_raw()
        if plant_id in _img_store:
            return list(_img_store[plant_id])
        if plant_id not in _has_image_set:
            return []
        return _reload_plant_images(plant_id)


def evict_images(plant_id: str) -> None:
    """Free the base64 strings for a plant after its thumbnail was decoded.

    The thumbnail lives in QPixmapCache; the raw strings are no longer needed
    for display.  Call this in MiniPlantCard._load_image() after a successful
    thumbnail decode to reclaim RAM.  Evicting only clears the in-memory cache;
    the rows remain in SQLite.
    """
    with _lock:
        _img_store.pop(plant_id, None)


def populate_images(node: PlantNode) -> None:
    """Fill node.images from the image store (for edit dialogs, lineage adds)."""
    node.images = list(get_images(node.id))


def set_thumb(plant_id: str, thumb: Optional[str]) -> None:
    """Persist a (re)generated thumbnail without touching full-res images.

    Used when a legacy plant (saved before thumbnails existed) gets its
    thumbnail generated on first display — this is a metadata-only row update,
    much cheaper than the old whole-file rewrite.  Writes are coalesced.
    """
    _load_raw()
    with _lock:
        meta = (_meta_cache or {}).get(plant_id)
        if meta is None:
            return
        meta["thumb"] = thumb
        _dirty_meta.add(plant_id)
        _schedule_meta_flush_locked()


def _meta_to_plant(pid: str, meta: dict) -> Optional[PlantNode]:
    """Construct a PlantNode from cached metadata (no image data)."""
    try:
        d = dict(meta)
        d["id"] = pid
        d["images"] = []
        node = PlantNode.from_dict(d)
        if not node.thumb and node.photo_count == 0 and pid in _has_image_set:
            # Legacy plant: has real images on disk but was saved before
            # thumb/photo_count existed.  photo_count=1 flips has_photo True;
            # MiniPlantCard._load_image() does its normal legacy fallback.
            node.photo_count = 1
        return node
    except Exception as e:
        log.warning("plant_db: skipping bad entry %s: %s", pid, e)
        return None


def get_all() -> List[PlantNode]:
    """Return all plants sorted by name — WITHOUT image data (lightweight)."""
    data = _load_raw()
    plants = []
    with _lock:
        for pid, meta in data.items():
            node = _meta_to_plant(pid, meta)
            if node is not None:
                plants.append(node)
    return sorted(plants, key=lambda p: (p.name or "").lower())


_hay: dict = {}   # id -> (meta dict identity, lower-cased haystack)


def search(query: str) -> List[PlantNode]:
    """Return plants matching query, constructing only matching PlantNodes.

    The previous implementation built a PlantNode for every plant on every
    keystroke and then filtered.  Searching the cached metadata first avoids
    thousands of throwaway object constructions for a narrow query.
    """
    q = query.strip().lower()
    if not q:
        return get_all()

    data = _load_raw()
    matches: List[PlantNode] = []
    with _lock:
        for pid, meta in data.items():
            haystack = _hay.get(pid)
            if haystack is None or haystack[0] is not meta:
                haystack = _hay[pid] = (meta, _search_text(meta))
            if q in haystack[1]:
                node = _meta_to_plant(pid, meta)
                if node is not None:
                    matches.append(node)
    return sorted(matches, key=lambda p: (p.name or "").lower())


def upsert(node: PlantNode) -> None:
    """Add or update a plant.  Writes only this plant's row(s).

    Image semantics are preserved from the JSON implementation:
      • If node.images is non-empty, those become the plant's images.
      • If node.images is empty AND the images were previously loaded into
        _img_store (meaning the caller had a chance to see/change them),
        the images are cleared.
      • If node.images is empty and images were never loaded this session,
        the existing on-disk images are left untouched — an autosave that
        only changed a name or position must not wipe photos it never read.
    """
    _load_raw()
    with _lock:
        try:
            d = node.to_dict()
            imgs = d.pop("images", [])
            d.pop("image_data", None)
            d["id"] = node.id
            _meta_cache[node.id] = d          # type: ignore[index]
            _dirty_meta.add(node.id)

            prev_imgs = _img_store.get(node.id)
            if imgs:
                _img_store[node.id] = list(imgs)
                _has_image_set.add(node.id)
                # A name/species/position-only edit still carries the same
                # image list it loaded for the dialog.  Re-writing megabytes
                # of unchanged base64 blobs on every save was one of the
                # largest remaining stalls; compare first and skip the
                # image rows entirely when nothing actually changed.
                if prev_imgs is None or imgs != prev_imgs:
                    _dirty_images.add(node.id)
            elif prev_imgs is not None:
                _img_store.pop(node.id, None)
                _has_image_set.discard(node.id)
                _dirty_images.add(node.id)
            # else: images were never loaded — leave existing rows alone.

            _flush_meta_locked()
            _flush_images_locked()
        except Exception as e:
            log.error("plant_db: upsert error: %s", e)


def remove(node_id: str) -> None:
    _load_raw()
    with _lock:
        try:
            (_meta_cache or {}).pop(node_id, None)
            _img_store.pop(node_id, None)
            _hay.pop(node_id, None)
            _has_image_set.discard(node_id)
            _dirty_meta.add(node_id)
            _dirty_images.add(node_id)
            _flush_meta_locked()
            _flush_images_locked()
        except Exception as e:
            log.error("plant_db: remove error: %s", e)


def delete(node_id: str) -> None:
    remove(node_id)


def remove_many(node_ids: Iterable[str]) -> int:
    """Delete many plants in ONE transaction.

    Used by bulk-delete actions.  The previous per-plant loop committed a
    separate SQLite transaction for every row (and two more for its images),
    so deleting a large selection could visibly freeze the UI.  This removes
    them all in a single commit instead.  Returns the number of IDs handled.
    """
    _load_raw()
    ids = [str(i) for i in node_ids]
    if not ids:
        return 0
    with _lock:
        try:
            for nid in ids:
                (_meta_cache or {}).pop(nid, None)
                _img_store.pop(nid, None)
                _hay.pop(nid, None)
                _has_image_set.discard(nid)
                _dirty_meta.discard(nid)
                _dirty_images.discard(nid)
            if _conn is not None:
                with _conn:
                    _conn.executemany(
                        "DELETE FROM images WHERE plant_id=?", [(i,) for i in ids])
                    _conn.executemany(
                        "DELETE FROM plants WHERE id=?", [(i,) for i in ids])
        except Exception as e:
            log.error("plant_db: remove_many error: %s", e)
            return 0
    return len(ids)


def clone_for_lineage(db_node: PlantNode) -> PlantNode:
    """Return a lineage-ready copy with a fresh ID.
    DEPRECATED — prefer emitting the original node and calling populate_images.
    Kept for any callers that still need UUID isolation."""
    populate_images(db_node)
    d = db_node.to_dict()
    d["id"] = str(uuid.uuid4())
    d["parent_ids"] = []
    d["x"] = 0.0
    d["y"] = 0.0
    return PlantNode.from_dict(d)


def import_from_file(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            incoming: Dict[str, dict] = json.load(fh)
    except Exception as e:
        log.error("plant_db: import_from_file read error %s: %s", path, e)
        return -1
    added = import_from_dict(incoming)
    log.info("plant_db: imported %d plant(s) from %s", added, path)
    return added


def import_from_dict(incoming: Dict[str, dict]) -> int:
    """Merge an already-parsed {id: plant_dict} mapping into the local DB.

    Never overwrites: any incoming id that already exists locally is left
    exactly as it is, so importing the same file twice is always safe to
    re-run.  Images are stored lazily (not loaded into RAM) so a large import
    doesn't balloon memory; cards will decode their thumbnails as usual.
    """
    _load_raw()
    added = 0
    with _lock:
        try:
            conn = _ensure_conn()
            with conn:
                for pid, d in incoming.items():
                    if pid in (_meta_cache or {}):
                        continue
                    if not isinstance(d, dict):
                        continue
                    meta = {k: v for k, v in d.items()
                            if k not in ("images", "image_data")}
                    meta["id"] = pid
                    _meta_cache[pid] = meta           # type: ignore[index]
                    imgs = d.get("images") or (
                        [d["image_data"]] if d.get("image_data") else [])
                    if imgs:
                        _has_image_set.add(pid)
                    conn.execute(
                        "INSERT OR REPLACE INTO plants"
                        "(id,meta_json,name,species,cultivar,search_text,has_image)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (pid, _meta_json(meta),
                         meta.get("name") or "", meta.get("species") or "",
                         meta.get("cultivar") or "", _search_text(meta),
                         1 if imgs else 0))
                    if imgs:
                        conn.executemany(
                            "INSERT OR REPLACE INTO images(plant_id,idx,data)"
                            " VALUES (?,?,?)",
                            [(pid, i, str(b)) for i, b in enumerate(imgs)])
                    added += 1
        except Exception as e:
            log.error("plant_db: import_from_dict error: %s", e)
            return added

    log.info("plant_db: merged %d new plant(s)", added)
    return added


def export_all_for_bundle() -> Dict[str, dict]:
    """Return every plant, metadata + full-resolution images inline.

    Used by the Export Data Bundle feature.  Unlike normal lazy usage, this
    touches every plant's full-resolution images at once — only call it for an
    explicit, one-off user export, never on a hot path.
    """
    _load_raw()
    with _lock:
        _flush_meta_locked()
        _flush_images_locked()
        img_map = _load_all_images_map_locked()
        out: Dict[str, dict] = {}
        for pid, meta in (_meta_cache or {}).items():
            d = dict(meta)
            d.pop("images", None)
            d.pop("image_data", None)
            d["images"] = img_map.get(pid, [])
            out[pid] = d
        return out


# Per-card thumbnail cost: MiniPlantCard caches one QPixmap per plant at a
# fixed CARD_W×IMG_H (see grid_view.py) regardless of zoom/column count, at
# 4 bytes/pixel (ARGB32). 180×120×4 = 86,400 bytes ≈ 84.4 KB. Kept here
# rather than imported from grid_view so this module has no PyQt6 dependency.
_THUMB_COST_KB = 84.4


def recommended_pixmap_cache_kb() -> int:
    """A QPixmapCache limit sized to this database.

    Counts image rows directly in SQLite so this does not need to load every
    plant's metadata first.  Formula: (plants with a photo) × (cost per cached
    thumbnail) × 1.3 safety margin, clamped to [20 MB, 300 MB].
    """
    try:
        conn = _ensure_conn()
        n = conn.execute(
            "SELECT COUNT(DISTINCT plant_id) FROM images").fetchone()[0]
    except Exception:
        _load_raw()
        n = len(_has_image_set)
    kb = n * _THUMB_COST_KB * 1.3
    return int(min(300 * 1024, max(20 * 1024, kb)))


def debug_ram_stats() -> dict:
    """Snapshot of this module's current in-RAM footprint."""
    _load_raw()
    with _lock:
        meta = _meta_cache or {}
        meta_bytes = 0
        if meta:
            try:
                meta_bytes = len(json.dumps(meta, ensure_ascii=False))
            except Exception:
                meta_bytes = 0
        img_bytes = sum(len(s) for imgs in _img_store.values() for s in imgs)
        return {
            "meta_plant_count":             len(meta),
            "meta_bytes_approx":            meta_bytes,
            "has_image_count":              len(_has_image_set),
            "images_resident_plant_count":  len(_img_store),
            "images_resident_bytes_approx": img_bytes,
        }


# ── F26: metadata revision — bump on every mutation so views can skip no-op rebuilds ──
def _with_revision(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        finally:
            with _lock:
                _bump_revision_locked()
    return wrapper

for _name in ("upsert", "remove", "delete", "remove_many", "set_thumb", "import_from_dict", "reset_cache"):
    if _name in globals():
        globals()[_name] = _with_revision(globals()[_name])
# `delete` calls the module-level `remove`; the extra bump is harmless.
