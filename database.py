"""
database.py — Save/load lineage files, file discovery, sidebar order.

v1.2.0 perf:
  • get_lineage_meta() uses an mtime-keyed cache — the file is only re-parsed
    when its modification time changes.  Sidebar refresh of 20 large files goes
    from ~3 s (20 × JSON parse) to <5 ms (20 × stat() calls).
  • data_dir is now configurable (see get_data_dir / set_data_dir).  Defaults
    to ~/BreedingCharts_Data so .plf files survive application updates.
"""
from __future__ import annotations
import json, logging, os, sys
from typing import List, Optional, Tuple
from models import LineageTree

log         = logging.getLogger(__name__)
LINEAGE_EXT = ".plf"


def _get_writable_dir() -> str:
    """Return a writable directory for app config files.

    When frozen by PyInstaller (--onefile), ``__file__`` resolves to the
    temporary *_MEIPASS* extraction folder, which is **read-only** at runtime
    and is deleted on exit.  Any writes there (settings.json, data_dir.txt,
    plant_database.json) silently vanish.

    When frozen, ``sys.executable`` is the path to the .exe itself, so its
    parent directory is the correct writable location (next to the .exe).
    When running from source, use the directory that contains this .py file.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _get_writable_dir()

# ── Configurable data directory ───────────────────────────────────────────────
# Stored separately from APP_DIR so users can upgrade the app without moving files.

_DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "BreedingCharts_Data")
_data_dir: str = ""   # empty = not yet loaded; call _load_data_dir() first


def _load_data_dir() -> str:
    """Read the data directory pointer from the settings JSON stored in APP_DIR."""
    global _data_dir
    if _data_dir:
        return _data_dir
    ptr_file = os.path.join(APP_DIR, "data_dir.txt")
    if os.path.isfile(ptr_file):
        try:
            with open(ptr_file, encoding="utf-8") as f:
                d = f.read().strip()
            if d and os.path.isdir(d):
                _data_dir = d
                return _data_dir
        except OSError:
            pass
    _data_dir = _DEFAULT_DATA_DIR
    return _data_dir


_made_dirs: set = set()


def get_data_dir() -> str:
    """Return the active data directory, creating it if needed."""
    d = _load_data_dir()
    if d not in _made_dirs:                 # F48: one makedirs per directory, not per call
        os.makedirs(d, exist_ok=True)
        _made_dirs.add(d)
    return d


def set_data_dir(new_dir: str) -> bool:
    """Persist a new data directory. Creates the folder if it doesn't exist.
    Returns True on success."""
    global _data_dir
    try:
        os.makedirs(new_dir, exist_ok=True)
        ptr_file = os.path.join(APP_DIR, "data_dir.txt")
        from fileio import atomic_write_bytes
        atomic_write_bytes(ptr_file, new_dir.encode("utf-8"))
        _data_dir = new_dir
        log.info("Data directory set → %s", new_dir)
        return True
    except OSError as e:
        log.error("set_data_dir(%s): %s", new_dir, e)
        return False


# ── mtime-keyed metadata cache ────────────────────────────────────────────────
# Key: filepath → (mtime_float, meta_dict)
# Invalidated automatically when the file is modified on disk.

_meta_cache: dict[str, Tuple[float, dict]] = {}


def _invalidate_meta(filepath: str) -> None:
    _meta_cache.pop(filepath, None)


def get_lineage_files(directory: str = None) -> List[str]:
    directory = directory or get_data_dir()
    try:
        return sorted(
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if f.lower().endswith(LINEAGE_EXT)
        )
    except OSError as e:
        log.error("get_lineage_files: %s", e)
        return []


def _meta_path(filepath: str) -> str:
    return filepath + ".meta"


def _meta_from_doc(d: dict) -> dict:
    return {
        "name":        d.get("name", "Unknown"),
        "description": d.get("description", ""),
        "node_count":  len(d.get("nodes", {})),
        "modified":    d.get("modified_at", "")[:10],
        "category":    d.get("category", ""),
        "sort_index":  int(d.get("sort_index", 0)),
    }


def _normalise_meta(raw: dict) -> dict:
    base = _meta_from_doc({})
    base.update({k: raw[k] for k in base if k in raw})
    base["node_count"] = int(base.get("node_count") or 0)
    base["sort_index"] = int(base.get("sort_index") or 0)
    return base


def write_lineage_meta(filepath: str, d: dict) -> dict:
    """Write the small <file>.plf.meta sidecar (best effort) and return its meta."""
    meta = _meta_from_doc(d)
    try:
        from fileio import atomic_write_json
        atomic_write_json(_meta_path(filepath), meta)
    except Exception as e:
        log.warning("write_lineage_meta(%s): %s", filepath, e)
    return meta


def delete_lineage_meta(filepath: str) -> None:
    try:
        os.remove(_meta_path(filepath))
    except OSError:
        pass


def get_lineage_meta(filepath: str) -> dict:
    """Return lightweight metadata for a lineage file.

    Uses an mtime cache plus the small <file>.plf.meta sidecar written on save.
    Listing a large library therefore reads a few hundred bytes per lineage
    instead of re-parsing every .plf (which embeds every thumbnail).
    """
    try:
        mtime = os.path.getmtime(filepath)
        cached = _meta_cache.get(filepath)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            if os.path.getmtime(_meta_path(filepath)) >= mtime:
                with open(_meta_path(filepath), "r", encoding="utf-8") as fh:
                    meta = _normalise_meta(json.load(fh))
                _meta_cache[filepath] = (mtime, meta)
                return meta
        except Exception:
            pass

        with open(filepath, "r", encoding="utf-8") as fh:
            d = json.load(fh)

        meta = write_lineage_meta(filepath, d)
        _meta_cache[filepath] = (mtime, meta)
        return meta

    except Exception as e:
        log.warning("get_lineage_meta(%s): %s", filepath, e)
        return {
            "name": os.path.basename(filepath),
            "description": "", "node_count": 0, "modified": "",
            "category": "", "sort_index": 0,
        }


def save_lineage(tree: LineageTree, filepath: str) -> bool:
    """Write the lineage file.

    Full-resolution images are NOT written inline here — they go to a
    separate lazily-loaded sidecar (see lineage_images.py) so that opening
    or listing this file never has to touch them. Only nodes touched this
    session (lazily loaded, or freshly added/edited) are merged into the
    sidecar; every other node's on-disk images are left exactly as they are.
    `thumb` (small, cheap) stays inline as before — that's what card
    rendering uses.

    The sidecar is SQLite-backed and the merge touches only the nodes whose
    images were loaded/edited this session (plus deleting rows for removed
    nodes), so a metadata-only autosave — the overwhelming majority — never
    reads or writes any full-resolution image data.
    """
    import lineage_images
    try:
        tree.touch()
        d = tree.to_dict()
        for node_d in d.get("nodes", {}).values():
            node_d["images"] = []   # real data lives in the sidecar

        from fileio import atomic_write_json, path_lock
        with path_lock(filepath):
            # Images first: if the sidecar merge fails, the document must not
            # be written, otherwise it could reference photos that never made
            # it to disk.  Eviction waits until both writes succeed.
            need_merge = bool(tree._image_loaded_ids or tree._images_maybe_stale)
            if need_merge:
                if not lineage_images.merge_and_save(tree, filepath):
                    raise RuntimeError("lineage image sidecar merge failed")
            atomic_write_json(filepath, d)

        if need_merge:
            lineage_images.evict_saved_images(tree)
            tree._images_maybe_stale = False

        meta = write_lineage_meta(filepath, d)
        try:
            _meta_cache[filepath] = (os.path.getmtime(filepath), meta)
        except OSError:
            _invalidate_meta(filepath)
        log.info("Saved '%s' → %s", tree.name, filepath)
        return True
    except Exception as e:
        log.error("save_lineage: %s", e)
        return False


def prepare_save(tree: LineageTree) -> dict:
    """Main-thread-only half of a save: snapshot everything a save needs to
    write into plain data structures. Cheap (no disk I/O) — just copies
    references out of the live tree, so it's safe to call synchronously
    from the UI thread even while the user keeps interacting.

    Pair with write_prepared_save() to do the actual (potentially slower —
    e.g. when the images sidecar needs merging) disk I/O on a background
    thread without that thread ever touching the live tree object. This is
    what the debounced autosave uses, since it can fire mid-interaction and
    a synchronous save_lineage() call would otherwise stall the UI thread
    for the duration of the write (and, on the rarer save that also merges
    the images sidecar, for a read+rewrite of that whole file).
    """
    import lineage_images
    tree.touch()
    d = tree.to_dict()
    for node_d in d.get("nodes", {}).values():
        node_d["images"] = []
    need_image_merge = bool(tree._image_loaded_ids or tree._images_maybe_stale)
    images_snapshot = (lineage_images.snapshot_for_merge(tree)
                       if need_image_merge else {})
    return {
        "doc": d,
        "need_image_merge": need_image_merge,
        "images_snapshot": images_snapshot,
        "valid_ids": set(tree.nodes.keys()),
    }


def write_prepared_save(prepared: dict, filepath: str) -> bool:
    """Disk-I/O-only half of a save. Touches only plain data handed to it
    (never the live tree/app state), so this is safe to run on a
    background thread."""
    import lineage_images
    try:
        from fileio import atomic_write_json, path_lock
        with path_lock(filepath):
            if prepared["need_image_merge"]:
                if not lineage_images.merge_and_save_snapshot(
                        filepath, prepared["images_snapshot"],
                        prepared["valid_ids"]):
                    raise RuntimeError("lineage image sidecar merge failed")
            atomic_write_json(filepath, prepared["doc"])
        d = prepared["doc"]
        meta = write_lineage_meta(filepath, d)
        try:
            _meta_cache[filepath] = (os.path.getmtime(filepath), meta)
        except OSError:
            _invalidate_meta(filepath)
        log.info("Saved (background) → %s", filepath)
        return True
    except Exception as e:
        log.error("write_prepared_save: %s", e)
        return False


def load_lineage(filepath: str) -> Optional[LineageTree]:
    import perf
    with perf.timed(f"load_lineage({os.path.basename(filepath)})"):
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            tree = LineageTree.from_dict(d)
            _migrate_legacy_inline_images(tree, d, filepath)
            log.info("Loaded '%s' (%d nodes)", tree.name, len(tree.nodes))
            return tree
        except Exception as e:
            log.error("load_lineage(%s): %s", filepath, e)
            return None


def _migrate_legacy_inline_images(tree: LineageTree, raw_dict: dict, filepath: str) -> None:
    """One-time upgrade for .plf files saved before the lazy-image sidecar
    existed: their full images are embedded directly per node. Split those
    out into the sidecar, generate a `thumb` for any node that doesn't have
    one yet, then rewrite the main file so every later load is fast and
    doesn't re-trigger this. New-format files (images already stripped, see
    save_lineage above) hit none of this — it's a no-op.
    """
    import image_utils
    import lineage_images
    legacy: dict = {}
    for nid, node_d in raw_dict.get("nodes", {}).items():
        node = tree.nodes.get(nid)
        if node is None:
            continue
        inline_imgs = node_d.get("images") or (
            [node_d["image_data"]] if node_d.get("image_data") else [])
        if not inline_imgs:
            continue
        legacy[nid] = inline_imgs
        if not node.thumb:
            node.thumb = image_utils.make_thumb_from_b64(inline_imgs[0])
        node.photo_count = len(inline_imgs)
        node.images = []   # don't keep it resident — it's headed for the sidecar

    if not legacy:
        return

    on_disk = lineage_images.load_all_images(filepath)
    on_disk.update(legacy)
    if not lineage_images.save_all_images(filepath, on_disk):
        log.error("_migrate_legacy_inline_images: image sidecar write failed "
                  "for %s; leaving inline images in place", filepath)
        return

    try:
        d = tree.to_dict()
        for node_d in d.get("nodes", {}).values():
            node_d["images"] = []
        from fileio import atomic_write_json
        atomic_write_json(filepath, d)
        write_lineage_meta(filepath, d)
        _invalidate_meta(filepath)
    except Exception as e:
        log.error("_migrate_legacy_inline_images: rewrite failed for %s: %s", filepath, e)
    log.info("Migrated %d node(s) with inline images → %s",
              len(legacy), lineage_images.images_filepath(filepath))


def default_filepath(name: str, directory: str = None) -> str:
    directory = directory or get_data_dir()
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()
    safe = safe.replace(" ", "_") or "lineage"
    return os.path.join(directory, safe + LINEAGE_EXT)


def export_view_to_png(view, filepath: str, bg_color: str = "#111816",
                       watermark_text: str = "", watermark_opacity: float = 0.35) -> bool:
    import perf
    with perf.timed("export_view_to_png"):
        try:
            image = view.export_to_image(
                bg_color=bg_color,
                watermark_text=watermark_text,
                watermark_opacity=watermark_opacity,
            )
            if image is None:
                log.error("export_view_to_png: export_to_image returned None")
                return False
            ok = image.save(filepath, "PNG")
            if ok: log.info("Exported PNG → %s", filepath)
            else:  log.error("image.save failed for %s", filepath)
            return ok
        except Exception as e:
            log.error("export_view_to_png: %s", e)
            return False


# ── Custom sidebar sort order ─────────────────────────────────────────────────

def _order_file() -> str:
    return os.path.join(get_data_dir(), "sidebar_order.json")


def save_sidebar_order(ordered_paths: List[str]) -> None:
    try:
        names = [os.path.basename(p) for p in ordered_paths]
        from fileio import atomic_write_json
        atomic_write_json(_order_file(), names)
    except Exception as e:
        log.warning("save_sidebar_order: %s", e)


def load_sidebar_order() -> List[str]:
    try:
        with open(_order_file(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def apply_sidebar_order(paths: List[str], order: List[str]) -> List[str]:
    if not order:
        return paths
    order_map = {name: i for i, name in enumerate(order)}
    def key(p):
        return order_map.get(os.path.basename(p), len(order))
    return sorted(paths, key=key)
