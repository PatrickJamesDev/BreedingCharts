"""
lineage_images.py — Lazy full-resolution image storage for lineage (.plf) files.

Mirrors the plant_db.py lazy-image architecture (metadata/images split,
evict-after-use, merge-on-save) but scoped to a single lineage file:

  <name>.plf          tree structure, node metadata, and small `thumb`
                       fields (JPEG, capped at image_utils.THUMB_MAX_DIM).
                       Always loaded eagerly by database.load_lineage() —
                       this is what card rendering uses, and it's small.

  <name>.plf.images    {node_id: [base64, ...]} full-resolution originals,
                       byte-for-byte as uploaded. Loaded ONLY when a node's
                       full images are actually needed — opening the Images
                       tab, exporting, copy/paste, adding/replacing a photo
                       — and never held for the whole session otherwise.

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
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def images_filepath(plf_path: str) -> str:
    return plf_path + ".images"


def load_all_images(plf_path: Optional[str]) -> Dict[str, List[str]]:
    """Read the whole sidecar file. Only call this when bulk access is
    genuinely needed (migration, or the merge step inside save)."""
    if not plf_path:
        return {}
    p = images_filepath(plf_path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        log.error("lineage_images: failed to read %s: %s", p, e)
        return {}


def get_node_images(plf_path: Optional[str], node_id: str) -> List[str]:
    """Lazily read just one node's full-resolution images from disk. The
    result isn't cached by this function — the caller decides whether to
    hold onto it (see populate_node_images)."""
    return list(load_all_images(plf_path).get(node_id, []))


def save_all_images(plf_path: str, images_by_node: Dict[str, List[str]]) -> bool:
    p = images_filepath(plf_path)
    try:
        cleaned = {nid: imgs for nid, imgs in images_by_node.items() if imgs}
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        from fileio import atomic_write_json
        atomic_write_json(p, cleaned)
        return True
    except Exception as e:
        log.error("lineage_images: failed to write %s: %s", p, e)
        return False


def delete_sidecar(plf_path: str) -> None:
    p = images_filepath(plf_path)
    try:
        if os.path.isfile(p):
            os.remove(p)
    except OSError as e:
        log.warning("lineage_images: could not remove %s: %s", p, e)


# ── Session-level helpers (operate on a LineageTree instance) ─────────────────

def populate_node_images(tree, plf_path: Optional[str], node_id: str) -> List[str]:
    """Make node.images authoritative for this session: load from the
    sidecar on first touch, then just return what's already there. Call
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
    """File-I/O half of the sidecar merge — operates only on plain data
    (never the live tree), so it's safe to run on a background thread."""
    on_disk = load_all_images(plf_path)
    on_disk.update(touched)
    on_disk = {nid: imgs for nid, imgs in on_disk.items()
               if nid in valid_node_ids and imgs}
    return save_all_images(plf_path, on_disk)


def merge_and_save(tree, plf_path: str) -> bool:
    """Write the images sidecar: touched nodes' current .images win, every
    other node's on-disk entry is left alone, and entries for nodes no
    longer in the tree are dropped."""
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
