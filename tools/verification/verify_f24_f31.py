import base64, json, os, sys, tempfile
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
app = QApplication([])
import database, plant_db, lineage_images
from models import PlantNode, LineageTree

src = tempfile.mkdtemp(); database.set_data_dir(src); plant_db.reset_cache()
img = base64.b64encode(os.urandom(400)).decode()
thumb = base64.b64encode(os.urandom(120)).decode()
node = PlantNode(name="F24 plant", images=[img], thumb=thumb, photo_count=1)
plant_db.upsert(node)

plants = plant_db.get_all()
assert len(plants) == 1 and plants[0].thumb is None
assert plant_db.get_thumb(node.id) == thumb
conn = plant_db._ensure_conn()
raw = json.loads(conn.execute("SELECT meta_json FROM plants WHERE id=?",
                              (node.id,)).fetchone()[0])
assert "thumb" not in raw and "images" not in raw
stored = conn.execute("SELECT data FROM thumbs WHERE plant_id=?",
                      (node.id,)).fetchone()[0]
assert isinstance(stored, bytes) and not stored.startswith(b"data:")
print("F24 thumbnail is outside meta_json; lazy get_thumb works; stored as BLOB")

# Batch lookup should return real and missing IDs without one query per card.
n2 = PlantNode(name="F24 batch", thumb=thumb, photo_count=1)
plant_db.upsert(n2)
batch = plant_db.get_thumbs([node.id, n2.id, "missing-id"])
assert batch[node.id] == thumb and batch[n2.id] == thumb
assert batch["missing-id"] is None
plant_db.remove(n2.id)
print("F24 batch get_thumbs returns present and missing IDs")

# A metadata-only autosave must not wipe the thumbnail or full-res images.
plants[0].name = "F24 plant renamed"; plant_db.upsert(plants[0])
assert plant_db.get_thumb(node.id) == thumb and plant_db.get_images(node.id) == [img]
# An authoritative edit that removes all images clears the orphan thumb.
plants[0].images = []; plants[0].thumb = None
plant_db._img_store[node.id] = []
plant_db.upsert(plants[0])
assert plant_db.get_thumb(node.id) is None
assert plant_db.get_all()[0].photo_count == 0
print("F24 autosave preserves data; explicit image removal clears thumb")

# F31: old JSON sidecar migrates once, then per-node reads and touched-node writes.
plf = os.path.join(src, "Tree.plf"); open(plf, "w").write("{}")
legacy = {"a": [img], "b": [thumb]}
open(plf + ".images", "w").write(json.dumps(legacy))
assert lineage_images.get_node_images(plf, "a") == [img]
assert os.path.isfile(plf + ".images.sqlite3")
assert not os.path.isfile(plf + ".images")
assert os.path.isfile(plf + ".images.migrated")
new = base64.b64encode(os.urandom(400)).decode()
lineage_images.merge_and_save_snapshot(plf, {"a": [new]}, {"a", "b"})
assert lineage_images.get_node_images(plf, "a") == [new]
assert lineage_images.get_node_images(plf, "b") == [thumb]
lineage_images.merge_and_save_snapshot(plf, {}, {"b"})
assert lineage_images.load_all_images(plf) == {"b": [thumb]}
print("F31 legacy JSON migrated; one-node write leaves sibling rows intact")

# Read handles are reused rather than reopened for every lazy image load.
conn_plf = os.path.join(src, "Conn.plf")
lineage_images.save_all_images(conn_plf, {"c": [img]})
assert lineage_images.get_node_images(conn_plf, "c") == [img]
conn_path = lineage_images.images_filepath(conn_plf)
assert conn_path in lineage_images._read_conns
conn_id = id(lineage_images._read_conns[conn_path])
assert lineage_images.get_node_images(conn_plf, "c") == [img]
assert id(lineage_images._read_conns[conn_path]) == conn_id
lineage_images.delete_sidecar(conn_plf)
assert conn_path not in lineage_images._read_conns
assert not os.path.isfile(conn_path)
print("F31 read connection reused; delete_sidecar closes cached handles")

