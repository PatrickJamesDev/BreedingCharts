import os, sys, tempfile, time
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QPointF
app = QApplication([])

import database
database._data_dir = tempfile.mkdtemp()
from models import PlantNode, LineageTree
from canvas_view import LineageScene, EdgeItem

# F30: metadata sidecar
tr = LineageTree(name="Meta Test", category="Test", description="d")
for i in range(5):
    n = PlantNode(name=f"P{i}", year=2020 + i)
    n.x = i * 300; n.y = 0
    tr.nodes[n.id] = n
path = os.path.join(database.get_data_dir(), "meta.plf")
assert database.save_lineage(tr, path)
assert os.path.isfile(path + ".meta"), "meta sidecar missing"
database._meta_cache.clear()
meta = database.get_lineage_meta(path)
assert meta["name"] == "Meta Test" and meta["node_count"] == 5

with open(path, "w", encoding="utf-8") as fh:
    fh.write('{"name":"Externally Edited","modified_at":"2030-01-02","category":"","sort_index":0,"nodes":{}}')
newer = time.time() + 5
os.utime(path, (newer, newer))
database._meta_cache.clear()
meta = database.get_lineage_meta(path)
assert meta["name"] == "Externally Edited" and meta["node_count"] == 0
print("F30 meta sidecar + stale fallback: OK")

# F42: incremental edge add/remove must equal a full rebuild
sc = LineageScene()
tr = LineageTree(name="Edges")
a = PlantNode(name="A"); b = PlantNode(name="B"); c = PlantNode(name="C")
d = PlantNode(name="D")
for n, x in ((a, 0), (b, 300), (c, 150), (d, 450)):
    n.x = x; n.y = 0; tr.nodes[n.id] = n
c.parent_ids = [a.id, b.id]; c.parent_roles = {a.id: "mother", b.id: "father"}
sc.load_tree(tr)

def sig():
    return sorted((e.parent_item.node.name, e.child_item.node.name,
                   round(e.path().boundingRect().x(), 1),
                   round(e.path().boundingRect().y(), 1))
                  for e in sc.items() if isinstance(e, EdgeItem))

def check(label):
    before = sig()
    sc._rebuild_edges()
    after = sig()
    assert before == after, (label, before, after)
    print(f"{label}: {len(before)} edges match rebuild")

sc.apply_connection(a.id, d.id)
check("incremental connect")
sc.remove_connection(a.id, d.id)
check("incremental disconnect")
sc.remove_node(d.id)
check("incremental remove_node")
print("F42 incremental edge operations: OK")

# F12: a parent's two edges follow each child's role, even if the parent is
# mother for one cross and father for another.
sc2 = LineageScene()
sc2.color_code_lines = True
sc2.mother_line_color = "#ff0000"
sc2.father_line_color = "#0000ff"
tr2 = LineageTree(name="Roles")
pnode = PlantNode(name="Parent")
c1 = PlantNode(name="Child1"); c2 = PlantNode(name="Child2")
c1.parent_ids = [pnode.id]; c1.parent_roles = {pnode.id: "mother"}
c2.parent_ids = [pnode.id]; c2.parent_roles = {pnode.id: "father"}
for n in (pnode, c1, c2): tr2.nodes[n.id] = n
sc2.load_tree(tr2)
pnode.color = "#123456"
sc2.update_node(pnode)
colors = {e.child_item.node.name: e._color for e in sc2._edges}
assert colors["Child1"] == "#ff0000" and colors["Child2"] == "#0000ff", colors
print("F12 per-edge parent role colours: OK")
os._exit(0)
