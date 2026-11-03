import os, sys, tempfile
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QEventLoop, QTimer
app = QApplication([])
import database, plant_db
database._data_dir = tempfile.mkdtemp(); plant_db.reset_cache()
from models import PlantNode
from settings import THEMES, build_stylesheet, AppSettings
app.setStyleSheet(build_stylesheet(AppSettings()))
import grid_view

def settle(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

N = 1500
for i in range(N):
    plant_db.upsert(PlantNode(name=f"Plant {i:04d}", species=f"Sp{i%30} alba"))
g = grid_view.PlantGridView(palette=THEMES["dark"]); g.resize(1400, 900)
g.show(); settle(100); g._refresh(); app.processEvents()
built = len(g._card_cache)
assert 0 < built < 150, built
assert len(g._visible_plants) == N
sb = g._scroll.verticalScrollBar(); assert sb.maximum() > 0
sb.setValue(sb.maximum()); app.processEvents(); settle(30)
assert 0 < len(g._card_cache) < 150
print(f"F33 virtual grid: {built} live cards for {N} plants; scroll extent {sb.maximum()} px")

# Off-screen cards must go back into the bounded spare pool and be reused by
# the next row instead of constructing new widgets on every scroll.
sb.setValue(sb.maximum() // 2)
spare_before = {id(c) for c in g._spare_cards}
assert spare_before
row = next(r for r in sorted(g._row_items) if r not in g._row_cards)
g._materialise_row(row)
assert row in g._row_cards
reused = [c for c in g._row_cards[row] if id(c) in spare_before]
assert reused and len(reused) == len(g._row_cards[row])
row_pids = {c.plant.id for cards in g._row_cards.values() for c in cards}
assert set(g._card_cache) == row_pids
assert len(g._spare_cards) <= 120
g._dematerialise_row(row); app.processEvents()
assert set(g._card_cache) == {c.plant.id for cards in g._row_cards.values() for c in cards}

# Re-binding a recycled card to an updated PlantNode (same id) must refresh
# its labels, not just keep the old name/species from the previous object.
old = g._row_items[row][0][1]
updated = PlantNode.from_dict(old.to_dict()); updated.name = "Recycled Rename"
g._row_items[row] = [(c, updated if p.id == old.id else p)
                     for c, p in g._row_items[row]]
g._materialise_row(row)
card = next(c for c in g._row_cards[row] if c.plant.id == old.id)
assert card._name_lbl.text() == "Recycled Rename"
g._dematerialise_row(row); app.processEvents()
print("F33 off-screen cards recycled into the bounded spare pool")

g._select_all(); app.processEvents()
assert len(g._selected_plants) == N
sb.setValue(sb.maximum() // 2); app.processEvents(); settle(30)
assert all(c._selected for c in g._card_cache.values())
print("F33 Ctrl+A selects all filtered plants; newly materialised rows honour selection")

g._search.setText("Plant 000"); g._rebuild_current_view(); app.processEvents()
assert len(g._visible_plants) == 10 and len(g._card_cache) <= 10
g._search.clear(); g._rebuild_current_view(); app.processEvents()

# A small export materialises the full grid, then returns to the bounded window.
database._data_dir = tempfile.mkdtemp(); plant_db.reset_cache()
for i in range(120):
    plant_db.upsert(PlantNode(name=f"P{i:03d}"))
g._refresh(); app.processEvents()
pix = g._grab_grid()
assert not pix.isNull() and len(g._card_cache) < 120
print(f"F33 export captured {pix.width()}x{pix.height()}px, then restored {len(g._card_cache)} live cards")

# Photos created only for the export must be decoded before grab(), not left
# as placeholders.  Materialise all rows and drain the lazy queue explicitly.
import base64
from PyQt6.QtGui import QImage
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
img = QImage(8, 8, QImage.Format.Format_RGB32); img.fill(0xff22aa44)
ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.OpenModeFlag.WriteOnly)
img.save(buf, "PNG"); buf.close()
png = base64.b64encode(bytes(ba)).decode()
for i in range(3):
    plant_db.upsert(PlantNode(name=f"Photo {i}", images=[png], thumb=png,
                              photo_count=1))
g._refresh(); app.processEvents()
g._materialize_all_rows()
while g._img_decode_queue:
    g._decode_next_card()
photo_cards = [c for c in g._card_cache.values() if c.plant.has_photo]
assert photo_cards
assert all(c._image_loaded and c._img_lbl.pixmap() is not None
           and not c._img_lbl.pixmap().isNull() for c in photo_cards)
photo_thumb = (photo_cards[0].plant.thumb
               or photo_cards[0]._prefetched_thumb
               or plant_db.get_thumb(photo_cards[0].plant.id))
assert photo_thumb
assert (photo_cards[0]._thumbnail_cache_key(photo_thumb)
        != photo_cards[0]._thumbnail_cache_key(photo_thumb + "x"))
print(f"F33 export decoded {len(photo_cards)} off-screen photo card(s) before grab")
g._restore_viewport_window()
os._exit(0)
