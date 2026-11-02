import os,sys,time,random,tempfile,base64
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import *; from PyQt6.QtGui import *; from PyQt6.QtCore import *
app=QApplication([])
import database; database._data_dir=tempfile.mkdtemp()
import plant_db, notes_store
from models import PlantNode, LineageTree, Note
from canvas_view import LineageScene, LineageView
# F6 year labels
sc=LineageScene(); v=LineageView(sc); v.resize(900,600); v.show()
t=LineageTree(name="Y"); a=PlantNode(name="a",year=2020); a.y=0; b=PlantNode(name="b",year=2021); b.y=340
t.nodes[a.id]=a; t.nodes[b.id]=b; sc.load_tree(t); app.processEvents()
pm=QPixmap(900,600); p=QPainter(pm); v.render(p); p.end()
a.y=700; a.year=1999; sc._update_year_rows()
fresh = v._sorted_year_rows(sc.year_rows)==sorted(sc.year_rows.items())
print("F6 year labels fresh after change:", fresh)
# F8
t2=LineageTree(name="G"); c=PlantNode(name="c"); c.parent_ids=["ghost"]; t2.nodes[c.id]=c
print("F8 get_generation dangling parent ->", t2.get_generation(c.id))
# F9
d1=tempfile.mkdtemp(); d2=tempfile.mkdtemp()
database._data_dir=d1; notes_store.reset_cache(); notes_store.upsert(Note(title="n1"))
database._data_dir=d2; notes_store.reset_cache()
print("F9 notes after folder switch:", [n.title for n in notes_store.get_all()])
# F1
database._data_dir=tempfile.mkdtemp(); plant_db.reset_cache()
for i in range(30): plant_db.upsert(PlantNode(name=f"P{i}"))
from grid_view import PlantGridView
from settings import THEMES
g=PlantGridView(palette=THEMES["dark"]); g.resize(1200,800)
try:
    g._refresh()
    while g._card_build_timer.isActive(): app.processEvents()
    print("F1 grid refresh OK, cards:", len(g._card_cache))
except Exception as e: print("F1 FAIL", e)
# F20 hit-test equivalence + speed
sc2=LineageScene(); v2=LineageView(sc2); v2.resize(1400,900); v2.show()
tr=LineageTree(name="x")
for i in range(1000):
    n=PlantNode(name=f"p{i}"); n.x=(i%40)*260; n.y=(i//40)*340; tr.nodes[n.id]=n
sc2.load_tree(tr); app.processEvents()
items=list(sc2._node_items.values())
ok=True; 
for it in random.sample(items,40):
    for pt in (it.top_port_scene(), it.bottom_port_scene(), QPointF(it.top_port_scene().x()+30,it.top_port_scene().y()-20)):
        r=v2._find_port_at(pt)
        if r[0] is None: ok=False
print("F20 port hit-test finds ports near cards:", ok)
t0=time.perf_counter(); [v2._find_port_at(QPointF(99999,99999)) for _ in range(20)]; print("F20 miss cost per call: %.3f ms"%((time.perf_counter()-t0)/20*1000))
os._exit(0)
