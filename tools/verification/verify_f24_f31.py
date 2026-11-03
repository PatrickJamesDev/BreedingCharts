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
