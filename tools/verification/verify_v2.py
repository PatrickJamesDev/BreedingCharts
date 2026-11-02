import os,sys,time,random,tempfile
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import *; from PyQt6.QtGui import *; from PyQt6.QtCore import *
app=QApplication([])
import database; database._data_dir=tempfile.mkdtemp()
from models import PlantNode, LineageTree
from canvas_view import LineageScene, LineageView
sc=LineageScene(); v=LineageView(sc); v.resize(1600,1000); v.show()
tr=LineageTree(name="p"); ns=[]
for i in range(300):
    n=PlantNode(name=f"Plant number {i}",species="Rosa gallica",cultivar="Cardinal",year=2000+i%20,tags=["red"]); n.x=(i%20)*260; n.y=(i//20)*340; tr.nodes[n.id]=n; ns.append(n)
for i in range(20,300): ns[i].parent_ids=[ns[i-20].id,ns[i-19].id]; ns[i].parent_roles={ns[i-20].id:"mother",ns[i-19].id:"father"}
sc.load_tree(tr); app.processEvents()
def frame():
    im=QImage(1600,1000,QImage.Format.Format_ARGB32_Premultiplied); im.fill(0xff000000); p=QPainter(im); v.render(p); p.end(); return im
def T(f,n=6):
    f(); b=1e9
    for _ in range(n):
        t=time.perf_counter(); f(); b=min(b,time.perf_counter()-t)
    return b*1000
res={}
for z in (1.0,0.3,0.05):
    v.resetTransform(); v.scale(z,z); v.centerOn(2500,2500); app.processEvents()
    res[z]=T(frame)
print("frame ms:", {k:round(x,1) for k,x in res.items()})
# scroll step
v.resetTransform(); v.scale(0.05,0.05); v.centerOn(2500,2500); app.processEvents()
def scroll():
    v.horizontalScrollBar().setValue(v.horizontalScrollBar().value()+3); v.viewport().repaint()
print("scroll step @0.05: %.0f ms"%T(scroll,8))
# visual equality vs uncached at zoom 1
v.resetTransform(); v.scale(1,1); v.centerOn(2500,2500); app.processEvents()
a=frame()
for it in sc._node_items.values(): it.setCacheMode(QGraphicsItem.CacheMode.NoCache)
u=frame()
for it in sc._node_items.values(): it.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
c=frame(); c=frame()
import itertools
d=[ (x,y) for y in range(0,1000,3) for x in range(0,1600,3) if u.pixel(x,y)!=c.pixel(x,y)]
mx=max((max(abs(QColor(u.pixel(x,y)).red()-QColor(c.pixel(x,y)).red()),abs(QColor(u.pixel(x,y)).green()-QColor(c.pixel(x,y)).green())) for x,y in d), default=0)
print("uncached vs cached: differing sampled px", len(d), "max channel delta", mx)
for it in sc._node_items.values(): it.setCacheMode(QGraphicsItem.CacheMode.NoCache)
b=frame()
diff=sum(1 for y in range(0,1000,7) for x in range(0,1600,7) if a.pixel(x,y)!=b.pixel(x,y))
print("sampled pixels differing cached vs uncached:", diff, "of", (1000//7+1)*(1600//7+1))
os._exit(0)
