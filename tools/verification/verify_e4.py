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
t=time.perf_counter(); g._select_all(); app.processEvents(); a=(time.perf_counter()-t)*1000
t=time.perf_counter(); g._deselect_all(); app.processEvents(); b=(time.perf_counter()-t)*1000
print(f"clean-state Ctrl+A on {len(g._card_cache)} cards: {a:.0f} ms | deselect all: {b:.0f} ms")
os._exit(0)
