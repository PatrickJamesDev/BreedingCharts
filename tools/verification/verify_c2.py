import os,sys,time,random,tempfile
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import *; from PyQt6.QtGui import *; from PyQt6.QtCore import *
app=QApplication([])
import database; database._data_dir=tempfile.mkdtemp()
from models import PlantNode, LineageTree
from canvas_view import LineageScene, LineageView
def mk(N):
    random.seed(5); tr=LineageTree(name="s"); ns=[]
    for i in range(N):
        n=PlantNode(name=f"Plant {i}",species=f"Sp{i%40}",year=2000+i%25,tags=["a"]); n.x=random.uniform(-3000,3000); n.y=random.uniform(-3000,3000); tr.nodes[n.id]=n; ns.append(n)
    for i in range(3,N):
        a,b=random.sample(ns[max(0,i-60):i],2); ns[i].parent_ids=[a.id,b.id]; ns[i].parent_roles={a.id:"mother",b.id:"father"}
    return tr
sc=LineageScene(); v=LineageView(sc); v.resize(1600,1000); v.show()
for N in (1000,3000):
    tr=mk(N)
    b=1e9
    for _ in range(3):
        t=time.perf_counter(); sc.load_tree(tr); b=min(b,time.perf_counter()-t)
    t=time.perf_counter(); sc.auto_layout(); al=time.perf_counter()-t
    t=time.perf_counter(); sc._do_auto_organize(); ao=time.perf_counter()-t
    # edge consistency: every edge endpoints equal ports of its nodes
    bad=0
    for lst in sc._node_edge_index.values():
        for e in lst:
            pass
    print(f"N={N}: load_tree {b*1000:.0f} ms | auto_layout {al*1000:.0f} ms | auto_organize {ao*1000:.0f} ms")
# correctness: edge geometry after layout equals fresh rebuild
from canvas_view import EdgeItem
def sig():
    return sorted((round(e.path().boundingRect().x(),1),round(e.path().boundingRect().y(),1)) for e in sc.items() if isinstance(e,EdgeItem))
s1=sig(); sc._rebuild_edges(); s2=sig(); print("edge paths after bulk layout == fresh rebuild:", s1==s2)
os._exit(0)
