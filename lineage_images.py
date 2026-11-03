"""
lineage_images.py — Lazy full-resolution image storage for lineage (.plf) files.

Mirrors the plant_db.py lazy-image architecture (per-node reads, evict-after-use,
merge-on-save) but scoped to a single lineage file:

  <name>.plf                 tree structure and node metadata.  Always loaded
                             eagerly by database.load_lineage().

  <name>.plf.images.sqlite3  full-resolution originals, one row per image.
                             SQLite lets us read/write just the node(s) that
                             were actually touched instead of parsing and
                             rewriting one giant JSON sidecar.  Old
                             <name>.plf.images JSON sidecars are migrated
                             automatically on first access.

Full-resolution data is still 100% present on disk; this module just keeps
it from being resident in RAM (or decoded to pixels) except when something
genuinely needs it.

Session-authoritative tracking
───────────────────────────────
`LineageTree._image_loaded_ids` (see models.py) records which nodes'
`.images` currently hold real, up-to-date data this session — either
because they were lazily loaded from the sidecar, or because the user just
set/edited them directly. On save, only nodes in that set are written back;
everything else is left exactly as it is on disk, so a node whose images
were never touched this session can't accidentally be wiped out just
because it's sitting in memory as `[]`.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import OrderedDict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_LEGACY_SUFFIX = ".images"
_SUFFIX        = ".images.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_images (
    node_id TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (node_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_node_images_node ON node_images(node_id);
"""

_migrate_lock = threading.Lock()
_READ_CONN_LIMIT = 8
_read_lock = threading.RLock()
_read_conns: "OrderedDict[str, sqlite3.Connection]" = OrderedDict()


def images_filepath(plf_path: str) -> str:
    return plf_path + _SUFFIX


def legacy_images_filepath(plf_path: str) -> str:
    return plf_path + _LEGACY_SUFFIX


def _connect(path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=10,
                           check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.executescript(_SCHEMA)
    return conn


def _get_read_conn(path: str) -> sqlite3.Connection:
    """Reuse a small pool of read connections instead of reopening SQLite for
    every node's lazy image load.  Guarded because the pool is shared by UI
    and worker threads; SQLite itself still serialises concurrent writers."""
    conn = _read_conns.pop(path, None)
    if conn is None:
        conn = _connect(path, check_same_thread=False)
    _read_conns[path] = conn
    while len(_read_conns) > _READ_CONN_LIMIT:
        _old_path, old_conn = _read_conns.popitem(last=False)
        try:
            old_conn.close()
        except Exception:
            pass
    return conn


def _drop_read_conn(path: str) -> None:
    conn = _read_conns.pop(path, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def close_read_connections(plf_path: Optional[str] = None) -> None:
    """Close cached read handles, optionally for one lineage file only.
    Called before deleting a sidecar so the file can be removed cleanly."""
    with _read_lock:
        if plf_path is None:
            while _read_conns:
                _old_path, conn = _read_conns.popitem(last=False)
                try:
                    conn.close()
                except Exception:
                    pass
            return
        _drop_read_conn(images_filepath(plf_path))


def _load_legacy(plf_path: Optional[str]) -> Dict[str, List[str]]:
    if not plf_path:
        return {}
    p = legacy_images_filepath(plf_path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): [str(b) for b in (v or [])]
                for k, v in data.items() if isinstance(v, list) and v}
    except Exception as e:
        log.error("lineage_images: failed to read legacy %s: %s", p, e)
        return {}


def _write_all(path: str, images_by_node: Dict[str, List[str]]) -> None:
    conn = _connect(path)
    try:
        with conn:
            conn.execute("DELETE FROM node_images")
            rows = [(nid, i, str(b))
                    for nid, imgs in images_by_node.items()
                    for i, b in enumerate(imgs) if b]
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO node_images(node_id,idx,data)"
                    " VALUES (?,?,?)", rows)
    finally:
        conn.close()