# Sidecar-first saves: an image merge failure must leave the old document
# untouched, and a successful save must merge images before the .plf.
import fileio
order_plf = os.path.join(src, "Order.plf")
order_plf2 = os.path.join(src, "Order2.plf")
fail_plf = os.path.join(src, "Fail.plf")
fail_plf2 = os.path.join(src, "Fail2.plf")
order_tree = LineageTree(name="Order")
on = PlantNode(name="Order node", images=[img], photo_count=1)
order_tree.nodes[on.id] = on
events = []
orig_merge_snapshot = lineage_images.merge_and_save_snapshot
orig_merge = lineage_images.merge_and_save
orig_write = fileio.atomic_write_json

def _merge(*a, **k):
    events.append("merge")
    return orig_merge_snapshot(*a, **k)

def _write(*a, **k):
    events.append("write")
    return orig_write(*a, **k)

try:
    lineage_images.merge_and_save_snapshot = _merge
    fileio.atomic_write_json = _write
    order_tree._image_loaded_ids.add(on.id)
    order_tree.nodes[on.id].images = [img]
    assert database.save_lineage(order_tree, order_plf)
    assert events[:2] == ["merge", "write"], events

    order_tree._image_loaded_ids.add(on.id)
    order_tree.nodes[on.id].images = [img]
    prepared = database.prepare_save(order_tree)
    events.clear()
    assert database.write_prepared_save(prepared, order_plf2)
    assert events[:2] == ["merge", "write"], events

    open(fail_plf, "w", encoding="utf-8").write("SENTINEL")
    order_tree._image_loaded_ids.add(on.id)
    order_tree.nodes[on.id].images = [img]
    lineage_images.merge_and_save = lambda *a, **k: False
    assert not database.save_lineage(order_tree, fail_plf)
    assert open(fail_plf, "r", encoding="utf-8").read() == "SENTINEL"

    open(fail_plf2, "w", encoding="utf-8").write("SENTINEL")
    order_tree._image_loaded_ids.add(on.id)
    order_tree.nodes[on.id].images = [img]
    prepared = database.prepare_save(order_tree)
    lineage_images.merge_and_save_snapshot = lambda *a, **k: False
    assert not database.write_prepared_save(prepared, fail_plf2)
    assert open(fail_plf2, "r", encoding="utf-8").read() == "SENTINEL"
finally:
    lineage_images.merge_and_save = orig_merge
    lineage_images.merge_and_save_snapshot = orig_merge_snapshot
    fileio.atomic_write_json = orig_write
print("F31 sidecar merges before .plf; merge failure leaves document intact")

# Restore the thumbnail/images before testing the bundle round-trip.
node.images = [img]; node.thumb = thumb; node.photo_count = 1
plant_db.upsert(node)

# Bundle export uses portable JSON, import re-creates the SQLite store.
t = LineageTree(name="Tree"); n = PlantNode(name="Lin", images=[img], photo_count=1)
t.nodes[n.id] = n; t._image_loaded_ids.add(n.id)
database.save_lineage(t, plf)
import data_bundle
bundle = os.path.join(src, "all.bcbundle")
assert data_bundle.export_bundle(bundle, [plf], include_plant_db=True)
dest = tempfile.mkdtemp(); database.set_data_dir(dest); plant_db.reset_cache()
res = data_bundle.import_bundle(bundle)
assert res.ok and res.plants_added == 1 and res.lineages_added == 1
assert plant_db.get_thumb(node.id) == thumb
assert plant_db.get_images(node.id) == [img]
import glob
paths = glob.glob(os.path.join(dest, "*.plf"))
assert len(paths) == 1 and lineage_images.get_node_images(paths[0], n.id) == [img]
print("F24/F31 bundle round-trip preserves thumbnails and full-res images")
os._exit(0)
