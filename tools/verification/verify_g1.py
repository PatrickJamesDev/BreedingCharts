import os,sys,time,random,base64
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import *; from PyQt6.QtCore import *
app=QApplication([])
import image_utils
img=QImage(4000,3000,QImage.Format.Format_RGB32); p=QPainter(img); random.seed(1)
for i in range(300):
    p.setBrush(QColor(random.randrange(256),random.randrange(256),random.randrange(256))); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(random.randrange(4000),random.randrange(3000),random.randrange(50,900),random.randrange(50,900))
p.end(); n=QImage(4000,3000,QImage.Format.Format_RGB32); ptr=n.bits(); ptr.setsize(n.sizeInBytes()); ptr[:]=os.urandom(n.sizeInBytes())
p=QPainter(img); p.setOpacity(0.1); p.drawImage(0,0,n); p.end()
ba=QByteArray(); b=QBuffer(ba); b.open(QIODevice.OpenModeFlag.WriteOnly); img.save(b,"JPEG",90); b.close(); raw=bytes(ba)
def T(f,n=4):
    best=1e9
    for _ in range(n):
        t=time.perf_counter(); r=f(); best=min(best,time.perf_counter()-t)
    return best*1000,r
a,t1=T(lambda: image_utils.make_thumb_from_bytes(raw))
q1=QImage(); q1.loadFromData(base64.b64decode(t1))
print(f"make_thumb_from_bytes 12MP: {a:.0f} ms -> {q1.width()}x{q1.height()}, {len(t1)/1024:.0f} KB b64")
# reference via old path
def old():
    q=QImage(); q.loadFromData(raw); q=image_utils._flatten_and_scale(q,1200); return image_utils._encode_jpeg_b64(q,85)
b_,t0=T(old); q0=QImage(); q0.loadFromData(base64.b64decode(t0))
print(f"old path: {b_:.0f} ms -> {q0.width()}x{q0.height()}, {len(t0)/1024:.0f} KB b64")
# similarity
q0s=q0.convertToFormat(QImage.Format.Format_RGB32); q1s=q1.convertToFormat(QImage.Format.Format_RGB32)
same_size=(q0s.size()==q1s.size()); tot=0; cnt=0
if same_size:
    for y in range(0,q0s.height(),9):
        for x in range(0,q0s.width(),9):
            c0=QColor(q0s.pixel(x,y)); c1=QColor(q1s.pixel(x,y)); tot+=abs(c0.red()-c1.red())+abs(c0.green()-c1.green())+abs(c0.blue()-c1.blue()); cnt+=3
print("same dimensions:", same_size, "| mean abs channel difference (0-255): %.2f"%(tot/max(cnt,1)))
# photo format
shot=img.scaled(2560,1440,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.FastTransformation)
print("photo_format(opaque photo):", image_utils.photo_format(shot))
argb=shot.convertToFormat(QImage.Format.Format_ARGB32); print("photo_format(ARGB but opaque):", image_utils.photo_format(argb))
argb.setPixelColor(5,5,QColor(0,0,0,0)); 
tr=QImage(100,100,QImage.Format.Format_ARGB32); tr.fill(Qt.GlobalColor.transparent); print("photo_format(transparent):", image_utils.photo_format(tr))
os._exit(0)