def _migrate_legacy(plf_path: Optional[str]) -> None:
    """Move an old JSON sidecar into SQLite exactly once.

    The old file is renamed to .images.migrated rather than deleted, so a
    failed upgrade can still be recovered manually.  The migration is a
    no-op for new-format files and for freshly created lineages.
    """
    if not plf_path:
        return
    db_path = images_filepath(plf_path)
    old_path = legacy_images_filepath(plf_path)
    if os.path.isfile(db_path) or not os.path.isfile(old_path):
        return
    with _migrate_lock:
        if os.path.isfile(db_path) or not os.path.isfile(old_path):
            return
        data = _load_legacy(plf_path)
        try:
            _write_all(db_path, data)
        except Exception as e:
            log.error("lineage_images: migration failed for %s: %s", old_path, e)
            # The database did not exist before this attempt; remove any
            # partial file so the next read retries from the untouched JSON.
            for p in (db_path, db_path + "-wal", db_path + "-shm"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return
        try:
            os.replace(old_path, old_path + ".migrated")
        except OSError as e:
            log.warning("lineage_images: could not archive %s: %s", old_path, e)
        log.info("lineage_images: migrated %s → %s (%d node(s))",
                 old_path, db_path, len(data))


def load_all_images(plf_path: Optional[str]) -> Dict[str, List[str]]:
    """Read every image row.  Only for explicit bulk access (export/bundle)."""
    if not plf_path:
        return {}
    _migrate_legacy(plf_path)
    db_path = images_filepath(plf_path)
    if not os.path.isfile(db_path):
        return _load_legacy(plf_path)
    out: Dict[str, List[str]] = {}
    try:
        with _read_lock:
            conn = _get_read_conn(db_path)
            try:
                for nid, data in conn.execute(
                        "SELECT node_id,data FROM node_images"
                        " ORDER BY node_id,idx"):
                    out.setdefault(nid, []).append(data)
            except Exception:
                _drop_read_conn(db_path)
                raise
    except Exception as e:
        log.error("lineage_images: failed to read %s: %s", db_path, e)
        return _load_legacy(plf_path)
    return out


def get_node_images(plf_path: Optional[str], node_id: str) -> List[str]:
    """Read just one node's full-resolution images.  The result isn't cached
    here — the caller decides whether to hold onto it."""
    if not plf_path:
        return []
    _migrate_legacy(plf_path)
    db_path = images_filepath(plf_path)
    if not os.path.isfile(db_path):
        return list(_load_legacy(plf_path).get(node_id, []))
    try:
        with _read_lock:
            conn = _get_read_conn(db_path)
            try:
                return [row[0] for row in conn.execute(
                    "SELECT data FROM node_images"
                    " WHERE node_id=? ORDER BY idx", (node_id,))]
            except Exception:
                _drop_read_conn(db_path)
                raise
    except Exception as e:
        log.error("lineage_images: get_node_images failed for %s: %s",
                  node_id, e)
        return []


def save_all_images(plf_path: str, images_by_node: Dict[str, List[str]]) -> bool:
    """Replace the whole store.  Used for migration and explicit bulk writes."""
    p = images_filepath(plf_path)
    cleaned: Dict[str, List[str]] = {}
    for nid, imgs in (images_by_node or {}).items():
        if not isinstance(imgs, (list, tuple)):
            continue
        vals = [str(b) for b in imgs if b]
        if vals:
            cleaned[str(nid)] = vals
    try:
        _write_all(p, cleaned)
        old = legacy_images_filepath(plf_path)
        if os.path.isfile(old):
            try:
                os.replace(old, old + ".migrated")
            except OSError:
                pass
        return True
    except Exception as e:
        log.error("lineage_images: failed to write %s: %s", p, e)
        return False


def delete_sidecar(plf_path: str) -> None:
    close_read_connections(plf_path)
    for p in (images_filepath(plf_path),
              legacy_images_filepath(plf_path),
              legacy_images_filepath(plf_path) + ".migrated"):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError as e:
            log.warning("lineage_images: could not remove %s: %s", p, e)
    for suffix in ("-wal", "-shm"):
        try:
            p = images_filepath(plf_path) + suffix
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


# ── Session-level helpers (operate on a LineageTree instance) ─────────────────

def populate_node_images(tree, plf_path: Optional[str], node_id: str) -> List[str]:
    """Make node.images authoritative for this session: load from the
    store on first touch, then just return what's already there. Call
    this before reading/editing a node's full images (edit dialog, copy,
    export, or mutating an existing node's photo)."""
    node = tree.nodes.get(node_id) if tree else None
    if node is None:
        return []
    if node_id not in tree._image_loaded_ids:
        if plf_path:
            node.images = get_node_images(plf_path, node_id)
        tree._image_loaded_ids.add(node_id)
    return node.images


def mark_images_authoritative(tree, node_id: str) -> None:
    """Mark a node's in-memory .images as the current truth (e.g. right
    after setting a brand-new photo) so it gets persisted on next save."""
    if tree is not None:
        tree._image_loaded_ids.add(node_id)


def snapshot_for_merge(tree) -> Dict[str, List[str]]:
    """Main-thread-only: copy just the data merge_and_save needs out of the
    live tree into a plain dict. Call this before handing work off to a
    background thread — the returned dict has no ties back to the tree, so
    a worker thread can safely read it while the UI thread keeps mutating
    the tree in parallel."""
    return {
        nid: list(tree.nodes[nid].images)
        for nid in tree._image_loaded_ids
        if nid in tree.nodes
    }


def merge_and_save_snapshot(plf_path: str, touched: Dict[str, List[str]],
                             valid_node_ids) -> bool:
    """File-I/O half of the sidecar merge — operate only on the touched
    nodes, so saving one edited photo no longer reads/writes every other
    node's full-resolution image rows."""
    try:
        _migrate_legacy(plf_path)
        db_path = images_filepath(plf_path)
        conn = _connect(db_path)
        valid = set(valid_node_ids)
        try:
            with conn:
                stored = {row[0] for row in conn.execute(
                    "SELECT DISTINCT node_id FROM node_images")}
                stale = stored - valid
                if stale:
                    conn.executemany(
                        "DELETE FROM node_images WHERE node_id=?",
                        [(nid,) for nid in stale])
                for nid, imgs in touched.items():
                    if nid not in valid:
                        continue
                    conn.execute(
                        "DELETE FROM node_images WHERE node_id=?", (nid,))
                    rows = [(nid, i, str(b))
                            for i, b in enumerate(imgs or []) if b]
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO node_images"
                            "(node_id,idx,data) VALUES (?,?,?)", rows)
        finally:
            conn.close()
        return True
    except Exception as e:
        log.error("lineage_images: merge save failed for %s: %s", plf_path, e)
        return False


def merge_and_save(tree, plf_path: str) -> bool:
    """Write touched nodes' current .images; every other node's on-disk
    rows are left alone, and entries for nodes no longer in the tree are
    dropped."""
    return merge_and_save_snapshot(
        plf_path, snapshot_for_merge(tree), set(tree.nodes.keys()))


def evict_saved_images(tree) -> None:
    """Free full-resolution bytes for nodes that were touched this session,
    now that they're safely persisted (on-disk matches memory). Safe to
    call right after a successful merge_and_save()."""
    for nid in list(tree._image_loaded_ids):
        node = tree.nodes.get(nid)
        if node is not None:
            node.images = []
    tree._image_loaded_ids.clear()
