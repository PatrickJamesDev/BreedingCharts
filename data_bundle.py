"""
data_bundle.py — Export/Import a portable .bcbundle (a zip file under the
hood) containing lineages and/or the global plant database, for sharing
data between two separate BreedingCharts installs (e.g. two breeders on
different computers).

Merge semantics on import are intentionally *additive only* — nothing
already on the local machine is ever overwritten or removed:

  • Plant database entries merge by id (plant_db.import_from_dict): an
    incoming plant whose id already exists locally is left untouched;
    every other incoming plant is added. Ids are UUID4s minted once at
    creation time (see models.PlantNode), so two independently-created
    plants will never collide — "already exists locally" reliably means
    "this exact plant came from here before" (e.g. re-importing the same
    bundle twice, or a plant that already arrived via an earlier import).

  • Lineages merge by LineageTree.id, not filename: an incoming lineage
    whose id already exists locally is skipped entirely (it's the same
    lineage, already here). Every other incoming lineage is written out
    as a brand-new .plf file (+ its image sidecar, if it has one),
    using a filename guaranteed not to collide with anything already in
    the data folder — so a friend's "My Lineage" and your own "My
    Lineage" both end up present side by side rather than one clobbering
    the other.
"""
from __future__ import annotations

import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

log = logging.getLogger(__name__)

BUNDLE_FORMAT   = "breedingcharts-bundle"
BUNDLE_VERSION  = 1
BUNDLE_EXT      = ".bcbundle"


# ── Export ───────────────────────────────────────────────────────────────────

def export_bundle(filepath: str, lineage_paths: List[str],
                   include_plant_db: bool = True) -> bool:
    """Write a bundle containing the given .plf files (with their image
    sidecars, if any) and, optionally, the entire global plant database."""
    import plant_db
    try:
        manifest = {
            "format":         BUNDLE_FORMAT,
            "format_version": BUNDLE_VERSION,
            "exported_at":    datetime.now().isoformat(),
            "lineages":       [],
            "includes_plant_db": include_plant_db,
        }
        with zipfile.ZipFile(filepath, "w", zipfile.ZIP_DEFLATED) as zf:
            for plf_path in lineage_paths:
                if not os.path.isfile(plf_path):
                    continue
                base = os.path.basename(plf_path)
                zf.write(plf_path, arcname=f"lineages/{base}")
                # Bundles keep the portable JSON image format so older
                # BreedingCharts builds (and this one) can import them.  The
                # local store itself is SQLite; only this explicit export
                # loads every image.
                import lineage_images
                sidecar_imgs = lineage_images.load_all_images(plf_path)
                if sidecar_imgs:
                    zf.writestr(f"lineages/{base}.images",
                                json.dumps(sidecar_imgs, ensure_ascii=False))
                manifest["lineages"].append(base)

            if include_plant_db:
                pdb = plant_db.export_all_for_bundle()
                zf.writestr("plant_database.json",
                             json.dumps(pdb, ensure_ascii=False))

            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        log.info("data_bundle: exported %d lineage(s)%s → %s",
                  len(manifest["lineages"]),
                  " + plant database" if include_plant_db else "",
                  filepath)
        return True
    except Exception as e:
        log.error("export_bundle: %s", e)
        return False


# ── Import ───────────────────────────────────────────────────────────────────

@dataclass
class BundleImportResult:
    plants_added:      int             = 0
    lineages_added:    int             = 0
    lineages_skipped:  int             = 0   # already present locally (same id)
    added_lineage_names: List[str]     = field(default_factory=list)
    error:             Optional[str]   = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _local_lineage_ids(data_dir: str) -> set:
    """Read just the `id` field of every local .plf file (cheap relative
    to a full parse, but this only runs once per import, never on a hot
    path)."""
    import database as _db
    ids = set()
    for path in _db.get_lineage_files(data_dir):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            tid = d.get("id")
            if tid:
                ids.add(tid)
        except Exception as e:
            log.warning("data_bundle: could not read id from %s: %s", path, e)
    return ids


def _unique_path(path: str) -> str:
    """Return `path` if free, otherwise the first `_2`, `_3`, ... suffix
    (before the extension) that is."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        candidate = f"{base}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def import_bundle(filepath: str) -> BundleImportResult:
    """Extract + merge a bundle into the current data folder. Nothing
    already present locally is ever overwritten — see module docstring."""
    import plant_db
    import database as _db

    result = BundleImportResult()
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                result.error = ("Not a valid BreedingCharts bundle "
                                 "(missing manifest.json).")
                return result
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

            # ── Plant database ──────────────────────────────────────────
            if "plant_database.json" in names:
                incoming = json.loads(
                    zf.read("plant_database.json").decode("utf-8"))
                result.plants_added = plant_db.import_from_dict(incoming)

            # ── Lineages ─────────────────────────────────────────────────
            data_dir = _db.get_data_dir()
            existing_ids = _local_lineage_ids(data_dir)
            for lf_name in manifest.get("lineages", []):
                arc = f"lineages/{lf_name}"
                if arc not in names:
                    continue
                try:
                    tree_dict = json.loads(zf.read(arc).decode("utf-8"))
                except Exception as e:
                    log.warning("data_bundle: skipping unreadable %s: %s", arc, e)
                    result.lineages_skipped += 1
                    continue

                tree_id = tree_dict.get("id")
                if tree_id and tree_id in existing_ids:
                    result.lineages_skipped += 1
                    continue

                dest_path = _db.default_filepath(
                    tree_dict.get("name", "Imported Lineage"), directory=data_dir)
                dest_path = _unique_path(dest_path)
                from fileio import atomic_write_json
                atomic_write_json(dest_path, tree_dict)
                _db.write_lineage_meta(dest_path, tree_dict)

                sidecar_arc = arc + ".images"
                if sidecar_arc in names:
                    try:
                        incoming_imgs = json.loads(
                            zf.read(sidecar_arc).decode("utf-8"))
                        if isinstance(incoming_imgs, dict):
                            import lineage_images
                            if not lineage_images.save_all_images(
                                    dest_path, incoming_imgs):
                                log.warning("data_bundle: image sidecar write "
                                            "failed for %s", lf_name)
                    except Exception as e:
                        log.warning("data_bundle: could not import images for "
                                    "%s: %s", lf_name, e)

                if tree_id:
                    existing_ids.add(tree_id)
                result.lineages_added += 1
                result.added_lineage_names.append(tree_dict.get("name", lf_name))

        log.info("data_bundle: imported %d plant(s), %d lineage(s) "
                  "(%d already present, skipped) from %s",
                  result.plants_added, result.lineages_added,
                  result.lineages_skipped, filepath)
        return result

    except Exception as e:
        log.error("import_bundle: %s", e)
        result.error = str(e)
        return result
