import os,sys,time,random,tempfile,json,base64
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtCore import QEventLoop, QTimer, QPointF
from PyQt6.QtGui import QImage
app=QApplication([])
import database, plant_db
TMP=tempfile.mkdtemp(); database._data_dir=TMP; plant_db.reset_cache()
from models import PlantNode, LineageTree
import settings as S
calls=[]; S.save_settings=lambda *a,**k: calls.append(time.time())
from settings import AppSettings, build_stylesheet, THEMES
import ui_main
ui_main.save_settings=lambda *a,**k: calls.append(time.time())
def settle(ms):
    l=QEventLoop(); QTimer.singleShot(ms,l.quit); l.exec()
s=AppSettings(); s.tour_shown=True; s.last_open_filepath=""; s.auto_layout_on_open=True
app.setStyleSheet(build_stylesheet(s))
win=ui_main.MainWindow(s); win.resize(1400,900); win.show(); settle(300)
# --- F7 open does not write
tree=LineageTree(name="Open me"); ns=[]
for i in range(12):
    n=PlantNode(name=f"p{i}",year=2020+i%3); n.x=random.uniform(-500,500); n.y=random.uniform(-500,500); tree.nodes[n.id]=n; ns.append(n)
fp=os.path.join(TMP,"Open_me.plf"); database.save_lineage(tree,fp); before=open(fp,"rb").read()
win._load_tree(database.load_lineage(fp),filepath=fp)
print("F7 after open: unsaved flag:",win._unsaved,"| debounce active:",win._debounce_timer.isActive(),"| undo stack depth:",len(win._scene._undo_stack))
settle(2200); print("F7 file unchanged on disk after 2.2 s:", open(fp,"rb").read()==before)
# --- F45 fit_view
win._view.fit_view(); print("F45 fit_view ok, zoom:", round(win._view.transform().m11(),3))
# --- F29 settings coalescing
calls.clear()
for t in ("dark","light","dark","light","dark"):
    if t in THEMES: win._quick_set_theme(t)
settle(900); print("F29 five theme switches -> save_settings calls:", len(calls))
# --- F15 stale decode token
it=next(iter(win._scene._node_items.values()))
it.node.thumb=None
import canvas_view
it._decode_pending=True; it._decode_gen=5; it._pixmap=None
win._scene._on_decode_done(it.node.id, QImage(10,10,QImage.Format.Format_RGB32), (id(it),4))
print("F15 stale result ignored:", it._pixmap is None and it._decode_pending)
win._scene._on_decode_done(it.node.id, QImage(10,10,QImage.Format.Format_RGB32), (id(it),5))
print("F15 current result applied:", it._pixmap is not None and not it._decode_pending)
# --- grid: F27 remove_many, F26 revision, F10 sheet parents
for i in range(60): plant_db.upsert(PlantNode(name=f"G{i:02d}",species="Rosa"))
mom=PlantNode(name="Mom"); dad=PlantNode(name="Dad"); plant_db.upsert(mom); plant_db.upsert(dad)
kid=PlantNode(name="Kid",parent_ids=[mom.id,dad.id],parent_roles={mom.id:"mother",dad.id:"father"}); plant_db.upsert(kid)
g=win._grid_view
refreshes=[0]; orig=g._refresh
def counting(): refreshes[0]+=1; orig()
g._refresh=counting
win._open_grid_view(); settle(500)
print("F26 first show refreshes:", refreshes[0])
win._show_lineage_view(); settle(100); g.mark_dirty(); win._open_grid_view(); settle(300)
print("F26 re-show with dirty flag but unchanged DB -> extra refreshes:", refreshes[0]-1)
plant_db.upsert(PlantNode(name="NewOne")); win._show_lineage_view(); settle(100); g.mark_dirty(); win._open_grid_view(); settle(300)
print("F26 after a DB change -> refreshed:", refreshes[0]-1)
g._plants_by_id={}; g._dirty=True; g._rebuild_plant_sheet(); tb=g._get_or_create_plant_sheet()
row=[r for r in range(tb.rowCount()) if tb.item(r,0).text()=="Kid"][0]
print("F10 sheet parent cells:", repr(tb.item(row,4).text()), repr(tb.item(row,5).text()))
QMessageBox.question=staticmethod(lambda *a,**k: QMessageBox.StandardButton.Yes)
rm=[0]; real=plant_db.remove_many
plant_db.remove_many=lambda ids: (rm.__setitem__(0,rm[0]+1), real(ids))[1]
g._selected_plants=[p for p in g._all_plants if p.name.startswith("G")][:25]
n0=len(plant_db.get_all()); g._delete_selected_batch(); settle(300)
print("F27 deleted 25 plants:", n0-len(plant_db.get_all()), "| remove_many calls:", rm[0])
# --- F4 save_current waits for worker
print("F4 _save_current_silently waits on running worker: present in code:", "wait(5000)" in open("ui_main.py").read())
os._exit(0)
