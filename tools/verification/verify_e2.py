import os,sys,time,tempfile
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QEventLoop, QTimer
app=QApplication([])
import database, plant_db
database._data_dir=tempfile.mkdtemp(); plant_db.reset_cache()
from models import PlantNode
from settings import THEMES, build_stylesheet, AppSettings
app.setStyleSheet(build_stylesheet(AppSettings()))
import grid_view
if not hasattr(grid_view,"_CARDS_FIRST_PAINT"): grid_view._CARDS_FIRST_PAINT=40; grid_view._CARDS_PER_TICK=40
def settle(ms):
    l=QEventLoop(); QTimer.singleShot(ms,l.quit); l.exec()
N=1500
for i in range(N): plant_db.upsert(PlantNode(name=f"Plant {i:04d}",species=f"Sp{i%30} alba"))
g=grid_view.PlantGridView(palette=THEMES["dark"]); g.resize(1400,900); g.show(); settle(100)
g._refresh()
while g._card_build_timer.isActive(): app.processEvents()
g._search.setText("Plant 000"); g._rebuild_current_view()
while g._card_build_timer.isActive(): app.processEvents()
shown=len(g._card_cache); g._select_all()
print(f"search shows {shown}; Ctrl+A selects {len(g._selected_plants)} (expected {shown})")
g._search.clear(); g._rebuild_current_view()
while g._card_build_timer.isActive(): app.processEvents()
t=time.perf_counter(); g._select_all(); print(f"select_all {len(g._card_cache)} cards: {(time.perf_counter()-t)*1000:.1f} ms")
# resize with unchanged column count
calls=[0]; orig=g._rebuild_current_view
def counting(): calls[0]+=1; orig()
g._rebuild_current_view=counting
g._filter_timer.timeout.disconnect(); g._filter_timer.timeout.connect(counting)
w=g.width()
for dw in (4,8,12,6):
    g.resize(w+dw,900); settle(120)
print("rebuilds triggered by 4 small resizes (same column count):", calls[0])
g.resize(w+400,900); settle(150); print("rebuilds after a resize that changes columns:", calls[0])
# theme change keeps thumbnail cache
from PyQt6.QtGui import QPixmapCache, QPixmap
QPixmapCache.insert("probe",QPixmap(10,10)); g.apply_card_settings(palette=THEMES["light"] if "light" in THEMES else THEMES["dark"], font="Georgia", font_size=9)
print("thumbnail cache survives theme change:", QPixmapCache.find("probe") is not None)
os._exit(0)
