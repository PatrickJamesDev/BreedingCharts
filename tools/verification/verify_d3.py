import os,sys,time,random,tempfile,json
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import *; from PyQt6.QtGui import *; from PyQt6.QtCore import *
app=QApplication([])
import database; database._data_dir=tempfile.mkdtemp()
from models import PlantNode, LineageTree
from canvas_view import LineageScene, LineageView
# --- meta from prepared doc: equality + cost
import base64
tr=LineageTree(name="Meta",category="Roses")
for i in range(300):
    n=PlantNode(name=f"p{i}"); n.thumb=base64.b64encode(os.urandom(120_000)).decode(); tr.nodes[n.id]=n
fp=os.path.join(database.get_data_dir(),"Meta.plf")
prep=database.prepare_save(tr)
database.write_prepared_save(prep,fp)
cached=database._meta_cache.get(fp)[1] if fp in database._meta_cache else None
database._meta_cache.pop(fp,None)
t=time.perf_counter(); parsed=database.get_lineage_meta(fp); dt=(time.perf_counter()-t)*1000
print("meta set at save time equals parsed meta:", cached==parsed, "| full-parse cost avoided on UI thread: %.0f ms (%.0f MB file)"%(dt,os.path.getsize(fp)/1048576))
# --- zoom suspend/resume with real wheel events
sc=LineageScene(); v=LineageView(sc); v.resize(1600,1000); v.show()
t2=LineageTree(name="p"); 
for i in range(300):
    n=PlantNode(name=f"P{i}"); n.x=(i%20)*260; n.y=(i//20)*340; t2.nodes[n.id]=n
sc.load_tree(t2); app.processEvents()
it=next(iter(sc._node_items.values()))
print("cache mode at rest:", it.cacheMode().name)
ev=QWheelEvent(QPointF(800,500),QPointF(800,500),QPoint(0,0),QPoint(0,120),Qt.MouseButton.NoButton,Qt.KeyboardModifier.ControlModifier,Qt.ScrollPhase.NoScrollPhase,False)
v.wheelEvent(ev); print("during zoom:", it.cacheMode().name)
t=time.time()
while time.time()-t<0.4: app.processEvents()
print("after 180 ms idle:", it.cacheMode().name, "| zoom factor now", round(v.transform().m11(),3))
os._exit(0)
