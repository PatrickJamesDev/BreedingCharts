import os,sys,time,random,tempfile
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import *; from PyQt6.QtCore import *
app=QApplication([])
import database; database._data_dir=tempfile.mkdtemp()
from models import PlantNode, LineageTree
from canvas_view import LineageScene, LineageView, EdgeItem
exec(open('/tmp/c2.py').read().split("sc=LineageScene()")[0].split("app=QApplication([])")[1].split("from canvas_view")[0]) if False else None
random.seed(5)
def mk(N):
    tr=LineageTree(name="s"); ns=[]
    for i in range(N):
        n=PlantNode(name=f"P{i}",year=2000+i%25); n.x=random.uniform(-3000,3000); n.y=random.uniform(-3000,3000); tr.nodes[n.id]=n; ns.append(n)
    for i in range(3,N):
        a,b=random.sample(ns[max(0,i-60):i],2); ns[i].parent_ids=[a.id,b.id]; ns[i].parent_roles={a.id:"mother",b.id:"father"}
    return tr,ns
sc=LineageScene(); v=LineageView(sc); v.resize(1600,1000); v.show()
tr,ns=mk(3000); sc.load_tree(tr); app.processEvents()
for label in ("add child with 2 parents","add node with no parents"):
    n=PlantNode(name="new",year=2010); n.x=0;n.y=0
    if "2 parents" in label: n.parent_ids=[ns[10].id,ns[11].id]; n.parent_roles={ns[10].id:"mother",ns[11].id:"father"}
    t=time.perf_counter(); sc.add_node(n); dt=(time.perf_counter()-t)*1000
    print(f"add_node ({label}) @3000 nodes: {dt:.1f} ms")
sig=lambda: sorted((e._src.node.id if hasattr(e,'_src') else id(e)) and 1 for e in sc.items() if isinstance(e,EdgeItem))
e1=len([e for e in sc.items() if isinstance(e,EdgeItem)]); sc._rebuild_edges(); e2=len([e for e in sc.items() if isinstance(e,EdgeItem)])
print("edge count after incremental add == after full rebuild:", e1==e2, e1, e2)
os._exit(0)
