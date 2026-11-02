import os,sys,time,random,tempfile,base64
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QEventLoop, QTimer
app=QApplication([])
import database, plant_db
TMP=tempfile.mkdtemp(); database._data_dir=TMP; plant_db.reset_cache()
from models import PlantNode, LineageTree
import settings as S; S.save_settings=lambda *a,**k:None
from settings import AppSettings, build_stylesheet
import ui_main, lineage_images
ui_main.save_settings=lambda *a,**k:None
def settle(ms):
    l=QEventLoop(); QTimer.singleShot(ms,l.quit); l.exec()
s=AppSettings(); s.tour_shown=True; s.last_open_filepath=""; s.auto_layout_on_open=False
win=ui_main.MainWindow(s); win.show(); settle(200)
tree=LineageTree(name="F2"); n=PlantNode(name="a"); tree.nodes[n.id]=n
fp=os.path.join(TMP,"F2.plf"); database.save_lineage(tree,fp)
t2=database.load_lineage(fp); win._load_tree(t2,filepath=fp); win._unsaved=False
node=list(win._tree.nodes.values())[0]
lineage_images.populate_node_images(win._tree,fp,node.id)
photo=base64.b64encode(os.urandom(4000)).decode(); node.images=[photo]; node.photo_count=1
lineage_images.mark_images_authoritative(win._tree,node.id); win._unsaved=True
real=ui_main.write_prepared_save
ui_main.write_prepared_save=lambda p,path: False
win._debounce_save(); settle(300)
print("after FAILED write: node.images restored in RAM:", len(node.images), "| loaded_ids:", len(win._tree._image_loaded_ids), "| unsaved:", win._unsaved)
ui_main.write_prepared_save=real
win._debounce_save(); settle(400)
on_disk=lineage_images.load_all_images(fp)
print("after retry: photo on disk:", on_disk.get(node.id)==[photo])
os._exit(0)
