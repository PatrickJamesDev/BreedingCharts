import os, sys, time
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPixmap
app = QApplication([])

from models import PlantNode, LineageTree
from canvas_view import LineageScene

tr = LineageTree(name="Undo")
a = PlantNode(name="A", x=0, y=0)
b = PlantNode(name="B", x=200, y=100)
c = PlantNode(name="C", x=400, y=200)
c.parent_ids = [a.id, b.id]; c.parent_roles = {a.id: "mother", b.id: "father"}
for n in (a, b, c):
    tr.nodes[n.id] = n
sc = LineageScene(); sc.load_tree(tr)
item_a = sc._node_items[a.id]
item_a._pixmap = QPixmap(4, 4); item_a._pixmap_dirty = False

# drag/undo: only positions differ -> same item, pixmap retained
sc._on_node_move_start(a.id)
assert "positions" in sc._undo_stack[-1] and "tree" not in sc._undo_stack[-1]
a.x, a.y = 777.0, 333.0
item_a.setPos(a.x, a.y)
sc.undo()
assert sc._node_items[a.id] is item_a, "drag undo recreated the card"
restored_a = sc.tree.nodes[a.id]
assert (restored_a.x, restored_a.y) == (0.0, 0.0), (restored_a.x, restored_a.y)
assert item_a._pixmap is not None and not item_a._pixmap_dirty, "drag undo dropped pixmap"
print("undo drag: lightweight snapshot, same item, position restored, pixmap retained")

# edit/undo: same item, name restored
before = sc.tree.to_dict()
sc._push_undo()
live_a = sc.tree.nodes[a.id]
live_a.name = "A renamed"
sc.update_node(live_a, record_undo=False)
assert sc.tree.nodes[a.id].name == "A renamed"
sc.undo()
assert sc.tree.to_dict() == before
assert sc._node_items[a.id] is item_a
print("undo edit: same item, name restored")

# add/undo: added node disappears
d = PlantNode(name="D", x=600, y=0)
sc.add_node(d)
assert d.id in sc._node_items
sc.undo()
assert d.id not in sc.tree.nodes and d.id not in sc._node_items
print("undo add: node removed")

# delete/undo: node and its edges return
before = sc.tree.to_dict()
sc._node_items[b.id].setSelected(True)
sc.delete_selected()
assert b.id not in sc.tree.nodes
sc.undo()
after = sc.tree.to_dict()
if after != before:
    for k in before:
        if before[k] != after.get(k):
            print("DIFF", k, repr(before[k])[:300], repr(after.get(k))[:300])
assert after == before, "delete undo did not restore the tree"
assert b.id in sc._node_items
assert any(e.child_item.node.id == c.id and e.parent_item.node.id in (a.id, b.id)
           for e in sc._edges)
print("undo delete: tree, card and edges restored")
os._exit(0)
