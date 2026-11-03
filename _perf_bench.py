"""
_perf_bench.py — headless performance benchmark for BreedingCharts.

Runs the real MainWindow offscreen against a synthetic dataset, then times
the interactive paths that users actually feel: switching views, refreshing
a grid, typing in a search box, resizing a window, loading/auto-laying-out
a lineage.

Nothing here is imported by the app; it is a developer tool.  All storage
is redirected to a throwaway temp directory, so the real data folder and
settings.json are never touched.

    python _perf_bench.py            # default sizes
    python _perf_bench.py 2000 600 800
                                     # plants, notes, lineage nodes
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

N_PLANTS = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
N_NOTES  = int(sys.argv[2]) if len(sys.argv) > 2 else 250
N_NODES  = int(sys.argv[3]) if len(sys.argv) > 3 else 400
N_NOTES_WITH_IMAGE = max(1, N_NOTES // 8)

TMP = tempfile.mkdtemp(prefix="bc_bench_")

# ── Redirect every storage path before anything imports them ─────────────────
import database
database._data_dir = TMP
import plant_db
import notes_store

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage, QColor
from PyQt6.QtCore import Qt

app = QApplication([])

import settings as settings_mod
settings_mod.save_settings = lambda *a, **k: None      # never clobber real settings
settings_mod.save_sidebar_order = lambda *a, **k: None

# ── Results table ────────────────────────────────────────────────────────────
_results: list[tuple[str, float]] = []


def bench(label: str, fn, repeat: int = 1) -> float:
    """Run fn() `repeat` times, record each run, print first + median.

    The first run is reported separately because for anything cached it is
    the only cold number — a median over repeats would hide exactly the cost
    worth measuring.
    """
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    first = times[0]
    med = sorted(times)[len(times) // 2]
    _results.append((label, med))
    tag = "" if repeat == 1 or abs(first - med) < 1.0 else f"   (first {first:8.2f})"
    print(f"  {label:<44} {med:9.2f} ms{tag}")
    return med


def drain(n: int = 3) -> None:
    for _ in range(n):
        app.processEvents()


def settle(ms: int = 400) -> None:
    """Run the real event loop for `ms` so debounce timers actually fire.

    processEvents() alone never expires a 150 ms single-shot timer, so a
    measurement taken straight after setText() would time a no-op.
    """
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _repaint(view, frames: int) -> None:
    """Force `frames` full synchronous repaints of a widget.

    render() goes through the real paint path (drawBackground, every visible
    item, every edge) without needing a compositor, which is what makes this
    measurable offscreen.
    """
    from PyQt6.QtGui import QPixmap, QPainter
    for _ in range(frames):
        pm = QPixmap(view.viewport().size())
        p = QPainter(pm)
        view.render(p)
        p.end()


# ── Synthetic data ───────────────────────────────────────────────────────────
def _png_b64(w: int, h: int, seed: int) -> str:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor((seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256))
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(buf.data())).decode("ascii")


def _note_html(i: int, with_image: bool) -> str:
    body = "".join(
        f"<p>Observation {j} for note {i}: leaves typical, fruit set "
        f"{'good' if j % 2 else 'poor'}.</p>" for j in range(4)
    )
    img = ""
    if with_image:
        img = (f'<p><img src="data:image/png;base64,{_png_b64(320, 200, i)}" '
               f'width="320" height="200" /></p>')
    return (
        '<!DOCTYPE HTML><html><head><meta charset="utf-8" />'
        '<style type="text/css">p, li { white-space: pre-wrap; }</style></head>'
        f'<body style="font-family:Segoe UI; font-size:10pt;">{img}{body}</body></html>'
    )


print(f"\nSynthetic dataset: {N_PLANTS} plants, {N_NOTES} notes "
      f"({N_NOTES_WITH_IMAGE} with inline images), {N_NODES} lineage nodes")
print(f"Temp data dir: {TMP}\n")

from models import PlantNode, Note, LineageTree

# Notes: write notes.json directly — this is setup, not something we measure.
notes_doc = {}
for i in range(N_NOTES):
    n = Note(title=f"Note {i:04d} — cross batch {i % 17}",
             html=_note_html(i, i < N_NOTES_WITH_IMAGE),
             color="" if i % 3 else "#c8963c",
             pinned=(i % 25 == 0))
    notes_doc[n.id] = n.to_dict()
with open(os.path.join(TMP, "notes.json"), "w", encoding="utf-8") as fh:
    json.dump(notes_doc, fh)
notes_store.reset_cache()

# Plants
t0 = time.perf_counter()
for i in range(N_PLANTS):
    plant_db.upsert(PlantNode(
        name=f"Plant {i:05d}",
        species=f"Species{i % 41} album",
        cultivar=f"Cultivar {i % 23}",
        description="Field notes for this accession.",
        year=2000 + (i % 26),
        color="#52a86a",
    ))
print(f"  (seeded plants in {time.perf_counter() - t0:.1f} s)")

# A lineage tree with a realistic parent/child structure
tree = LineageTree(name="Bench Lineage")
nodes = []
for i in range(N_NODES):
    n = PlantNode(name=f"Line {i:04d}", species=f"Species{i % 41} album",
                  year=2000 + (i % 26))
    tree.nodes[n.id] = n
    nodes.append(n)
for i in range(2, N_NODES):
    if i % 2 == 0:
        child = nodes[i]
        child.parent_ids = [nodes[i - 1].id, nodes[i - 2].id]
        child.parent_roles = {nodes[i - 1].id: "mother", nodes[i - 2].id: "father"}

# ── Boot the real window ─────────────────────────────────────────────────────
from settings import AppSettings, build_stylesheet
import ui_main
ui_main.save_settings = lambda *a, **k: None

s = AppSettings()
s.tour_shown = True
s.auto_layout_on_open = False
s.show_props_panel = False
s.last_open_filepath = ""
app.setStyleSheet(build_stylesheet(s))

t0 = time.perf_counter()
win = ui_main.MainWindow(s)
boot_ms = (time.perf_counter() - t0) * 1000.0
win.resize(1600, 1000)
win.show()
drain(10)
print(f"\nMainWindow boot (no lineage open): {boot_ms:.0f} ms\n")

RSS_AFTER_BOOT = None
import perf as perf_mod
RSS_AFTER_BOOT = perf_mod.process_rss_bytes()

notes = win._notes_view
grid = win._grid_view
scene = win._scene

# ── 1. Lineage canvas ────────────────────────────────────────────────────────
print("[ lineage canvas ]")
bench("scene.load_tree (cold)", lambda: scene.load_tree(tree), repeat=3)
bench("auto_layout", scene.auto_layout, repeat=3)
bench("auto_organize", scene.auto_organize, repeat=3)
scene.load_tree(tree)
drain(5)
bench("canvas repaint (20 frames)", lambda: _repaint(win._view, 20), repeat=1)


# ── 2. Grid / Spreadsheet view ───────────────────────────────────────────────
print("\n[ grid view ]")
win._open_grid_view()
settle(150)
grid._all_plants = plant_db.get_all()
grid._role_map_cache = None
bench("grid _rebuild_grid (cold)", grid._rebuild_grid, repeat=3)
bench("grid _rebuild_grid (same data)", grid._rebuild_grid, repeat=3)
grid._search.setText("Plant 01")
settle(250)
bench("grid search keystroke", grid._rebuild_grid, repeat=3)
grid._search.clear()
settle(250)
bench("grid resize (same width)", lambda: grid.resize(1600, 1000), repeat=3)

def _settle_grid_rows(view) -> None:
    for _ in range(100):
        view._build_next_card_batch()
        if all(r in view._row_cards for r in view._active_rows):
            break

def _scroll_run(step: int, steps: int, settle_rows: bool = False) -> None:
    sb = grid._scroll.verticalScrollBar()
    sb.setValue(0)
    grid._update_visible_rows(force=True)
    _settle_grid_rows(grid)
    for _ in range(steps):
        sb.setValue(min(sb.maximum(), sb.value() + step))
        grid._update_visible_rows()
        grid._build_next_card_batch()
    if settle_rows:
        _settle_grid_rows(grid)

def _jump_run(fraction: float) -> None:
    sb = grid._scroll.verticalScrollBar()
    sb.setValue(0)
    grid._update_visible_rows(force=True)
    _settle_grid_rows(grid)
    sb.setValue(int(sb.maximum() * fraction))
    grid._update_visible_rows()
    _settle_grid_rows(grid)

bench("grid scroll 60px steps x40", lambda: _scroll_run(60, 40), repeat=3)
bench("grid scroll 50% jump + settle", lambda: _jump_run(0.5), repeat=3)
grid._scroll.verticalScrollBar().setValue(0)
grid._update_visible_rows(force=True)
_settle_grid_rows(grid)
bench("grid sheet mode rebuild", grid._rebuild_plant_sheet, repeat=2)

# ── 3. Notes view ────────────────────────────────────────────────────────────
print("\n[ notes view ]")
import notes_view as nv_mod

# A brand-new view: _build() ends in refresh(), so construction *is* the
# cold first render of every card.
bench("notes grid construct + first render",
      lambda: nv_mod.NotesGridView(notes._pal, parent=win), repeat=1)
notes._card_cache.clear()
notes._last_sig = None
drain(3)
notes.refresh()
drain(5)
bench("notes refresh (same data)", notes.refresh, repeat=3)
notes._search.setText("batch 3")
settle(300)
bench("notes search keystroke", notes._rebuild_grid, repeat=3)
notes._search.clear()
settle(300)
bench("notes resize (same width)", lambda: notes.resize(1600, 1000), repeat=3)
notes.resize(900, 1000)          # narrower -> fewer columns, real relayout
settle(300)
bench("notes resize (narrower, relayout)", notes._rebuild_grid, repeat=3)
notes.resize(1600, 1000)
settle(300)

RSS_END = perf_mod.process_rss_bytes()

# ── 3b. Correctness of the caching paths ─────────────────────────────────────
# Speed means nothing if cards stop appearing, keep stale text, or leak
# widgets, so assert the observable behaviour directly.
print("\n[ correctness ]")
_failures: list[str] = []


def check(label: str, cond) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


def _visible_cards(view) -> list:
    return [view._grid_layout.itemAt(i).widget()
            for i in range(view._grid_layout.count())
            if view._grid_layout.itemAt(i).widget() is not None]


notes._search.clear()
settle(300)
all_notes = notes_store.get_all()
cards = _visible_cards(notes)
check("every note has a card", len(cards) == len(all_notes))
check("cards are cached, not leaked",
      len(notes._card_cache) == len(all_notes))
check("cards parented to the canvas",
      all(c.parent() is notes._inner for c in cards))
# This page is inside a hidden QStackedWidget page, so isVisible() is False for
# everything — isHidden() tests the widget's own flag, which is what we mean.
check("cards are not explicitly hidden",
      all(not c.isHidden() for c in cards))

card_note_ids = {c._note.id for c in cards}
check("card set matches note set", card_note_ids == {n.id for n in all_notes})

# Text actually rendered
sample = next(n for n in all_notes if n.title.strip() and "base64" not in n.html)
sc = next(c for c in cards if c._note.id == sample.id)
check("title rendered", sc._title_lbl.text().startswith(sample.title[:20]))
check("body preview rendered", len(sc._body_lbl.text()) > 0)
check("pinned badge state correct",
      all((not c._pin_row.isHidden()) == bool(c._note.pinned) for c in cards))

# Rebuild with identical data must not disturb the widget identities
before = {c._note.id: c for c in cards}
notes.refresh()
settle(300)
after = {c._note.id: c for c in _visible_cards(notes)}
check("warm rebuild reuses the same widgets", before == after)

# Filtering
notes._search.setText("batch 3")
settle(300)
filtered = _visible_cards(notes)
check("search narrows the card set", 0 < len(filtered) < len(all_notes))
check("filtered-out cards are hidden but retained",
      all(c.isHidden() for c in cards if c._note.id not in {f._note.id for f in filtered}))

notes._search.setText("zzz-no-such-note")
settle(300)
# A query that matches nothing leaves the scroll area up but empty — the
# "No notes yet" placeholder is reserved for a genuinely empty collection.
check("no-match shows an empty board",
      len(_visible_cards(notes)) == 0 and notes._empty_lbl.isHidden())

notes._search.clear()
settle(300)
check("clearing search restores every card",
      len(_visible_cards(notes)) == len(all_notes))

# Editing a note must refresh exactly that card's text
target = all_notes[0]
target.title = "EDITED TITLE"
target.html = "<p>edited body</p>"
notes_store.upsert(target)
notes.refresh()
settle(300)
tc = next(c for c in _visible_cards(notes) if c._note.id == target.id)
check("edited note's card re-renders", tc._title_lbl.text() == "EDITED TITLE")
check("edited note's body re-renders", "edited body" in tc._body_lbl.text())

# Deleting a note must evict its card
gone = target.id
notes_store.delete(gone)
notes.refresh()
settle(300)
check("deleted note's card is evicted", gone not in notes._card_cache)
check("remaining cards intact",
      len(_visible_cards(notes)) == len(notes_store.get_all()))

# Grid view
grid._search.clear()
settle(300)
grid_cards = list(grid._card_cache.values())
check("grid cards all parented", all(c.parent() is not None for c in grid_cards))
check("grid live cards bounded by visible plants",
      0 < len(grid_cards) <= len(grid._visible_plants))
check("grid spare card pool bounded",
      len(getattr(grid, "_spare_cards", [])) <= 120)
row_pids = {c.plant.id for cards in grid._row_cards.values() for c in cards}
check("grid cache matches materialised rows", set(grid._card_cache) == row_pids)

print(f"  ---> {'all checks passed' if not _failures else str(len(_failures)) + ' FAILED'}")

# ── 4. Persistence round-trip ────────────────────────────────────────────────
print("\n[ persistence ]")
import database as db_mod
bench("save_lineage", lambda: db_mod.save_lineage(tree, os.path.join(TMP, "bench.plf")), repeat=2)
bench("load_lineage", lambda: db_mod.load_lineage(os.path.join(TMP, "bench.plf")), repeat=2)

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
if RSS_AFTER_BOOT and RSS_END:
    print(f"RSS after boot : {perf_mod.format_bytes(RSS_AFTER_BOOT)}")
    print(f"RSS at end     : {perf_mod.format_bytes(RSS_END)}")
    print(f"RSS growth     : {perf_mod.format_bytes(RSS_END - RSS_AFTER_BOOT)}")
print(f"Widget count at end: {len(app.allWidgets())}")
print("=" * 62)

win.close()
notes_store.reset_cache()
try:
    plant_db._shutdown()
except Exception:
    pass
shutil.rmtree(TMP, ignore_errors=True)
os._exit(0)
