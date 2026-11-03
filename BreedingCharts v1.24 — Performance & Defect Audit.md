# BreedingCharts v1.24 — Performance & Defect Audit

## Document guide, status and deliverables

**Deliverables:** this document, and `BreedingCharts_v1_24_perf_patched.zip` (the uploaded code with the verified fixes applied, a `CHANGES_PERF.md` change list, and headless verification scripts in `tools/verification/`). **Back up your data folder before running the patched build.**

**How this document is organised.** It was built up in several passes, so later sections correct earlier ones:

| Read this | For |
| --- | --- |
| §1–§3 | Executive summary, critical defects (C1–C7), other bugs (C8–C18) |
| §4–§6 | Subsystem-by-subsystem performance analysis, assessment of the earlier AI session's changes, phased plan |
| §9–§10 | Before/after benchmarks and scaling measurements |
| §11–§13 | New findings, the **fix register with verified file:line references (F1–F54)**, concrete patches |
| §14–§16 | Further passes: **errata E1–E5**, profiling-driven fixes, interaction costs, painted card chrome |
| **§17** | **Authoritative status of every fix, the supersession map (which earlier statements were corrected), final measured results, next steps** |

**Three things to know before reading in detail**

1. The uploaded snapshot's Grid View cannot render any plant (undefined constants, C1), and two data-safety bugs exist in the save path (C2, C3). All three are fixed in the patched zip.
2. Several of my own earlier recommendations were corrected by measurement: one-shot `json.dumps` in a save thread would freeze the UI, lazy imports are not worth doing, render hints do nothing, and `DeviceCoordinateCache` slows zooming unless suspended (fixed by F62). §17.2 lists every correction.
3. All timings come from a shared headless Linux sandbox with synthetic photos. Trust ratios, not absolute milliseconds; verify on your own machine with the scripts provided.

**Subject:** `BreedingCharts_v1_24_notes_overhaul` (≈15,200 lines of Python, PyQt6) **Scope:** every module was read; suspected defects were reproduced headless (Qt `offscreen`, PyQt6 6.12) and measured; `pyflakes` was run over the tree. **Context:** the snapshot is the state the code was left in when the AI-assisted optimisation session ("improve performance significantly without removing features") stopped mid-run with an error.

> **Reading the numbers.** Timings come from a shared cloud sandbox, not your Windows machine. Treat them as *relative* (ratios, scaling behaviour), not absolute. Where a figure depends on real photo content, I used a synthetic noisy 2400×1800 image whose 1200 px thumbnail came out at \~220 KB base64; real photos will vary.

---

## 1. Executive summary

The session did real work: a SQLite row store for the plant database, mtime-cached lineage metadata, async image decoding on the canvas, paint-path caching in `node_item.py`, a card cache in the Notes view, incremental card construction in the grid, and a background debounced save. Those are sound ideas.

But the snapshot as shipped has **one blocking defect, several data-safety defects, and a number of optimisations that are dead code or silently broken**. The architecture still has three structural limits that no micro-optimisation fixes: full-size thumbnails held everywhere, one QWidget tree per plant card, and whole-file JSON rewrites.

### Severity table

| ID | Severity | Area | One-line description |
| --- | --- | --- | --- |
| C1 | **Blocker** | Grid view | `_CARDS_FIRST_PAINT` / `_CARDS_PER_TICK` are never defined → `NameError`; the card grid cannot render any plant |
| C2 | **Data loss** | Autosave | A failed background save has already evicted un-persisted photos from RAM and cleared the "needs merge" flags; the retry never writes them |
| C3 | **Data loss** | Persistence | Lineage files are written in place (truncate-then-write); an exception or crash mid-write destroys the existing file. Two writers can also overlap |
| C4 | **Data loss** | Grid view | Ctrl+A selects *every* plant even while a search filter hides most of them; Delete then removes the hidden ones too |
| C5 | High | Canvas | Year-band labels are memoised on a dict that is mutated in place → labels stay stale after layout/year changes |
| C6 | High | Open/layout | With the default `auto_layout_on_open=True`, merely opening a lineage overwrites your hand-placed positions and rewrites the file 1.5 s later |
| C7 | High | Layout | `get_generation()` raises on a dangling parent id, so auto-layout silently never runs for that file |
| C8 | Medium | Notes | `notes_store.reset_cache()` fails to reset the sorted cache → after changing data folder, the old folder's notes are shown |
| C9 | Medium | Auto-organise | The "6-pass barycenter sweep" is effectively one pass; crossing minimisation is much weaker than advertised |
| C10 | Medium | Spreadsheet | Parent 1/Parent 2 columns can be blank because `_plants_by_id` is not refreshed on the sheet path |

### Performance headline (measured)

| Scenario | Result |
| --- | --- |
| Canvas, 500 photo nodes | **515 MB** of decoded card pixmaps resident (≈1.03 MB per card); all decodes finish after ≈6.7 s |
| Grid, 1,003 plants (constants patched) | first paint 84 ms, **all cards built 5.9 s**, 7,947 widgets (≈7.9 per plant) |
| Zoomed-out canvas (zoom 0.05, 1080p) | background grid builds **520k QPointF per frame, ≈650 ms** |
| Fetch one node's photos from a 71 MB sidecar | **161 ms** — the whole sidecar is JSON-parsed for 1.8 MB of wanted data |
| Serialise a 500-node lineage (220 KB thumb each) | **108 MB** file; `json.dump(indent=2)` 3.7 s vs compact `dumps` 1.2 s |
| Mouse-move port hit-test, 500 nodes | 1.67 ms per event, linear in node count, also runs during node drags |

---

## 2. Critical and high-severity defects

### C1 — Grid view cannot render (undefined constants)

`grid_view.py:1697` calls `self._flush_plan(_CARDS_FIRST_PAINT)` and `:1769` calls `self._flush_plan(_CARDS_PER_TICK)`. Neither name is defined anywhere in the project (`pyflakes` and an AST scan agree). Reproduced:

```
File "grid_view.py", line 1697, in _rebuild_grid
    self._flush_plan(_CARDS_FIRST_PAINT)
NameError: name '_CARDS_FIRST_PAINT' is not defined
```

Every non-empty plant list hits this, so Grid View (and, via `_refresh`, anything that refreshes it: edit, delete, add, view restore when `last_active_view` is `grid`) fails. Because `log_setup` installs an excepthook, the failure is logged as `CRITICAL ... Unhandled exception` and the view stays empty rather than the process necessarily dying; I did not exercise the live event loop to confirm abort-vs-continue. The bundled `breedingcharts.log` predates the incremental-build change, which is why it shows no error.

The session's "800-plant test hung in the GUI rebuild" was therefore partly masked: the first hang was the first-run tour's modal `exec()` (a test artefact), but the later grid failure was this `NameError`.

**Fix:** define the two module constants (with the constants patched to 40/40 the grid works: first paint 84 ms). Add a smoke test that opens the grid with ≥1 plant; `_perf_bench.py` would have caught this had it been run after the last edit.

### C2 — Failed background save permanently drops un-persisted photos

`ui_main.py::_debounce_save` (≈902–949) calls `prepare_save()`, and *immediately* — before the worker thread has written anything — sets `node.images = []` for every node in `_image_loaded_ids`, then clears that set and `_images_maybe_stale`. If `write_prepared_save` then fails (disk full, antivirus lock, network drive hiccup), `_on_debounce_save_finished(False)` only sets `_unsaved = True`.

Reproduced: after a simulated failed write, the node's images were 0 in RAM, `_image_loaded_ids` was 0, and the retry's `prepared["need_image_merge"]` was `False`. The new photo exists nowhere.

**Fix:** evict only in the *success* handler (pass the set of ids that were snapshotted through the worker's signal), or keep the snapshot until the write is confirmed.

### C3 — Non-atomic writes and overlapping writers

- `database.save_lineage`, `write_prepared_save`, `_migrate_legacy_inline_images`, `data_bundle.import_bundle`, `lineage_images.save_all_images` and `settings.save_settings` all do `open(path, "w")` then `json.dump`. `open(..., "w")` truncates first. Reproduced: a node with an unserialisable value turned a 2,640-byte valid file into a 2,060-byte invalid one (`save_lineage` returned `False`, but the old good file was already gone). A crash, power loss or kill during a multi-MB write has the same effect. `notes_store._save` does this correctly (`.tmp` + `os.replace`); nothing else does.
- `_open_file` and `closeEvent` call `_save_current_silently()` (synchronous `save_lineage`) without checking whether a `_SaveWorker` is still running; `closeEvent` waits 3 s and then proceeds regardless. The two can write the same `.plf` and `.plf.images` concurrently.
- The `.plf` and its `.plf.images` sidecar are written as two separate non-atomic steps, so a crash between them leaves them inconsistent.

**Fix:** one `atomic_write_json(path, obj)` helper (temp file in same directory, `flush`, `os.fsync`, `os.replace`) used everywhere; a single save lock/queue so only one writer touches a lineage at a time; write sidecar first, main file last.

### C4 — Select-all + Delete can remove plants you cannot see

`PlantGridView._select_all` (≈1924) does `self._selected_plants = list(self._all_plants[:])` — *all* plants, not the filtered/visible ones — although its docstring says "every visible plant card". Reproduced: with a search showing 10 of 1,003 plants, Ctrl+A selected 1,003. `_delete_selected_batch` then asks "Permanently delete 1003 plants (first four names…)" and deletes them all. The confirmation shows a count, but a user who sees ten cards will reasonably misread it.

**Fix:** select the ids currently in the layout plan. Also show the filtered count in the confirmation.

### C5 — Year-band labels go stale

`LineageView._sorted_year_rows` memoises `(year_rows, sorted_items)` keyed by *identity* of `scene.year_rows`, with the comment "the dict is replaced wholesale". It is not: `_update_year_rows` and `load_tree` call `self.year_rows.clear()` and refill the same dict. Reproduced: after the first non-empty paint, changing a node's year/row left `drawForeground` using `[(2020, 0), (2021, 340)]` while the scene held `[(1999, 700), (2021, 340)]`; `same dict object reused? True`. Effect: after auto-layout, drags between rows, year edits, or opening a different lineage, the left-hand year strip and dashed row lines are drawn at the old positions until restart. (PNG/PDF export reads the dict directly, so exports are right while the screen is wrong.)

**Fix:** key the memo on a revision counter bumped in `_update_year_rows`, or assign a fresh dict there.

### C6 — Opening a lineage rewrites it (and your layout)

`_load_tree` runs `scene.auto_layout()` after `_loading` is reset, so `tree_changed` fires, `_unsaved` becomes `True`, and the 1.5 s debounce saves. `auto_layout_on_open` defaults to `True`. Reproduced: open a file, touch nothing → flag `True`, timer active, file rewritten, manual positions overwritten. This is exactly the repeating `Loaded 'X'` → `Saved 'X'` pairs in `breedingcharts.log` (15:38:27–31). Consequences:

1. Hand-arranged canvases are destroyed on every open (if the setting is on).
2. Every browse through the sidebar triggers a full write of the lineage (with all thumbnails — see §4.4).
3. `_open_file` → `_save_current_silently()` then performs a *synchronous* write on the UI thread when you click the next lineage.

**Fix:** run auto-layout inside the `_loading` guard and don't mark dirty; or only auto-layout nodes that have never had a stored position. Consider renaming the setting "Auto-layout new/unpositioned plants".

### C7 — `get_generation` crashes on dangling parents

`models.py::get_generation` does `1 + max(... for pid in node.parent_ids if pid in self.nodes)`. If every listed parent is missing the generator is empty → `ValueError: max() iterable argument is empty`. Reproduced; `auto_layout` swallows it (`ERROR:canvas_view:auto_layout: ...`), so on-open layout silently doesn't happen for that file. Dangling ids arise from imports, cross-lineage links, or hand-edited files. **Fix:** `max(..., default=-1)`; also make the recursion iterative (deep chains hit Python's recursion limit).

---

## 3. Other correctness defects

| # | Location | Defect | Impact / fix |
| --- | --- | --- | --- |
| C8 | `notes_store.py::reset_cache` | Declares `global _cache` only; `_sorted_cache = None` creates a *local* (pyflakes: "assigned but never used"). Reproduced: after switching to an empty folder, `get_all()` still returned the old folder's note | Add `_sorted_cache` to the `global` statement |
| C9 | `canvas_view.py::_do_auto_organize` | Barycenter keys read `node.x`, which is not updated between passes (positions applied only after the loop), so passes 2–6 recompute identical sorts. Reproduced: identical output for 1 vs 6 passes; upper rows end ordered by child-x, last row by parent-x | Maintain a working x-index per row and update it after each row sort, then early-exit when no change |
| C10 | `grid_view.py::_rebuild_plant_sheet` | Refreshes `_all_plants` when dirty but builds `id_to_name` from `_plants_by_id`, which only `_refresh()` updates. Reproduced: parent columns blank | Rebuild `_plants_by_id` wherever `_all_plants` is assigned |
| C11 | `canvas_view.py::_rebuild_edges` | `_role_map` is keyed by *parent id only*; a plant that is mother in one cross and father in another gets whichever was iterated last, on its badge, accent colour and card height | Key by `(child, parent)` or compute per-edge |
| C12 | `grid_view.py:1271 & 1882` | `_delete_selected_table_rows` defined twice (second silently wins) | Delete one |
| C13 | `grid_view.py::_dedup_plants` | `... or "".strip().lower()` binds as `X or ("".strip().lower())`, so cultivar is neither stripped nor lower-cased. Method is also unused | Parenthesise or delete |
| C14 | `node_item.py:201–205` | Annotations use `QFontMetrics` which is not imported (harmless under `from __future__ import annotations`; flagged by pyflakes) | Import it |
| C15 | `canvas_view.py::_on_decode_done` | Result is matched by `node_id` only. After an undo rebuild, a late result from the *old* item's task can be applied to the *new* item, which then ignores its own (correct) result because `_decode_pending` is already `False` | Carry a per-item generation token in the task |
| C16 | `ui_main.py::_open_file` | A new `_LoadWorker` replaces `self._load_worker` without waiting; sidebar disabling is the only guard against "QThread destroyed while running" | Keep a list / wait / ignore clicks while loading |
| C17 | `canvas_view.py::EdgeItem.paint` | Arrow head extends up to 8 px beyond the path's bounding rect (pen 2 px) so it can be clipped / leave repaint artefacts | Override `boundingRect` to include the arrow |
| C18 | `main.py` | Logs "Launching BreedingCharts v1.2" and `setApplicationVersion("1.2")` in a v1.24 build | Single `__version__` |

---

## 4. Performance analysis by subsystem

### 4.1 Canvas (`canvas_view.py`, `node_item.py`)

**What was improved (verified in code):** per-item caches for card height, palette, QColor/QPen/QBrush, fonts and metrics; shared shadow brushes and accent-bar path; O(1) role lookup; per-node edge index so a drag updates only the 1–5 touched edges; view frozen during bulk load; edge rebuild skipped in `update_node`; async QImage decode in a `QThreadPool` with `QPixmap.fromImage` on the GUI thread (the correct Qt threading pattern); drag-time `tree_changed` deferred to mouse-release.

**What still limits it:**

1. **Per-card pixmap is \~1 MB and every photo card is decoded up-front.** `IMG_SCALE = 3` targets 600×378 with `KeepAspectRatioByExpanding`, giving 600×450 (1,055 KB) for a 4:3 photo — then it is painted at ≈200×126 scene units. Measured at 500 photo nodes: 515 MB resident, \~6.7 s to finish decoding. `_enqueue_visible_decodes` only *prioritises* visible items; off-screen ones are decoded and kept forever. The grid's own comment budgets 84 KB per card. **Fix:** decode at display size × devicePixelRatio (\~1.5× is plenty → ≈180 KB), only for items near the viewport, evict far items, and cap by a QPixmapCache budget. Optionally use `levelOfDetailFromTransform` to skip images/text below \~0.3 zoom.
2. **No Qt-level rendering optimisations.** There is no `setViewportUpdateMode`, `setOptimizationFlags` (`DontSavePainterState`, `DontAdjustForAntialiasing`), `setCacheMode`/`ItemCoordinateCache` on cards, nor an item-index choice. Each card paints \~25 primitives with antialiasing and `SmoothPixmapTransform` downscaling a 3×-oversized pixmap. Caching a card into an `ItemCoordinateCache` pixmap (invalidated on change, bypassed while hover animates) removes most per-frame cost.
3. **Background grid dots (`drawBackground`).** A Python list of `QPointF` is built every repaint with no density limit: zoom 0.25 → 21k points (14 ms), 0.1 → 130k (115 ms), 0.05 → **520k (652 ms) per frame**. Skip the grid (or double the spacing) when on-screen spacing < \~8 px; or cache the dot tile as a tiled `QBrush` texture and draw with `fillRect`.
4. **Mouse-move hit-testing.** `LineageView.mouseMoveEvent` calls `_find_port_at`, a Python loop over *every* node with two `mapToScene` calls each, on every move event, including during node drags. 0.36 ms at 100 nodes, 1.67 ms at 500 — linear, so ≈17 ms (a missed frame) at 5,000. Use `scene.items(QRectF around cursor)` or a coarse spatial grid, and skip it while a drag is in progress.
5. **Edge items.** `EdgeItem.shape()` constructs a `QPainterPathStroker` on every call (hit-tests, collision queries); cache the stroked path and clear it in `update_path`. `paint` allocates a `QColor`, `QBrush`, `QPainterPath` per edge per frame; build the arrow once in `update_path`.
6. **Accent-key string per paint.** `_do_paint` builds an f-string of six fields per card per frame just to decide whether its cached QColor is valid. A scene-level integer "style revision" is cheaper.
7. **Edges are rebuilt wholesale** (remove all, recreate all) on add, remove, connect, disconnect, paste, theme and settings changes. Make edge maintenance incremental by node id.
8. **Undo is a full rebuild.** `_push_undo` serialises the whole tree (0.1 ms at 100 nodes, 1 ms at 500 — cheap because strings are shared), but `undo()` rebuilds every item and re-enqueues every image decode (88 ms + several seconds of decode at 500 nodes) for what may be a single drag. The stack (50 entries) also holds references to full-resolution base64 strings, which defeats lazy eviction. Prefer command-style undo (node moved, field changed) with snapshots only for structural changes.
9. **Synchronous decode fallbacks.** `scene.add_node` and `paste_nodes` use `_add_node_item` (not `_add_node_item_and_decode`), so new/pasted photo cards decode synchronously inside `paint()` — a paste of K photo nodes stalls the UI K×(5–15 ms).
10. **Settings churn.** `apply_settings` always invalidates every item and rebuilds all edges, even for unrelated settings (autosave minutes, keybinds). `MainWindow._apply_settings` also re-sets the *application* stylesheet each time, which re-polishes every widget. Diff old/new settings and apply only what changed.
11. **Small items:** `fit_view`/`export_to_image` use `scene.items()` (sorted, includes edges) instead of the node dict; `mouseReleaseEvent` runs `_update_year_rows` on every click; `search_nodes` rebuilds a haystack string per node per keystroke (precompute lowercase, debounce); `CommentItem` emits `tree_changed` on every move/resize event (nodes defer it; comments don't) and allocates `QFont`/`QColor`/`QPen` per paint; `_hover_timer` is created without a parent.

### 4.2 Grid / spreadsheet (`grid_view.py`)

**Improvements present:** card cache, incremental plan-based build (once C1 is fixed), per-card style-state memo, role map computed once, O(1) id→name map, `QPixmapCache` for card thumbnails, `remove_many` in plant\_db.

**Measured (constants patched, 1,003 plants, no photos):** first paint 84 ms; *all* cards built in 5.9 s (≈5.9 ms each); 7,947 widgets; re-layout with cached cards 16 ms.

1. **One QFrame + \~8 child widgets + \~7 per-widget stylesheets per plant.** Each `setStyleSheet` on a child under an application stylesheet triggers style resolution and polish. This is the structural cost; it scales linearly in time, widgets and RAM and will not become "large-database" friendly by tuning. **Fix (architectural):** replace the card grid with a virtualised view — a `QListView` in `IconMode` with a `QStyledItemDelegate` that paints the card with `QPainter`, over a `QAbstractListModel` backed by the plant cache. Only visible rows are painted; sorting/filtering become model operations (`QSortFilterProxyModel`); selection is native (multi-select, Ctrl+A over the filtered rows fixes C4 for free). Same for the spreadsheet: `QTableView` + `QAbstractTableModel` instead of 9×N `QTableWidgetItem` objects.
2. **Thumbnail decode on the GUI thread, one per timer tick.** `_decode_next_card` loads a 1200 px JPEG into a `QPixmap`, scales, crops, and stores it (≈7 ms decode + scale) — 1,000 photo plants ≈ 7+ s of GUI-thread work while the event loop spins at 0 ms intervals. Also `self._img_decode_queue.pop(0)` is O(n). Move to `QRunnable` workers producing `QImage` (as the canvas already does), use `collections.deque`, and request only visible rows.
3. **Thumbnail is 1200 px but shown at 180×120** (≈95 KB as pixmap). Store a second small thumbnail (≈360 px) for lists/grids; keep the 1200 px one only for the canvas.
4. **`apply_card_settings` clears the whole `QPixmapCache` when the palette changes** — thumbnails don't depend on theme colours, so a theme switch forces re-decoding every photo. Also clears `_card_cache` and rebuilds everything for a colour change; restyle in place or, with a delegate, just `viewport().update()`.
5. **`resizeEvent` → full `_rebuild_current_view` after 50 ms**, even when the column count hasn't changed (it re-adds all N cards to the layout). Compare `n_cols` first.
6. **`_select_all` is O(N²)** (`any(sp.id == pid ...)` per card): 15 ms at 1,000 here, ≈0.4 s at 5,000, 1.5 s at 10,000. Use a set.
7. **Deletes loop `plant_db.delete()`** (two transactions each) in `_delete_selected_batch` and `_delete_selected_table_rows`; `remove_many()` exists and is unused.
8. **`eventFilter` is installed on every card** and runs in Python for every event (paint, hover, style change…); it also calls `_get_or_create_plant_sheet()` (which can *create* a tab) on any key press. Handle keys with shortcuts on the parent instead.
9. **`showEvent`/`mark_dirty` churn.** `_load_tree` marks the grid dirty on every lineage open, so every later switch to Grid re-runs `plant_db.get_all()` (22 ms per 5,000) and a relayout. `plant_db.get_revision()` was written for exactly this and is never called (see §5).
10. `_on_table_cell_changed` / `_table_context_menu` still use a linear `next(p for p in _all_plants ...)` although `_plants_by_id` exists.

### 4.3 Plant database (`plant_db.py`)

**Good:** SQLite WAL, dirty-row flushing, lazy per-plant image rows, coalesced thumbnail writes, single-transaction `remove_many`. Measured insert/update/get\_all/search are all fast at 5,000 plants (`get_all` 22 ms).

**Problems:**

1. **`search()` never uses the stored `search_text`.** It does `meta.get("search_text") or _search_text(meta)`, but `search_text` lives in a SQL column, not in `_meta_cache`, so the haystack is rebuilt (join + lower) for every plant on every keystroke: 3.2 ms vs 0.2 ms with a precomputed map at 5,000 plants (16×). Cache lower-cased haystacks in memory, invalidated per row on upsert.
2. **Metadata rows embed the thumbnail.** `meta_json` and `_meta_cache` hold `thumb` (≈100–300 KB of base64 at 1200 px). The module docstring claims "\~1–5 MB at startup"; at 10,000 photo plants it is \~2 GB of Python strings, and every rename rewrites the thumbnail text in the row. Move thumbnails to their own table/BLOB column, load lazily, and store raw bytes (base64 adds 33%).
3. **`upsert` from the lineage edit path rewrites all image rows and leaves them resident.** `PlantNode.images` loaded from a lineage sidecar are not in `_img_store`, so `prev_imgs is None` → image rows are DELETE+INSERTed. Measured: 4.2 MB rewritten for a year-only edit (21 ms here), and the strings then stay in `_img_store` for the session because `_open_edit_dialog` never calls `evict_images`. Compare against the database's own rows (hash/length) before rewriting; evict after upsert.
4. **Revision counter is dead code** (`_bump_revision_locked`/`get_revision` are never called anywhere), so no view can skip rebuilds.
5. `AutocompleteDB.load_from_plant_db` (called from every `PlantDialog.__init__`) builds a PlantNode for every plant and sorts three sets just to collect strings; query `SELECT DISTINCT name/species/cultivar` or maintain the sets incrementally.
6. `_ensure_conn` is reachable without the lock from `recommended_pixmap_cache_kb` (potential double connection); images are stored as base64 TEXT rather than BLOB.
7. `PlantDBDialog._filter` rebuilds a QListWidgetItem for every plant on every keystroke with no debounce, and re-joins/lower-cases strings; use the model/view approach or a debounce plus cached haystacks.

### 4.4 Lineage files and image sidecar (`database.py`, `lineage_images.py`, `models.py`)

1. **Thumbnails are inline in the `.plf` and are 1200 px.** `THUMB_MAX_DIM = 1200` at quality 85 (≈220 KB base64 for my synthetic photo). Every debounced save rewrites all of them even when the user only dragged one card. Measured: a 500-node lineage with photos is a **108 MB** `.plf`; `json.dump(indent=2)` to file took 3.7 s versus 1.2 s for compact `dumps`. `json.dump` (not `dumps`) always uses the pure-Python encoder, and `indent=2` adds whitespace; the debounce "background" thread holds the GIL for much of that, so the UI still stutters. **Fix:** move thumbnails out of the `.plf` into a sidecar/SQLite table keyed by node id (or generate a small 300–400 px thumb inline and a large one on demand); write compact JSON with `ensure_ascii=False`; save only when the tree actually changed.
2. **The sidecar is one big JSON dict, parsed whole to fetch one node.** `get_node_images` → `load_all_images` parses the entire `.plf.images`. Measured 161 ms to read 1.8 MB out of a 71 MB sidecar, and it happens on every edit-dialog open, copy, export and paste. `merge_and_save_snapshot` reads *and rewrites* the whole file for any touched node. **Fix:** store images per node in the same SQLite approach used for plants (or one file per node / an offset index), so cost scales with the data needed.
3. **Cancelling a dialog still dirties the sidecar.** `_open_edit_dialog` calls `populate_node_images`, which adds the id to `_image_loaded_ids`; the next save then does a full read+rewrite of the sidecar even if nothing changed.
4. **`get_lineage_meta` parses the whole file on a cache miss** just to read name/count/category. Write a tiny metadata header (or `.meta` file / index table) at save time.
5. **Unrelated parses of every lineage on the UI thread:** `DuplicateDetectorDialog._scan` and `CrossLineageConnectDialog._load` call `load_lineage` for every file (including thumbnails, and potentially the legacy-migration rewrite), and `data_bundle._local_lineage_ids` fully parses every `.plf` to read one id.
6. **Startup/close writes:** `save_settings` (pretty-printed, non-atomic) runs on every lineage open and view switch; `_save_last_view` also saves.

### 4.5 Notes (`notes_view.py`, `notes_store.py`)

**Improvements present:** card cache with signature-based `set_note`, memoised plain-text previews, cached preview pixmaps, rebuild skipped when the signature is unchanged, lazy first render, atomic temp-file save. (The session's last message said the Notes view "destroys and recreates" cards; in this snapshot it already reuses them.)

**Remaining:**

1. Every `upsert`/`delete` rewrites the *entire* `notes.json` including every inline base64 image, with `indent=2` through the pure-Python encoder, on the UI thread. Store notes in SQLite (one row each) or at least compact JSON; keep pasted images as separate files/rows.
2. Every `NoteCard` carries a `QGraphicsDropShadowEffect` plus a `QPropertyAnimation`; graphics effects force an off-screen render and blur on each repaint (hover animates `blurRadius` for 140 ms). Fine for dozens of notes, costly for hundreds; paint a static shadow pixmap instead.
3. No virtualisation (all cards are live widgets); `search()` lower-cases each note's preview per keystroke — cache the lower-cased text with the plain text.
4. `_plain_cache` entries for deleted notes are never evicted. `reset_cache` bug: see C8.
5. `NoteEditorDialog` mutates the shared cached `Note` object live (colour/pin) and `reject()` also saves, so there is no real cancel — deliberate (Keep-style), but worth documenting.

### 4.6 Main window / startup (`ui_main.py`, `main.py`)

- **Good:** startup restore of the last lineage is now asynchronous; sidebar updates a single row after autosave (`touch_file_row`); Notes view is lazy.
- `QApplication.setStyleSheet(build_stylesheet(s))` is re-run for *every* `_apply_settings` call (see 4.1-10); re-polishing the whole widget tree is expensive and unnecessary for non-visual settings.
- Everything is imported eagerly (dialogs, tour, notes, settings); lazy-import rarely used dialogs to trim cold start.
- `perf` instrumentation exists (`--debug`) but only covers a few calls; extend it to paint, decode and save to make regressions visible.

---

## 5. Assessment of the optimisation session's work

| Change | Verdict |
| --- | --- |
| SQLite store for plant DB | Good. Real win for large DBs. Needs thumbnail separation, in-memory haystack cache, evict-after-upsert (4.3) |
| `get_lineage_meta` mtime cache | Good; still parses the whole file on a miss (4.4-4) |
| Async canvas image decode (QImage in worker, QPixmap on GUI) | Correct pattern. Decoded size too large and not bounded (4.1-1); stale-result race (C15) |
| `node_item.py` paint caches | Reasonable. Shared brushes/paths are safe. Residual per-frame allocations and key-string building (4.1-6); no card caching |
| Edge index for drag updates | Good |
| Deferred `tree_changed` on drag | Good (comments not covered) |
| `_do_auto_organize` children map | Harmless but the algorithm was already effectively single-pass (C9) |
| Background debounced save | Right idea, but evicts before success (C2), non-atomic (C3), still GIL-heavy |
| Incremental grid build | **Broken by missing constants** (C1) |
| `plant_db.get_revision()` / `_bump_revision_locked` | **Dead code**: defined, documented as used by views, never called. No view skips rebuilds |
| `sidebar.touch_file_row` | Good |
| Notes card cache / memoised previews | Good |
| `_perf_smoke_test.py`, `_perf_bench.py` | Useful, but the bench seeds plants and nodes with **no photos**, so it never measures the dominant costs (decode, pixmap RAM, thumbnail I/O); it also has no assertion that the grid renders. The shipped test files import-patch private variables and write nothing to CI |

Net: the optimisations targeted CPU micro-costs; the dominant costs are *data volume* (1200 px thumbnails everywhere, whole-file rewrites) and *object count* (widget-per-plant), which remain.

---

## 6. Recommended remediation plan

### Phase 0 — same day (small, safe)

1. Define `_CARDS_FIRST_PAINT` / `_CARDS_PER_TICK` (e.g. 40 / 40). Add a headless test that opens the grid with 50 plants.
2. Fix C2 (evict on success only), C4 (select visible only), C5 (year-rows revision), C7 (`default=`), C8 (`global`), C10, C12.
3. Run auto-layout inside the loading guard so opening a file is not a write (C6), or make it opt-in.
4. Add `atomic_write_json` and use it everywhere (C3); serialise saves behind one lock.
5. Guard `drawBackground` (skip dots when spacing < 8 px).

### Phase 1 — days (contained refactors)

1. Canvas: decode at \~1.5× display size, viewport-only decode with eviction, `ItemCoordinateCache` for resting cards, cached edge shape/arrow, hit-test via `scene.items(rect)`, skip port search during drags.
2. Settings diffing so unrelated changes don't restyle the app or rebuild edges.
3. `plant_db`: cached lower-case haystacks; call `get_revision()` from views to skip no-op rebuilds; evict after lineage upserts; use `remove_many` for deletes.
4. Debounce the Plant DB dialog filter; cache autocomplete word lists.
5. Compact JSON and `dumps`-then-write for `.plf`, notes and settings.

### Phase 2 — architectural (what actually unlocks large databases)

1. **Thumbnails:** two sizes (≈360 px grid/list, ≈900–1200 px canvas), stored as BLOBs outside the metadata row and outside the `.plf`.
2. **Virtualised grid and spreadsheet:** `QAbstractListModel`/`QAbstractTableModel` + delegates + proxy model. Removes \~8 widgets per plant, makes selection, sort, filter and Ctrl+A correct by construction.
3. **Lineage storage:** per-node image rows (SQLite) instead of the JSON sidecar; a metadata header for the sidebar; incremental saves of changed nodes only.
4. **Command-based undo** instead of full-tree snapshot + scene rebuild.
5. **Decode pipeline shared by canvas and grid:** one `QThreadPool` job type producing `QImage`, a size-keyed LRU cache, cancellation when items leave the viewport.

### Phase 3 — safety net

- Replace the ad-hoc scripts with `pytest` + `pytest-qt` (offscreen): grid renders; open-then-no-write; failed-save retry keeps photos; year labels update; notes folder switch; Ctrl+A with filter; undo restores.
- Make the benchmark seed real photo thumbnails, record time **and** RSS, and fail on regressions.
- Run `pyflakes`/`ruff` in CI (it flags C1, C8 and the duplicate method immediately).

---

## 7. Appendix

### 7.1 Measurements (sandbox, relative only)

| Test | Result |
| --- | --- |
| Thumb from 2400×1800 synthetic photo | 220 KB base64, 28 ms to create |
| Decode 1200×900 JPEG thumb | 5.6–6.7 ms |
| Canvas pixmap per card (600×378 expanding → 600×450) | 1,055 KB (grid card target: 95 KB) |
| Canvas N=100: load / decode-all / pixmaps | 15 ms / 1.25 s / 103 MB |
| Canvas N=500: load / decode-all / pixmaps | 168 ms / 6.7 s / 515 MB |
| `_push_undo` N=100 / 500 | 0.1 ms / 1.0 ms |
| `undo()` rebuild N=100 / 500 (excl. decode) | 24 ms / 88 ms |
| `_find_port_at` N=100 / 500 | 0.36 ms / 1.67 ms |
| `.plf` 500 nodes with thumbs | 108 MB; `json.dump(indent=2)` 3.7 s; `dumps(indent=2)` 2.4 s; compact 1.2 s |
| Background grid dots at zoom 1 / 0.25 / 0.1 / 0.05 | 1 ms / 14 ms / 115 ms / 652 ms per frame |
| `plant_db.search` at 5,000 | 3.2 ms recomputing haystacks vs 0.2 ms precomputed |
| `plant_db.get_all` at 5,000 | 22 ms |
| Lineage-edit `upsert` rewriting 4.2 MB images | 21 ms; strings stay resident |
| Sidecar 71 MB, fetch one node | 161 ms |
| Grid, 1,003 plants (constants patched) | first paint 84 ms; all cards 5.9 s; 7,947 widgets; relayout 16 ms |
| `MainWindow` boot (empty data dir) | 65 ms |

### 7.2 Static analysis (`pyflakes`)

Undefined names: `_CARDS_FIRST_PAINT`, `_CARDS_PER_TICK` (grid\_view), `QFontMetrics` (node\_item, annotations only), `PlantNode` (grid\_view, annotation). Redefinitions: `_delete_selected_table_rows`; repeated local imports of `QApplication`/`QMessageBox`/`QTimer` in `dialogs.py:1731`. Unused local: `_sorted_cache` in `notes_store.reset_cache` (the C8 bug). Plus 37 unused-import warnings.

### 7.3 Not verified

- Real-photo thumbnail sizes (synthetic image used) and Windows/GPU behaviour of painting; no live GUI event-loop run of the NameError path.
- Potential `QObject` destruction from a worker thread when `QRunnable.setAutoDelete(True)` releases `_ImgDecodeTask.signals` (a known PyQt pitfall); not reproduced.
- Windows-specific file locking (antivirus/OneDrive) as the real-world trigger for C2/C3.
- Frozen-exe (`build_exe.py`) behaviour was read but not executed.

### 7.4 Reproduction notes

All experiments ran with `QT_QPA_PLATFORM=offscreen` against a temp data directory (`database._data_dir` redirected), so no real data or `settings.json` was touched. Each defect above marked "Reproduced" was triggered by a short script calling the app's own functions (e.g. `LineageView._sorted_year_rows`, `MainWindow._debounce_save` with `write_prepared_save` stubbed to fail, `database.save_lineage` with an unserialisable value, `PlantGridView._rebuild_grid` with the two constants assigned). I can turn these into a proper `pytest` suite if useful.

---

## 8. Additional improvements found in a second pass

These are lower-risk wins that were not covered above. Ordered roughly by value for effort.

### 8.1 Image handling

1. **Decode thumbnails with `QImageReader.setScaledSize()` instead of `QImage.loadFromData()` + `scaled()`.** `image_utils.make_thumb_from_*` fully decodes the original (a 12 MP photo is \~48 MB of RGB) and then scales it. For JPEG, Qt can decode directly at 1/2, 1/4 or 1/8 size, which is several times faster and uses far less transient RAM. This is used when adding a photo, in the plant dialog `_commit`, during legacy migration, and in bundle import. Also run it in a worker thread: `PlantDialog._commit` and `_set_image_from_path` currently freeze the UI while it runs.
2. **Clipboard and dropped images are stored as full-size PNG.** `_global_paste`, `PlantNodeItem._set_image_from_qimage` and `dialogs._qimage_to_b64` save the clipboard `QImage` as PNG. A screenshot or photo as PNG is typically many times larger than the same image as JPEG/WebP, and that base64 ends up in the sidecar and plant DB. Store photographic content as JPEG (q≈90) or WebP, keep PNG only when the image has real transparency, and optionally cap the long edge (e.g. 3000 px) for the stored original.
3. **Base64 everywhere.** Images are base64 text in JSON and in SQLite TEXT. That is +33% size, a decode on every use, and megabyte-long Python strings that are slow to hash/compare. Use SQLite BLOB (or files on disk) for originals and thumbnails.
4. **Do not key caches by the base64 string.** `PlantDialog._icon_cache` and `_preview_cache` use the full image string as a dict key; the first lookup hashes the whole string and a hit compares it again. Key by index or a short digest.
5. **Re-encoding quality for thumbs:** 1200 px q85 is generous for a 200 px card. A 640 px canvas thumbnail (with an on-demand larger one for zoom > 2×) cuts file size, RAM and decode time by roughly a factor of 3–4.

### 8.2 Small, global overheads

1. **`database.get_data_dir()` calls `os.makedirs(..., exist_ok=True)` on every call** and is invoked from sidebar refresh, notes and plant-DB path helpers. Create it once when it is set/loaded.
2. **Sidebar:** `search.textChanged` calls `_filter` immediately (no debounce), which clears and rebuilds every `QListWidgetItem` and builds a new `QFont` per item. Debounce 120–150 ms, share one `QFont`, and hide/show items instead of recreating them. `_fuzzy_match` lower-cases the lineage name for every character of the query; lower-case once.
3. **Settings writes:** `save_settings` (pretty-printed, non-atomic) runs on every lineage open, view switch, theme/grid/images/props toggle and exit. Coalesce with a single-shot timer and write atomically.
4. **`get_all()` returns fresh `PlantNode` objects each call** (22 ms per 5,000, plus sort). Cache the sorted list and invalidate it with the revision counter; most callers only need id/name/species/cultivar.
5. **`PlantNode` is a plain dataclass.** With 10k+ nodes in RAM, `@dataclass(slots=True)` (or lighter dict rows for the grid) reduces per-object memory and speeds attribute access.
6. **JSON speed:** if a dependency is acceptable, `orjson` (optional import with `json` fallback) serialises/parses large documents several times faster than the standard library and releases the GIL for less time; at minimum use `json.dumps(..., separators=(",", ":"))` then a single `write()` instead of `json.dump(indent=2)`.
7. **Per-item `QTimer` for hover animation (canvas)**: one shared 60 Hz timer that advances all animating cards would avoid creating a timer per card and unparented timers that outlive deleted items.

### 8.3 Startup and packaging

1. **`--onefile` + `--collect-all PyQt6`** (`build_exe.py`). A onefile PyInstaller build unpacks the whole bundle to a temp folder on *every* launch, and `--collect-all PyQt6` pulls in every Qt module and plugin. Typical effect: multi-second cold start and a very large exe. Prefer `--onedir` (optionally wrapped in an installer), drop `--collect-all`, and exclude unused modules (WebEngine, Qml, Multimedia, 3D, etc.; this app needs Core/Gui/Widgets and optionally Svg).
2. **Import cost:** `import ui_main` alone took \~390 ms cold here because dialogs, tour, notes and settings import eagerly. Move rarely-used dialogs (About, Duplicate Detector, Pedigree, Export Bundle, Tour) to function-level imports.
3. **Show the window first.** Window creation is fast (MainWindow built in \~65 ms with an empty data dir). Keep heavy work (plant DB load, pixmap cache sizing, `warmup_decode_threadpool`) after `show()` via `QTimer.singleShot(0, ...)`.
4. **Application stylesheet cost scales with live widgets.** Re-applying it took \~10 ms with 300 widgets; with the grid's \~8,000 widgets it will be far higher. Another reason to virtualise the grid and to avoid re-applying the stylesheet for non-visual settings.

### 8.4 Qt rendering switches worth trying (measure each)

- `QGraphicsView.setViewportUpdateMode(...)` (`MinimalViewportUpdate` while dragging; `SmartViewportUpdate` otherwise) and `setOptimizationFlags(DontSavePainterState | DontAdjustForAntialiasing)`.
- `setCacheMode(CacheBackground)` on the view once the dot grid is a cached tile.
- `scene.setItemIndexMethod(NoIndex)` can help scenes whose items move constantly; keep BSP for large static scenes and set `setBspTreeDepth` explicitly.
- Drop `SmoothPixmapTransform` while panning/zooming (apply it only when the view is idle) — a cheap interaction-time win.
- On high-DPI Windows displays, verify `QT_ENABLE_HIGHDPI_SCALING` behaviour: pixmap sizes (3× `IMG_SCALE`) assume DPR ≈ 1–3 but are fixed, so sizing from `devicePixelRatioF()` avoids both blurriness and waste.

### 8.5 Process and tooling

- Add `--profile` support using `cProfile`/`pyinstrument` around the three heavy paths (open lineage, switch to grid, drag a node) and keep the output in `debug.log`.
- Track `QPixmapCache` hit rate and resident RAM in the existing Debug → RAM dialog so regressions in image memory are visible immediately.
- Add a synthetic "stress" dataset generator (5,000 plants with photos, a 1,000-node lineage) to the repo and run it before releases; the current bench has no photos and therefore cannot see the dominant costs.

### 8.6 Suggested priority for these additions

| Priority | Item |
| --- | --- |
| Do now | 8.2-1 (`makedirs`), 8.2-2 (sidebar debounce), 8.2-3 (settings coalescing), 8.1-2 (JPEG/WebP for pasted photos), 8.1-4 (cache keys) |
| Next | 8.1-1 (scaled JPEG decode, off the UI thread), 8.2-6 (compact JSON / orjson), 8.3-2/3 (lazy imports, post-show init), 8.4 (view flags, measure) |
| With the architectural work | 8.1-3/5 (BLOBs, smaller thumbs), 8.2-4/5 (cached lists, slots), 8.3-1 (onedir packaging), 8.3-4 |

---

## 9. Before/after comparison benchmarks

Each row compares the pattern the app uses today with the proposed replacement, run in the same process on the same machine (Qt offscreen, software raster, shared cloud CPU). Ratios are the meaningful part; absolute times on your Windows PC will differ. Scripts used: three self-contained Python files (thumbnails/JSON, model-view/hit-testing, canvas frame time) that I can hand over.

### 9.1 Results

| # | Operation | Current approach | Proposed approach | Gain |
| --- | --- | --- | --- | --- |
| 1 | Create a 1200 px thumbnail from a 12 MP JPEG (3.1 MB) | `loadFromData` + `scaled`: **152 ms** | `QImageReader.setScaledSize`: **65 ms** | **2.4×** |
| 2 | Same, but target a 640 px canvas thumbnail | 152 ms | **60 ms** | 2.5× |
| 3 | Thumbnail size and decode | 1200 px q85: **168 KB** b64, 7.4 ms decode | 640 px q82: **60 KB** b64, 2.0 ms decode | 2.8× smaller, 3.7× faster |
| 4 | Resident canvas pixmap per card (4:3 photo) | `IMG_SCALE 3`: 600×450 = **1,055 KB** | `IMG_SCALE 2`: 469 KB; `1.5`: **264 KB** | 2.2× / **4×** less RAM (1,000 cards: 1.03 GB → 0.26 GB) |
| 5 | Store a pasted 2560×1440 screenshot/photo | PNG: **4.23 MB**, 1,098 ms | JPEG q90: **0.44 MB**, 49 ms; WebP q85: 0.13 MB, 301 ms | JPEG 9.6× smaller and **22× faster**; WebP 32× smaller |
| 6 | Write a 300-node document with thumbnails (36 MB) | `json.dump(indent=2)`: **640 ms** | `orjson.dumps`: **44 ms** | **14.5×** |
| 7 | Same, atomic (temp + fsync + `os.replace`) | not used today | compact `dumps` + atomic: 857 ms | the data-safety fix costs \~0.2 s on 36 MB; with orjson it would be far lower |
| 8 | Parse the same file | `json.load`: 124 ms | `orjson.loads`: 100 ms | 1.2× (parsing is memory-bound) |
| 9 | Card grid, 500 plants | widget per plant: **717 ms**, \~4,000 widgets | — | — |
| 10 | Card grid, 2,000 plants | widget per plant: **4,185 ms**, \~16,000 widgets | — | build time grows faster than linearly (5.8× for 4× the plants) |
| 11 | Card grid, 5,000 plants | extrapolates to well over 10 s | `QListView` (IconMode) + delegate: **22 ms** to be scrollable, constant regardless of N | **>100×** at 2,000+, and RAM no longer grows with N |
| 12 | Spreadsheet, 5,000 rows × 4 | `QTableWidget` + items: 48 ms | `QTableView` + model: 6 ms | 8× (see caveat 9.2-3) |
| 13 | Canvas port hit-test per mouse move, 1,000 nodes | Python loop over all nodes: **3.02 ms** | `scene.items(rect)`: **0.004 ms** | **>700×** (cursor over empty space; still sub-0.1 ms over items) |
| 14 | Canvas background at zoom 0.05 | per-frame Python dot list: **711 ms** (519,901 points) | cached tile brush: **0.5 ms** (or skip below \~8 px spacing: 0 ms) | **>1,000×** |
| 15 | `_select_all`, 5,000 plants | `any()` scan: **330 ms** | set lookup: **0.4 ms** | **\~800×** |
| 16 | Canvas frame, 300 photo cards, zoom 1.0 (\~25 visible) | **19 ms** | `DeviceCoordinateCache` on cards: **6 ms** | **3.2×** |
| 17 | Canvas frame, same scene, zoom 0.3 (all 300 visible) | **73 ms** (≈14 fps) | `DeviceCoordinateCache`: **21 ms** (≈48 fps) | **3.5×** |
| 18 | `plant_db.search` at 5,000 plants | haystack rebuilt per call: 3.2 ms | precomputed haystacks: 0.2 ms | 16× |

### 9.2 Corrections and caveats on earlier sections

1. **JSON compact vs indent.** In §4.4 I reported compact `dumps` as \~3× faster than `json.dump(indent=2)` (1.2 s vs 3.7 s at 500 nodes). In this controlled re-run on a 36 MB document the two were about equal (640 ms vs 722 ms; disk-write noise dominates because the content is almost entirely long base64 strings). **Conclusion:** indentation is not the bottleneck for thumbnail-heavy files; *volume* is. The real fixes are (a) take thumbnails out of the `.plf`, (b) use `orjson` or write once from a bytes object, (c) save only on real change. Treat the earlier 3× figure as run-to-run noise on a shared machine.
2. **Render hints and optimisation flags did nothing measurable.** Turning off `SmoothPixmapTransform` (19 vs 19 ms; 74 vs 73 ms) and setting `DontSavePainterState | DontAdjustForAntialiasing` (20 and 74 ms) did not change frame time in this software-raster test. §8.4's suggestion to drop smooth transforms while panning should be treated as unproven; per-item caching (row 16–17) is the one that works. On a GPU-backed Windows viewport results may differ, so measure there.
3. **`QTableWidget` is not a large cost at 5,000×4** (48 ms). Moving to `QTableView` is a worthwhile cleanup (8×, no per-cell objects, native sorting via proxy), but it is far lower priority than the card grid, and matters mainly beyond \~50k cells.
4. **List rebuilding is cheap.** Re-creating a 5,000-item `QListWidget` for a filter that keeps few rows cost 2 ms, versus 5 ms for `setRowHidden`. The sidebar/PlantDB-dialog filter rebuild in §8.2-2 is therefore *not* a measurable problem for short result sets; keep a debounce for large result sets, but do not prioritise it.
5. **Edge hit-test shape.** `EdgeItem.shape()` costs \~9 µs per call uncached. It only matters if called thousands of times per event (large scenes under hover); medium-low priority, lower than I implied in §4.1-5.
6. **Hit-test row 13** is a best case (cursor away from items). It still removes an O(N) Python loop from every mouse move, including drags.

### 9.3 What the measurements say to do first

Ranked by measured gain per unit of effort:

1. **Cache canvas cards** with `DeviceCoordinateCache` (invalidate on edit/hover/selection change) — 3×+ frame time, one-line change plus invalidation hooks.
2. **Guard the background dots** — removes a 0.7 s per-frame stall when zoomed out; trivial.
3. **Replace the Python port hit-test loop** with `scene.items(rect)` and skip it during drags — removes per-event O(N) cost.
4. **Drop canvas pixmaps to \~1.5× display size** — 4× less image RAM with no visible change at normal zoom; add a re-decode at higher zoom only if wanted.
5. **Store pasted photos as JPEG (or WebP)** — \~10–30× smaller, 22× faster to encode.
6. **Scaled JPEG decode (`QImageReader`) for thumbnail creation**, in a worker thread — 2.4× faster and no UI freeze.
7. **Fix `_select_all` and the plant search haystack** — trivial, 16–800×.
8. **Virtualise the card grid** (`QListView` + delegate) — the single largest scalability change (4.2 s → constant for 2,000 plants; no per-plant widgets).
9. **Use orjson (or at least a single bytes write) and atomic replace** — 14× faster serialisation that also pays for the fsync/replace safety cost.
10. **Smaller thumbnails (640 px) stored outside the lineage JSON** — \~3× less data to write, parse, hold and decode.

### 9.4 Method

Each case ran the competing implementations back-to-back in one process, taking the best of 3–5 runs after a warm-up, with `QT_QPA_PLATFORM=offscreen`. Photo content was synthetic (random ellipses over noise), so JPEG sizes are indicative only. Canvas frames were rendered with `QGraphicsView.render()` into a 1600×1000 `QImage` using the app's real `LineageScene`/`PlantNodeItem` classes and 300 nodes with thumbnails and parent edges, after all thumbnails had decoded.

---

## 10. Third pass: scaling behaviour, paint profile and remaining overheads

This pass asks two questions: *which canvas operations actually scale badly*, and *where does a frame spend its time*. Same environment and caveats as §9.

### 10.1 Canvas operation scaling (no photos; synthetic lineage with 2 parents per node)

| Operation (ms) | N = 200 | N = 1,000 | N = 3,000 | Verdict |
| --- | --- | --- | --- | --- |
| `load_tree` (excl. image decode) | 78 | 318 | 838 | Linear, fine; **\~0.28 ms/node** |
| `auto_layout` | 26 | 164 | 688 | Slightly super-linear; acceptable |
| `_do_auto_organize` | 27 | 142 | 428 | Linear; acceptable |
| `add_node` (rebuilds every edge) | 14 | 66 | **266** | Linear in *total* edges for a one-node change |
| `_rebuild_edges` alone | 10 | 39 | 158 | Dominates `add_node` |
| Drag one node, per frame | 0.05 | 0.05 | 0.06 | **Constant** — the edge index works as intended |
| `search_nodes`, per keystroke | 0.1 | 0.5 | 1.9 | Fine |
| `_push_undo` | 0.4 | 1.4 | 5.2 | Fine (strings shared) |

**Takeaway:** the canvas *logic* is not the bottleneck up to thousands of nodes. The earlier optimisation work (edge index, deferred drag signals) is validated. What hurts is **rendering** (§10.2), **image memory/decoding** (§4.1) and **I/O volume** (§4.4). The only logic-side item worth doing is making `add_node`, `remove_node`, `apply_connection` and paste update edges incrementally instead of calling `_rebuild_edges()` (a 266 ms hitch per added plant at 3,000 nodes, \~25× more than a one-node change should cost).

### 10.2 Where a canvas frame goes (cProfile, 300 cards visible, no photos, 74 ms/frame)

| Function | Calls (5 frames) | Time | Share |
| --- | --- | --- | --- |
| `drawRoundedRect` | 8,800 (≈8 per card) | 85 ms | 16% |
| `PlantNodeItem._do_paint` (Python body only) | 1,100 | 79 ms | 15% (≈72 µs per card of pure Python) |
| `drawText` | 7,800 (≈7 per card) | 66 ms | 12% |
| `LineageView.drawBackground` | 5 | 62 ms (**12 ms/frame**) | 11% |
| `EdgeItem.paint` (Python) | 2,005 | 36 ms | 7% |
| `setPen` | 19,820 (≈18 per card) | 29 ms | 5% |
| `elidedText` + `horizontalAdvance` | 3,300 + 4,400 | 34 ms | 6% |
| Enum `__or__` evaluated at paint time | 4,500 | \~21 ms cumulative | 4% |

Things this shows that I had not stated precisely before:

1. **A resting card is redrawn from scratch every time it is exposed**: \~8 rounded rects, \~7 text draws, \~18 pen changes and 7 text measurements per card, plus \~72 µs of Python. That is exactly the work `DeviceCoordinateCache` removes (measured 3.5× in §9.1 rows 16–17). Repainting *because something else moved* (an edge, a selection halo, a neighbour) then costs a pixmap blit instead of a full redraw.
2. **`drawBackground` costs 12 ms/frame even at zoom 0.3** (grid dots plus tint) and has no caching, so it is paid on every pan step and every item update that exposes background. Cache it (`setCacheMode(CacheBackground)` together with a tiled-pixmap brush) and skip dots below \~8 px spacing (§9.1 row 14).
3. **Text measurement per paint.** `elidedText` and `horizontalAdvance` run for name/species/tags on every paint; the result depends only on text and card width. Cache the elided strings on the item and invalidate them in `refresh()`/`invalidate_caches()`.
4. **Enum flag ORs inside `paint`.** `Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap` (`node_item.py:657–658`, `:673`) and the similar ones at `canvas_view.py:1273`, `1767`, `1809` are evaluated on each call; PyQt6 enums are Python `enum.Flag` objects, so each `|` goes through `enum.py`. Hoist them to module-level constants (this is also why `enum.py:__or__` shows 4,500 calls).
5. **Edge painting is a Python call per edge per frame** (2,005 calls here). With cached shapes, a pre-built arrow polygon, and one shared pen/brush, this shrinks; alternatively draw all edges in one `QGraphicsItem` (one `paint` call that loops in C++-friendly batches via `drawLines`/a combined `QPainterPath` per colour). Combined with `DeviceCoordinateCache` on cards, edges become the next hot spot.

### 10.3 Database write batching

| Test (5,000 rows) | Result |
| --- | --- |
| App `plant_db.upsert` one at a time | 400 ms (80 µs each) |
| Raw SQLite, commit per row | 112 ms |
| Raw SQLite, one transaction (`executemany`) | **13 ms** (8.6× faster than per-row commits) |

`upsert` already coalesces dirty rows, so the app's per-row cost is acceptable for interactive edits. For **bulk operations** — data-bundle import, CSV import into the database, "add all to lineage", batch tag edits — add a single `upsert_many(nodes)` that builds all rows and writes them in one transaction. I measured only the raw SQLite ratio; the application-level gain should be similar in order of magnitude but is not measured.

### 10.4 Additional low-effort items

1. **Hoist enums and constants** out of `paint()` and other hot paths (§10.2-4). Trivial, \~4% of frame time, zero risk.
2. **Cache `QFontMetrics` results and elided strings per card** (§10.2-3).
3. **Merge edge items** into a single batched item, or at least cache shape/arrow (§10.2-5, §4.1-5).
4. **Incremental edge maintenance** for add/remove/connect/paste (§10.1).
5. **`upsert_many` for imports and multi-select operations** (§10.3).
6. **Set `Qt.WA_OpaquePaintEvent` / opaque backgrounds** where a widget fully paints its background (grid viewport, notes scroll area) so Qt can skip erasing; **prefer `setUpdatesEnabled(False)` around any remaining bulk widget rebuild** (the grid already freezes the view in places; the Notes and sidebar rebuilds do not).
7. **Pre-warm once, off the critical path:** the first canvas paint after load builds fonts, pens, palettes and metrics for every card. Do this for visible cards first (it already decodes them first) and finish the rest from an idle timer.
8. **Release memory after big transitions:** when switching lineage, call `QPixmapCache.clear()` selectively, clear the undo stack of the previous lineage (it is replaced but references old full-resolution strings until the next GC), and run `gc.collect()` once after a large `load_tree` swap if RAM is a concern.

### 10.5 Updated top-priority list (supersedes §9.3 ordering where they differ)

1. Define the two missing grid constants (C1) — the app's grid is unusable without it.
2. Data-safety fixes C2, C3, C4 (and make auto-layout-on-open non-dirtying, C6).
3. `DeviceCoordinateCache` on cards + hoisted enums + cached elided text (measured 3.5× frame time, plus the paint-time overheads above).
4. Guard/cached background (11% of a zoomed-out frame; 0.7 s per frame at minimum zoom).
5. Smaller canvas pixmaps (4× less RAM) and viewport-limited decoding.
6. `scene.items(rect)` hit-testing; skip during drags.
7. JPEG/WebP storage for pasted photos; scaled-JPEG thumbnail creation in a worker.
8. Virtualised card grid (`QListView` + delegate) — measured 4.2 s → constant for 2,000 plants.
9. Thumbnails out of the lineage JSON; `orjson` + atomic replace.
10. Incremental edge updates and `upsert_many` for bulk operations.

### 10.6 Honest limits of these measurements

- All timings use Qt's offscreen *software* raster engine. On Windows the canvas normally uses the same raster engine unless a `QOpenGLWidget` viewport is set, so ratios should carry over, but absolute frame times will be faster on your CPU.
- I did not measure `DeviceCoordinateCache` while a zoom animation is running (the cache is re-rendered at each new scale) or with the hover shadow animation active; the right approach is to cache only resting cards and call `setCacheMode(NoCache)` on the hovered one.
- The sample lineage is synthetic; the real 89-plant database from your log is small enough that the *current* bottlenecks you will feel are the missing grid constants, startup/open writes, and image decode, not the scaling limits above. The scaling data matters if the database grows to hundreds or thousands of plants, which is the stated goal.

---

## 11. Fourth pass: new measurements and findings

Same environment and caveats as §9 (offscreen software raster, shared CPU; ratios are meaningful, absolute times are not). All line numbers in §11–§13 were re-verified with `grep -n` against the uploaded zip and **supersede approximate line numbers quoted in earlier sections**.

### 11.1 Measurements

| ID | Test | Current pattern | Replacement | Result |
| --- | --- | --- | --- | --- |
| M1 | 800 grid cards × 6 labels, construction | per-widget `setStyleSheet` (as `grid_view.py:624, 646, 657, 664, 680`) **823 ms** | one container stylesheet with `#objectName` selectors **307 ms**; `QPalette` per label 370 ms | **2.7×** faster |
| M2 | `plant_db` startup with 1,500 plants that have a \~163 KB thumbnail | thumbnail inside `meta_json`: **470 ms, +213 MB resident** | — | the module docstring's "\~1–5 MB at startup" is false for photo databases |
| M3 | Parse those 1,500 metadata rows | with `thumb` key: **301 ms** | without `thumb`: **5.7 ms** | **52×** |
| M4 | Fetch one node's 2 photos, 53 MB store (40 nodes × 2 photos) | whole-JSON sidecar (`lineage_images.py:46–56, 62`): **149 ms** | SQLite BLOB rows keyed by node: **0.22 ms** | **676×** |
| M5 | Change one node's photo in that store | read + rewrite whole sidecar (`lineage_images.py:128`→`74–75`): **282 ms** | one SQLite DELETE+INSERT: **6.3 ms** | **45×** |
| M6 | Sidebar metadata of a 9.5 MB lineage on cache miss (`database.py:113–146`) | full JSON parse **23.2 ms** (×100 files = 2.3 s) | tiny `.plf.meta` header **0.014 ms** (×100 = 1 ms) | **1,600×** |
| M7 | Paint-time enum OR (`node_item.py:657–658, 673`) | `Qt.AlignmentFlag.AlignLeft \| …` **3.53 µs** | module constant **0.088 µs** | 40× |
| M8 | `QFontMetrics.elidedText` (`node_item.py:671, 727`) | **12.0 µs** per call | dict-cached **0.14 µs** | 86× |
| M9 | Canvas frame split, 300 cards, 560 edges, zoom 0.3 | total **60 ms**: cards 43, edges 12 (20%), grid dots 13 | — | cards dominate while uncached |
| M10 | Same frame with `DeviceCoordinateCache` on cards | total **19 ms**; edges alone **9 ms (46%)** | — | after card caching, **edges become the main cost** |
| M11 | One scroll step at zoom 0.05 (whole scene in view, 300 cards) | no cache **548 ms** | `setCacheMode(CacheBackground)` **47 ms** | **11.7×**, before even fixing the dot loop |

### 11.2 New findings

**N1 — Thumbnails live inside the metadata row, so photo databases cost hundreds of MB at startup (M2, M3).** `plant_db._meta_json` (`plant_db.py:146`) serialises the full metadata dict, including `thumb`, into `meta_json` (written at `:236–240`, `:361–365`, `:748`; loaded into `_meta_cache` at `:287–289`; set at `:540`). Every rename rewrites the thumbnail text; every startup parses and keeps it. At 1,500 photo plants: +213 MB and 470 ms, i.e. ≈140 KB of RAM per plant before any card exists. Fix in §13.9.

**N2 — Per-widget stylesheets are the biggest constant factor in card construction (M1).** `MiniPlantCard` calls `setStyleSheet` on up to seven child widgets per plant (`grid_view.py:624, 646, 657, 664, 680`; plus `:536, :543` per section header). Each call re-resolves and re-polishes that widget against the application sheet. Moving to one container-level sheet with `objectName`/dynamic-property selectors cuts construction 2.7× even *before* virtualising the grid.

**N3 — After cards are cached, edges are 46% of the frame (M10).** `EdgeItem` (`canvas_view.py:118–195`) is one Python `paint` call per edge (2,005 calls per 5 frames in §10.2). Once N1-style card caching lands, optimise `EdgeItem.paint` and `shape` (cache polygon/stroked path in `update_path`, `:151`) and share pens/brushes per colour class. Do **not** merge all edges into one opaque item: per-edge hover/selection highlight is a feature and must survive.

**N4 — `CacheBackground` is a one-line 11.7× win for panning when zoomed out (M11).** `LineageView.__init__` (`canvas_view.py:1113–1124`) never sets a cache mode. Combine with the dot-density guard in `drawBackground` (`:1204–1227`); call `resetCachedContent()` whenever grid visibility, theme palette or accent tint changes.

**N5 — The image sidecar and lineage metadata should be indexed, not parsed (M4–M6).** `lineage_images.py` (`:46–75, 93–127, 128–`) and `database.get_lineage_meta` (`:113–146`) both pay O(file size) for O(1) answers.

**N6 — DeviceCoordinateCache memory is small, but shares `QPixmapCache`.** Qt stores per-item caches in `QPixmapCache`. Cached pixmaps are device-sized: a card is ≈220×300 px at 100% zoom → ≈260 KB, \~30 visible → ≈8 MB; at zoom 0.3 a card is ≈66×90 px → 24 KB × 300 ≈ 7 MB (arithmetic from `CARD_W = 220`, `node_item.py:36`; I did not measure Qt's internal accounting). `main.py:82–86` sizes the limit from the number of photo plants (20–300 MB), so add \~16 MB headroom or items and thumbnails will evict each other.

**N7 — Decode pool is the global `QThreadPool`.** `_enqueue_decode` (`canvas_view.py:589–594`) uses `QThreadPool.globalInstance()`; any other `QRunnable` shares it. A dedicated pool (`QThreadPool(self)`, `setMaxThreadCount(max(2, QThread.idealThreadCount()-1))`) keeps decoding from starving other work. (Design recommendation, not measured.)

**N8 — `_open_file` saves synchronously on the UI thread before loading (`ui_main.py:1099–1102`).** Because of C6 the flag is almost always set, so every sidebar click writes the whole previous lineage (with thumbnails) on the UI thread before the load even starts. Fixing C6 removes most of this; additionally guard with `if self._unsaved:` (already inside `_save_current_silently`, `:964`) and prefer handing the write to `_SaveWorker`.

---

## 12. Fix register (file : lines → change)

Grouped by priority. "Gain" cites a measurement ID from §9, §10 or §11 where one exists; blank means correctness only.

### 12.1 P0 — correctness and data safety

| # | Location | Change |
| --- | --- | --- |
| F1 | `grid_view.py:1697, 1769` | Define `_CARDS_FIRST_PAINT = 40` and `_CARDS_PER_TICK = 40` at module scope (above `class MiniPlantCard`, line 553). Add a smoke test that builds the grid with 50 plants. (Obsolete if the grid is virtualised, §13.12.) |
| F2 | `ui_main.py:902–949` (`_debounce_save`), `:951` (`_on_debounce_save_finished`) | Do **not** set `node.images = []` / clear `_image_loaded_ids` / `_images_maybe_stale` before the write. Capture `snap = {nid: tuple(images)}`, pass it to the finished-handler, and evict only on `ok` and only where `tuple(node.images) == snap[nid]`. Patch in §13.2. |
| F3 | `database.py:176–177, 228–229, 292–293`; `lineage_images.py:74–75`; `data_bundle.py:179`; `settings.py:429, 443` | Replace `open(path,"w")` + `json.dump` with `atomic_write_bytes(path, serialised_bytes)` (new `fileio.py`, §13.3): serialise first, write to a temp file in the same directory, `fsync`, `os.replace`. Take a per-path lock in `save_lineage`/`write_prepared_save`. Write the sidecar before the `.plf`. |
| F4 | `ui_main.py:1099–1102` (`_open_file`), `:964` (`_save_current_silently`) | Before a synchronous save call `self._save_worker.wait()` if `isRunning()` (as `closeEvent` already does at `:2306+`), or route through the same single-writer lock as F3. |
| F5 | `grid_view.py:1924–1932` (`_select_all`) | Select only the plants in the currently laid-out (filtered) list; use a set for membership. Patch in §13.4. Also include the filtered count in the delete confirmation (`_delete_selected_batch`, `:1992`). |
| F6 | `canvas_view.py:530–535` (`_update_year_rows`), `:1142`, `:1281–1292` (`_sorted_year_rows`), `load_tree` (`year_rows.clear()`) | Add `self._year_rev` (int) to the scene, bump it in every place that mutates `year_rows`, and key the view's memo on it instead of dict identity. Patch in §13.5. |
| F7 | `ui_main.py:1186–1205` (`_load_tree`), `canvas_view.py:940–943, 959, 1039–1040` | Run `auto_layout()` while `self._loading` is still `True` (move the `self._loading = False` at `:1193` below the layout call) and give `auto_layout`/`_do_auto_layout` a `record_undo` flag so open-time layout does not call `_push_undo()` (`:1040`). Decision for the owner: lay out on open only when no node has a stored position; the toolbar "Auto-Layout" already exists for the rest. |
| F8 | `models.py:240–251` (`get_generation`) | `max(..., default=-1)` and make iterative with a cycle guard. Patch in §13.6. |
| F9 | `notes_store.py:43–47` (`reset_cache`) | `global _cache, _sorted_cache`. One-line change. |
| F10 | `grid_view.py:1175` (and `:1456`) | Build `id_to_name` from the list actually used (`{p.id: p.name for p in self._all_plants}`), or assign `self._plants_by_id` wherever `self._all_plants` is assigned in `_rebuild_plant_sheet` (`:1154`). |
| F11 | `canvas_view.py:998` (`for _pass in range(6)`) in `_do_auto_organize` (`:958`) | Keep a working index map `pos[id] = (row, idx)` updated after each row sort so each pass sees the previous pass's order; stop early when no row changed. Today passes 2–6 are no-ops. |
| F12 | `canvas_view.py:643–656` (`_rebuild_edges`), `:776` | Key `_role_map` by `(child_id, parent_id)` instead of `parent_id`. |
| F13 | `grid_view.py:1271` and `:1882` | Delete the first `_delete_selected_table_rows` (the second silently wins). Make both use `plant_db.remove_many` (see F27). |
| F14 | `grid_view.py:1553` (`_dedup_plants`) | Parenthesise `((p.cultivar or "").strip().lower())` or delete the unused method. |
| F15 | `canvas_view.py:596` (`_on_decode_done`), `:589–594` | Add a per-item generation token to `_ImgDecodeTask` and ignore results whose token != `item._decode_gen`. |
| F16 | `main.py:42, 47` | Single `__version__`, use it for the log line and `setApplicationVersion`. |

### 12.2 P1 — high-value performance changes

| # | Location | Change | Gain |
| --- | --- | --- | --- |
| F17 | `node_item.py:212` (after the `setFlag` calls in `PlantNodeItem.__init__`) | `self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)`. `update()` already invalidates the item's cache (hover tick `:794`, `refresh` `:455`, `invalidate_caches` `:222`), so no extra hooks needed. Raise `QPixmapCache` limit by ≈16 MB at `main.py:82–86` (N6). | M10: 60 → 19 ms/frame |
| F18 | `canvas_view.py:1115–1116`, `:1179` (`set_theme_palette`), `set_show_grid`, tint setter | `self.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)`; call `self.resetCachedContent()` when grid/theme/tint change. | M11: 548 → 47 ms/step |
| F19 | `canvas_view.py:1204–1227` (`drawBackground`) | Early-return when `40 * transform().m11() < 8`, or double the spacing until it is ≥ 8 screen px. | 711 ms/frame at zoom 0.05 → 0 |
| F20 | `canvas_view.py:1300–1322` (`_find_port_at`), call sites `:1466`, `:1493` | Query `scene.items(QRectF)` around the cursor instead of looping every node; skip the hover query while a node drag or pan is active. Patch in §13.8. | 3.0 ms → 0.004 ms at 1,000 nodes |
| F21 | `node_item.py:38` (`IMG_SCALE`), `:435–436`, `canvas_view.py:589–590`, `:438` | `IMG_SCALE = 1.5` (or derive from `devicePixelRatioF()`), use `int()` in the two target-size computations; in `apply_settings` (`:438`) call `_enqueue_decode` instead of the synchronous `_reload_pixmap()`. | 1,055 → 264 KB/card |
| F22 | `canvas_view.py:74–89` (`_ImgDecodeTask.run`), `image_utils.py:61–110` | Decode with `QImageReader` + `setScaledSize` (JPEG decodes at 1/2, 1/4, 1/8 natively); do the same in `make_thumb_from_bytes/_b64/_qimage`. Run thumbnail creation in the worker pool, not in `PlantDialog._commit` (`dialogs.py:653/716/769`). | 152 → 65 ms |
| F23 | `image_utils.py:26–27` (`THUMB_MAX_DIM = 1200`, `THUMB_QUALITY = 85`) | Two thumbnails: 360 px for list/grid, 640 px for canvas (regenerate a larger one on demand when zoomed in). Store as raw bytes, not base64 (§13.9). | 168 → 60 KB; decode 7.4 → 2.0 ms |
| F24 | `plant_db.py:146, 236–240, 287–289, 361–365, 540, 748` | Move `thumb` out of `meta_json` into a `thumbs(id, small BLOB, large BLOB)` table; fetch lazily with a bounded LRU. Migration in one transaction. Patch in §13.9. | M2: −213 MB, M3: 52× |
| F25 | `plant_db.py:575–596` (`search`) | Cache lower-cased haystacks in a dict updated by `upsert`/`remove*`; the current `meta.get("search_text")` is always `None` (the column is not in `_meta_cache`). | 3.2 → 0.2 ms @5k |
| F26 | `plant_db.py:99–107, 598, 662, 719, 540` and `grid_view.py:999, 2081` | Call `_bump_revision_locked()` in `upsert`, `remove`, `remove_many`, `set_thumb`, `import_from_dict`, `reset_cache`; store `self._built_rev` in the grid and skip `_refresh()` in `showEvent` when `plant_db.get_revision() == self._built_rev`. `ui_main.py:_load_tree` should stop calling `mark_dirty()` (`grid_view.py:999`) unless the DB changed. | avoids O(N) rebuild per lineage open |
| F27 | `grid_view.py:1882–…` and `:1992–…` | Replace the per-plant `plant_db.delete()` loop by one `plant_db.remove_many(ids)` (`plant_db.py:662`). | 1 transaction instead of 2N |
| F28 | `plant_db.py:598–631` (`upsert`), `ui_main.py:1441, 1471, 1508, 1550, 1809` | After a lineage-side `upsert`, call `plant_db.evict_images(node.id)` (`:511`); compare incoming images with the stored rows (length/hash column) before DELETE+INSERT. | avoids rewriting MBs on a year edit |
| F29 | `ui_main.py:866, 871, 876, 880, 885, 890, 1184, 1209, 1953, 2181, 2312`; `settings.py:426–429` | Replace direct `save_settings` calls with `self._schedule_settings_save()` (single-shot 500 ms `QTimer`), keep the synchronous call only in `closeEvent`. Make the write atomic (F3). | removes UI-thread disk writes |
| F30 | `database.py:113–146`, `:149–189`, `:222–231` | Write `<file>.plf.meta` (`name, category, accent, node_count, modified`) atomically on every save; `get_lineage_meta` reads it when its mtime ≥ the `.plf` mtime, else parses once and regenerates. Also `data_bundle.py:102–130`, `dialogs.py:1371, 1412`. | M6: 1,600× |
| F31 | `lineage_images.py:46–75, 82, 93–127, 128–` | Replace the JSON sidecar by a SQLite store (`images.db`, table `(lineage_id, node_id, idx, data BLOB)`); keep the public function signatures; one-time import of existing `.plf.images`. Patch in §13.10. | M4/M5: 676× / 45× |
| F32 | `grid_view.py:624, 646, 657, 664, 680` (+ `:536, 543`), `:941–960` | Give labels `objectName`s and a role/state dynamic property; build one stylesheet in `apply_card_settings`; delete per-label `setStyleSheet`. Patch in §13.11. | M1: 2.7× |
| F33 | `grid_view.py` whole card grid (`MiniPlantCard` `:553`, `_rebuild_grid` `:~1630–1770`, `_card_cache`, `_decode_next_card` `:1503`) | Virtualise: `QListView` (IconMode, `setUniformItemSizes(True)`, `setLayoutMode(Batched)`) + `QAbstractListModel` over the plant cache + `QStyledItemDelegate.paint` + `QSortFilterProxyModel`; thumbnails via `QPixmapCache` filled by a `QThreadPool` of `QImage` decoders. Removes F1, F5, F10, F32, `:1503` and the 8 widgets per plant. Outline in §13.12. | §9 rows 9–11: 4.2 s → 22 ms |

### 12.3 P2 — smaller, safe wins

| # | Location | Change |
| --- | --- | --- |
| F34 | `node_item.py:657–658, 673`; `canvas_view.py:1273, 1767, 1809` | Hoist `Qt.AlignmentFlag.… \| …` to module constants (M7). |
| F35 | `node_item.py:671, 694, 710, 727–728` | Cache `elidedText` / `horizontalAdvance` results per item in a dict cleared by `invalidate_caches` (`:222`) and `refresh` (`:455`) (M8). |
| F36 | `node_item.py:514–525` | Replace the per-paint `acc_key` f-string with a scene-level `style_rev` integer. |
| F37 | `grid_view.py:952` | Remove `QPixmapCache.clear()` from `apply_card_settings`; thumbnails do not depend on palette. |
| F38 | `grid_view.py:1813–1816` (`resizeEvent`) | Compute the new column count first and return when unchanged. |
| F39 | `grid_view.py:1503–1507` | `collections.deque.popleft()` instead of `list.pop(0)`; decode in a pool as `QImage` (as `canvas_view.py:62–89` does). |
| F40 | `grid_view.py:1750, 1817…` | Stop `card.installEventFilter(self)` per card; handle keys with `QShortcut`/`keyPressEvent` on the grid. |
| F41 | `grid_view.py:1221, 1255, 1282, 1896` | `self._plants_by_id.get(pid)` instead of `next(p for p in self._all_plants …)`. |
| F42 | `canvas_view.py:753`, `:857`, `:610`/`:624` | `add_node`/`paste_nodes` should use `_add_node_item_and_decode` and add only the new node's edges instead of `_rebuild_edges()` (266 ms per add at 3,000 nodes). |
| F43 | `canvas_view.py:738` (`undo`), `:675–829` | Reuse existing items' pixmaps when the node's `thumb` is unchanged; longer-term, command-based undo. |
| F44 | `canvas_view.py:151, 167` | Cache the arrow polygon and stroked shape in `update_path`; share pens/brushes per colour class (N3). |
| F45 | `canvas_view.py:1690` (`fit_view`), `:1705` (`export_to_image`) | Iterate `self._scene._node_items.values()` instead of `scene.items()`. |
| F46 | `canvas_view.py:288–317` (`CommentItem`) | Defer `tree_changed` to mouse release like nodes do. |
| F47 | `plant_db.py:797–806` | Query is fine, but call `_ensure_conn()` under `_lock`. |
| F48 | `database.py:65` (`get_data_dir`) | Cache the path and run `os.makedirs` once, not on every call. |
| F49 | `ui_main.py:819–865` (`_apply_settings`), `:830` (`app.setStyleSheet`) | Diff old/new settings; only re-style when theme/font changed; only call `_scene.apply_settings`/`_grid_view.apply_card_settings` when their inputs changed. |
| F50 | `dialogs.py:44, 292` | Cache `AutocompleteDB` lists and invalidate with `plant_db.get_revision()`; stop calling `plant_db.get_all()` per dialog open. |
| F51 | `dialogs.py:226–239`, `ui_main.py:1778–1800` (`_global_paste`), `node_item.py:911` | Encode photos as JPEG q90 (PNG only with real alpha); key `_icon_cache`/`_preview_cache` (`dialogs.py:290–291, 481, 541, 562`) by index. |
| F52 | `notes_store.py:74–90`, `:160`; `notes_view.py:628–633` | Compact JSON via bytes + atomic write (or SQLite); cache lower-cased preview text; replace `QGraphicsDropShadowEffect` with a pre-rendered shadow pixmap. |
| F53 | `build_exe.py:171, 181, 217, 252` | `--onedir`, remove `--collect-all PyQt6`, exclude unused Qt modules. |
| F54 | `main.py:52–54`; top of `ui_main.py` | Move `warmup_decode_threadpool()` and the heavy imports (tour, duplicate detector, bundle dialogs) after `show()` / to function scope. |

---

## 13. Concrete patches

### 13.1 What was verified

I applied the patches marked **verified** below to a copy of the uploaded zip and re-ran the reproductions and benchmarks. Others are proposals written against the exact code I read; they are not run.

| Fix | Status | Result on the patched copy |
| --- | --- | --- |
| F1 grid constants | **verified** | `PlantGridView._refresh()` builds 30 cards instead of raising `NameError` |
| F2 failed-save restore | **verified** | after a simulated failed write the photo is back in RAM and `_image_loaded_ids`; after a successful retry it is on disk |
| F6 year-row revision | **verified** | year labels are fresh after a layout/year change (previously stale) |
| F8 `get_generation` | **verified** | dangling parent → generation 0, no exception |
| F9 notes cache | **verified** | after switching to an empty folder `get_all()` returns `[]` |
| F17 `DeviceCoordinateCache` | **verified** | canvas frame, 300 cards: zoom 1.0 **15.9 → 5.7 ms**, zoom 0.3 **64.2 → 19.9 ms**. Cached vs uncached output differs by at most **1/255** per channel (sampled over the frame), i.e. rounding only |
| F18 + F19 `CacheBackground` + dot guard | **verified** | zoom 0.05 frame **555 → 12.7 ms**; one scroll step **553 → 14 ms (≈40×)** |
| F20 port hit-test | **verified** | ports still found for 40 random cards in 3 positions each; cost for a miss over 1,000 nodes **3.0 → 0.010 ms** |
| F3, F4, F5, F7, F10–F16, F21–F54 | proposed | not applied |

### 13.2 F2 — do not lose photos when a background save fails (`ui_main.py`) — verified

The live tree is emptied of full-resolution images when the save is *prepared*. Keep the prepared snapshot until the write succeeds and put the images back if it does not.

```python
# _debounce_save, right where the worker is created (≈ line 937)
self._save_worker = _SaveWorker(prepared, self._filepath, parent=self)
self._inflight_prepared = prepared          # NEW
self._save_worker.signals.finished.connect(self._on_debounce_save_finished)

# _on_debounce_save_finished (≈ line 951)
def _on_debounce_save_finished(self, ok: bool) -> None:
    self._inflight_prepared = None if ok else getattr(self, "_inflight_prepared", None)
    if ok:
        ...                                  # unchanged
    else:
        log.warning("Debounce save failed: %s", self._filepath)
        prepared = getattr(self, "_inflight_prepared", None)
        if prepared and self._tree is not None:
            for nid, imgs in prepared.get("images_snapshot", {}).items():
                node = self._tree.nodes.get(nid)
                if node is not None and not node.images:
                    node.images = list(imgs)
                    self._tree._image_loaded_ids.add(nid)
            self._tree._images_maybe_stale = True
        self._unsaved = True
        self._update_title()
```

Limitation: if the user opened a different lineage before the failure arrives, `self._tree` is that other tree; guard with `if self._filepath == <path the worker wrote>` (store the path next to `_inflight_prepared`).

### 13.3 F3 — atomic writes (new `fileio.py`, used by `database.py`, `lineage_images.py`, `data_bundle.py`, `settings.py`) — proposed

```python
# fileio.py
import os, tempfile, threading
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

def path_lock(path: str) -> threading.Lock:
    key = os.path.abspath(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())

def atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)               # atomic on NTFS and POSIX
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def dumps_bytes(obj) -> bytes:              # serialise BEFORE touching the target
    try:
        import orjson
        return orjson.dumps(obj)
    except ImportError:
        import json
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
```

Call sites: `database.py:176–177`, `:228–229`, `:292–293` → `with path_lock(filepath): atomic_write_bytes(filepath, dumps_bytes(doc))`; `lineage_images.py:74–75`; `data_bundle.py:179`; `settings.py:429, 443`. Because serialisation happens first, the `TypeError` case I reproduced (valid 2,640-byte file turned into an invalid 2,060-byte one) leaves the old file intact. Measured cost of compact `dumps` + fsync + `os.replace` on a 36 MB document: 857 ms vs 640 ms today; with `orjson` the same atomic write is dominated by the fsync (§9.1 row 6–7).

### 13.4 F5 — select only what is visible (`grid_view.py:1924–1932`) — proposed

```python
# in _rebuild_grid, after filtering + sorting (the list the plan is built from):
self._visible_plants = plants

def _select_all(self) -> None:
    """Select every plant currently shown (respects the active search)."""
    self._selected_plants = list(self._visible_plants)
    sel_ids = {p.id for p in self._selected_plants}          # O(N), not O(N^2)
    for pid, card in self._card_cache.items():
        card.set_selected(pid in sel_ids)
    self.setFocus()
    self._update_selection_badge()
```

Also initialise `self._visible_plants = []` next to `_all_plants`, and in `_delete_selected_batch` (`:1992`) put the number of *visible* and *selected* plants in the confirmation text.

### 13.5 F6 — year-row memo keyed on a revision (`canvas_view.py`) — verified

```python
# LineageScene._update_year_rows (≈ line 530)
def _update_year_rows(self) -> None:
    self._year_rev = getattr(self, "_year_rev", 0) + 1       # NEW
    self.year_rows.clear()
    ...

# LineageView._sorted_year_rows (≈ line 1281)
def _sorted_year_rows(self, year_rows: dict):
    rev = getattr(self._scene, "_year_rev", 0)
    cached = self._year_rows_sorted
    if cached is not None and cached[0] == rev:
        return cached[1]
    ordered = sorted(year_rows.items())
    self._year_rows_sorted = (rev, ordered)
    return ordered
```

`load_tree` also mutates `year_rows` (`.clear()`); make it call `_update_year_rows()` (it already does at the end) or bump `_year_rev` explicitly so a lineage swap can never reuse a stale list.

### 13.6 F8 — safe, non-recursive generation (`models.py:240–251`) — minimal fix verified, iterative version proposed

Minimal (verified):

```python
result = 1 + max(
    (self.get_generation(pid, _cache)
     for pid in node.parent_ids if pid in self.nodes), default=-1)
```

Iterative with cycle guard (proposed, avoids Python's recursion limit and loops in corrupt files):

```python
def get_generation(self, node_id: str, _cache: dict = None) -> int:
    cache = {} if _cache is None else _cache
    stack, visiting = [node_id], set()
    while stack:
        nid = stack[-1]
        if nid in cache: stack.pop(); visiting.discard(nid); continue
        node = self.nodes.get(nid)
        parents = [p for p in (node.parent_ids if node else ()) if p in self.nodes]
        if not parents: cache[nid] = 0; stack.pop(); continue
        pending = [p for p in parents if p not in cache and p not in visiting]
        if pending:
            visiting.add(nid); stack.extend(pending); continue
        cache[nid] = 1 + max((cache.get(p, 0) for p in parents), default=-1)
        visiting.discard(nid); stack.pop()
    return cache[node_id]
```

### 13.7 F7 — opening a lineage must not write it (`ui_main.py:1186–1205`, `canvas_view.py:940, 1039–1040`) — proposed

```python
# ui_main._load_tree: keep _loading True through the layout
self._loading = True
self._tree = tree; self._filepath = filepath; self._unsaved = False
self._scene.filepath = filepath
self._scene.load_tree(tree)
self._props.clear()
self._update_title(); self._update_node_count(); self._update_undo_state()
self._grid_view.mark_dirty()                  # see F26: only if the DB changed
self._grid_view.set_lineage_open(display_name)
if getattr(self._settings, "auto_layout_on_open", True) and tree.nodes \
        and not any(n.x or n.y for n in tree.nodes.values()):   # owner decision: respect saved layout
    self._scene.auto_layout(record_undo=False)
self._view.fit_view()
self._loading = False                          # moved below the layout call

# canvas_view.auto_layout / _do_auto_layout
def auto_layout(self, record_undo: bool = True) -> None:
    ...
    self._do_auto_layout(record_undo)
def _do_auto_layout(self, record_undo: bool = True) -> None:
    if record_undo: self._push_undo()
```

Behaviour note: with this change the default "auto-layout on open" no longer destroys a layout the user arranged, and opening a file no longer causes a write. The explicit toolbar "Auto-Layout" (`ui_main.py` toolbar) and `auto_organize` still work as today.

### 13.8 F17–F20 — canvas rendering (verified)

```python
# node_item.py, PlantNodeItem.__init__, directly after the ItemSendsGeometryChanges flag (line 212)
self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

# canvas_view.py, LineageView.__init__ (after line 1116)
self.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)

# canvas_view.py, drawBackground (line 1204), right after `if not self._show_grid: return`
sp = 40
if sp * self.transform().m11() < 8:      # dots closer than 8 screen px are just noise
    return

# canvas_view.py, _find_port_at (line 1300): replace the loop over every node
r = QRectF(scene_pos.x() - snap_scene, scene_pos.y() - snap_scene,
           2 * snap_scene, 2 * snap_scene)
for item in (i for i in self._scene.items(r) if isinstance(i, PlantNodeItem)):
    ...  # existing top/bottom port distance checks, unchanged
```

Follow-ups that these changes require:

- Call `self.resetCachedContent()` from `LineageView.set_show_grid`, `set_theme_palette` (`:1179`) and whenever the accent tint changes, otherwise the cached background will keep the old colours.
- Item caches are invalidated by `update()`. Everything in `node_item.py` that changes appearance already calls `update()` (`refresh` `:455`, `invalidate_caches` `:222`, hover tick `:794`, selection). If any code mutates a node field and relies on a bare scene repaint, switch it to `item.update()`.
- Raise `QPixmapCache.setCacheLimit` at `main.py:82–86` by ≈16 MB (N6).
- Import `PlantNodeItem` at module top in `canvas_view.py` (my test used a local import).

### 13.9 F24 — move thumbnails out of metadata (`plant_db.py`, `image_utils.py`) — implemented

F24 landed with a single thumbnail BLOB per plant plus a batched `get_thumbs()` lookup; the two-size (`small`/`large`) split in F23 remains proposed.

```sql
CREATE TABLE IF NOT EXISTS thumbs(
    id    TEXT PRIMARY KEY,
    small BLOB,         -- ~360 px JPEG for grid/list
    large BLOB          -- ~640 px JPEG for the canvas
);
```

1. `_meta_json` (`plant_db.py:146`): drop `"thumb"` before `json.dumps`; keep a `has_thumb` boolean.
2. `_load_raw` (`:287–289`): metadata rows are now small (M3: 301 → 5.7 ms for 1,500 plants, −213 MB).
3. `set_thumb` (`:540`): write the BLOB(s) to `thumbs`; do not touch `_meta_cache`.
4. New `get_thumb(plant_id, size="small") -> bytes | None` backed by a bounded `OrderedDict` LRU (≈ 256 entries). `grid_view.py:727` (`_load_image`) and the canvas decode (`canvas_view.py:589`) read through it.
5. One-time migration inside `_ensure_conn` (`:150`): for each row whose `meta_json` has `thumb`, insert into `thumbs` (decoding base64 once, creating `small` with `QImageReader` scaled decode) and rewrite `meta_json` without it, in a single transaction, then `VACUUM`.
6. `PlantNode.thumb` (`models.py`) stays as a *transient* attribute for lineages; `.plf` files keep their current inline thumbnail until F30/F31 land, so nothing in the file format changes in this step.

### 13.10 F31 — per-node image store instead of the JSON sidecar (`lineage_images.py`) — implemented

Implemented as one `<name>.plf.images.sqlite3` per lineage (rather than one shared database keyed by lineage), with sidecar-first saves and reusable read connections.

Keep the public functions (`load_all_images`, `get_node_images`, `save_all_images`, `populate_node_images`, `snapshot_for_merge`, `merge_and_save_snapshot`, `merge_and_save`, `evict_saved_images`, `delete_sidecar`) so the ≈ 20 call sites in `ui_main.py`, `database.py`, `data_bundle.py` do not change.

```sql
-- one file per data dir: <data_dir>/lineage_images.db  (WAL, synchronous=NORMAL)
CREATE TABLE IF NOT EXISTS lineage_images(
    lineage_key TEXT NOT NULL,       -- stable id: lineage file stem or tree.id
    node_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    data        BLOB NOT NULL,       -- raw JPEG/PNG bytes, not base64
    PRIMARY KEY (lineage_key, node_id, idx)
);
```

- `get_node_images` (`:62`): `SELECT data FROM lineage_images WHERE lineage_key=? AND node_id=? ORDER BY idx` (M4: 149 ms → 0.22 ms).
- `merge_and_save_snapshot` (`:128`): in one transaction, for each touched node `DELETE` then `INSERT`; delete rows of node ids not in `valid_node_ids` (M5: 282 ms → 6.3 ms for one changed photo). No more read-merge-rewrite of the whole file.
- Migration: on first access, if `<file>.plf.images` exists, import it once, then rename it to `.imported.bak`. Export (`data_bundle.py`) keeps writing base64 inside the bundle for compatibility.
- `delete_sidecar` (`:82`) → `DELETE WHERE lineage_key=?`. Rename of a lineage must update `lineage_key` (use a stable id, not the file name).
- The lineage JSON keeps `photo_count`, so the canvas never needs to touch this store until a photo is actually opened.

### 13.11 F32 — one stylesheet for all grid cards (`grid_view.py`) — proposed

```python
# MiniPlantCard.__init__ (lines 624, 646, 657, 664, 680): replace  lbl.setStyleSheet(f"color: ...")  by
name_lbl.setObjectName("cardName")
sp_lbl.setObjectName("cardSpecies")
role_lbl.setObjectName("cardRole");  role_lbl.setProperty("role", role)   # "mother" | "father" | ""
yr_lbl.setObjectName("cardYear")
tag_lbl.setObjectName("cardTag")

# PlantGridView.apply_card_settings (line 941): build once per palette change
p = self._pal
self._grid_widget.setStyleSheet(f"""
  #cardName    {{ color:{p['node_name']};  background:transparent; }}
  #cardSpecies {{ color:{p['text_muted']}; background:transparent; }}
  #cardTag     {{ color:{p['tag_fg']};     background:transparent; }}
  #cardRole[role="mother"] {{ color:{p['mother_line']}; background:transparent; }}
  #cardRole[role="father"] {{ color:{p['father_line']}; background:transparent; }}
""")
```

Measured on 800 cards × 6 labels: 823 → 307 ms (M1). The exact palette keys must match what `MiniPlantCard` reads today (`self._pal.get(...)` at the lines above). This also removes the need for `QPixmapCache.clear()` at `:952` (F37).

### 13.12 F33 — virtualised card grid (outline) — implemented

Implemented with virtualised widget rows, progressive one-row-per-tick materialisation and a bounded spare-card pool rather than the delegate sketch below; the scalability goal is the same.

Why: widget-per-plant measured 717 ms for 500 plants and 4,185 ms for 2,000 (≈ 8 widgets each); a delegate-painted `QListView` was ready in 22 ms at 5,000 and does not grow with N (§9.1 rows 9–11).

```python
class PlantListModel(QAbstractListModel):         # rows = plant ids in display order
    def __init__(self): super().__init__(); self._ids: list[str] = []
    def rowCount(self, parent=QModelIndex()): return 0 if parent.isValid() else len(self._ids)
    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        pid = self._ids[idx.row()]
        if role == Qt.ItemDataRole.UserRole: return pid
        if role == Qt.ItemDataRole.DisplayRole: return plant_db.get_meta(pid)   # dict from the in-memory cache
    def set_ids(self, ids):                         # called from _refresh / filter / sort
        self.beginResetModel(); self._ids = ids; self.endResetModel()

class PlantCardDelegate(QStyledItemDelegate):
    def sizeHint(self, opt, idx): return QSize(CARD_W_GRID, CARD_H_GRID)
    def paint(self, p, opt, idx):
        meta = idx.data(); pix = thumb_cache.get(meta["id"])   # QPixmapCache, filled by a QThreadPool of QImage decoders
        ...                                         # draw frame, thumb (or placeholder), name, species, role badge, year, tags
```

View: `QListView`, `setViewMode(IconMode)`, `setResizeMode(Adjust)`, `setUniformItemSizes(True)`, `setLayoutMode(Batched)`, `setSelectionMode(ExtendedSelection)`, `setMovement(Static)`, with a `QSortFilterProxyModel` for search/sort/group. Features that must be preserved and where they map: selection badge and Ctrl+A (`selectAll()` over the proxy = visible rows, fixing F5 by construction), double-click edit (`doubleClicked`), context menu (`customContextMenuRequested`), drag to lineage (`startDrag`), section headers for group-by (use a second delegate role or a header row type), card hover state (`QStyle.State_MouseOver`). Delete the per-card `installEventFilter` (F40), `_card_cache`, `_flush_plan`, `_decode_next_card` (`:1503`) and the two undefined constants (F1).

### 13.13 F34–F35 — constants and text cache in the card paint (`node_item.py`) — proposed

```python
# module level
_AL_TOP_WRAP = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap   # lines 657–658
_AL_VCENTER  = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter                          # line 673

# PlantNodeItem
def _elided(self, fm, text, width):
    k = (text, width)
    v = self._elide_cache.get(k)
    if v is None:
        v = self._elide_cache[k] = fm.elidedText(text, Qt.TextElideMode.ElideRight, width)
    return v
# self._elide_cache = {} in __init__; self._elide_cache.clear() in invalidate_caches (222) and refresh (455)
```

M7/M8: 3.53 → 0.09 µs and 12.0 → 0.14 µs per call. With F17 these matter mainly on the frames where a card is re-rendered (hover, selection, edit).

### 13.14 Suggested regression tests (pytest + pytest-qt, offscreen)

1. `test_grid_renders`: 50 plants → `PlantGridView._refresh()` creates 50 cards, no exception (F1).
2. `test_failed_save_keeps_photos`: stub `write_prepared_save` → False; photo present in RAM; retry writes it (F2).
3. `test_atomic_save`: unserialisable value → `save_lineage` returns False and the previous file is byte-identical (F3).
4. `test_select_all_respects_filter` (F5).
5. `test_year_labels_follow_layout` (F6).
6. `test_generation_dangling_parent` and `_cycle` (F8).
7. `test_open_does_not_write`: mtime and bytes of the `.plf` unchanged 2 s after open with defaults (F7).
8. `test_notes_reset_cache` (F9).
9. `test_cached_render_matches`: cached vs uncached frame within ±1/255 per channel (F17).
10. `test_frame_budget`: 300 cards ≤ 25 ms at zoom 0.3; scroll step at zoom 0.05 ≤ 50 ms (F17–F19) — use generous thresholds on CI.

### 13.15 Expected combined effect (measured pieces only)

| Area | Before | After the verified/measured fixes |
| --- | --- | --- |
| Canvas frame, 300 cards, zoom 1.0 / 0.3 / 0.05 | 15.9 / 64.2 / 555 ms | 5.7 / 19.9 / 12.7 ms |
| Pan step when zoomed out | 553 ms | 14 ms |
| Mouse-move port test at 1,000 nodes | 3.0 ms | 0.010 ms |
| Grid with the two missing constants | `NameError` | renders (then 4.2 s → 22 ms with F33 at 2,000 plants) |
| Plant DB startup, 1,500 photo plants | 470 ms, +213 MB | metadata-only rows: parse 5.7 ms (F24, measured on the parse step) |
| One-node photo fetch / change | 149 ms / 282 ms | 0.22 ms / 6.3 ms (F31, measured on raw SQLite) |
| Sidebar metadata, 100 lineages, cold | 2.3 s | 1 ms (F30, measured on the read step) |

The "after" figures for F24, F30, F31 are for the isolated operation in the benchmark, not an end-to-end run of a modified app; expect the same order of magnitude but verify with the regression tests above.

---

## 14. Fifth pass: corrections, profiling-driven fixes, negative results

Same environment and caveats as §9. Patches marked **verified** were applied to the working copy and re-measured; each was also checked for behavioural equivalence.

### 14.1 Errata — these supersede statements in earlier sections

Two of my earlier recommendations were wrong when measured. I am correcting them here rather than leaving them to mislead.

| # | Earlier statement | Measurement | Correct guidance |
| --- | --- | --- | --- |
| E1 | §4.4-1 and §9.2-1: the background save thread "holds the GIL for much of that, so the UI still stutters", and §8.2-6 / §13.3: write **compact `json.dumps`** then one `write()` | UI heartbeat (a 4 ms `QTimer`) while a 36 MB document is saved from a thread: `json.dump(indent=2)` → max gap **7–9 ms**; `json.dump` compact streamed → **5.8 ms**; **`json.dumps` compact → 517–551 ms freeze** (C one-shot encoder never releases the GIL); `orjson` → 36 ms save, max gap **22 ms** | **Do not call `json.dumps` on a large document in the save thread.** The current streaming `json.dump` in `database.write_prepared_save` (`database.py:229`) is *good for responsiveness* (it yields the GIL between chunks), only slow in total (≈550 ms/36 MB). Use `orjson` (15× faster *and* only a \~22 ms hold) or keep streaming `json.dump` with compact separators. §13.3's `dumps_bytes` fallback must stream, see §14.4-A. |
| E2 | §8.3-2: `import ui_main` takes \~390 ms cold, so lazy-import dialogs | With bytecode cached: **76 ms** in-process, **95 ms** as a fresh process (bare `import PyQt6.QtWidgets` alone is 43 ms). The 390 ms included first-time compilation of the `.py` files | Lazy imports save only a few ms; **drop F54's import part**. (Moving `warmup_decode_threadpool` after `show()` is still harmless.) Packaged builds ship `.pyc`, so this is not a startup lever. |
| E3 | §4.4-1: `indent=2` as a meaningful cost | See §9.2-1 and E1: total time is dominated by the volume of base64 text | Unchanged conclusion: reduce volume (F23/F24/F30/F31); `indent=2` itself is not the problem and it keeps the UI responsive. |

### 14.2 Negative result

- **Garbage collector tuning does nothing here.** `load_tree(3000)`: default **925 ms**, `gc.disable()` **906 ms**, `gc.set_threshold(50000, 20, 20)` **963 ms**. Do not spend time on `gc.freeze()`/thresholds.

### 14.3 New findings from profiling

**N9 — Every card builds its own private set of \~25 Qt colour/pen/brush objects.** `cProfile` of `load_tree(1000)` shows `PlantNodeItem._get_cached_palette` (`node_item.py:243–286`) at **0.197 s of 0.318 s** (62%). The method is "cached" per item, but the cache is per *item*, so each of 1,000 cards parses \~15 hex colours into `QColor`, then builds 10 `QPen` and 5 `QBrush` objects. These objects depend only on the scene palette and the two role-line colours, so one set can be shared by every card (the code's own comment already says they "are never mutated afterwards"). Moving them to a module-level memo keyed by `(id(palette), mother_colour, father_colour)`:

| N | `load_tree` before | after (verified) | gain |
| --- | --- | --- | --- |
| 1,000 | 190 ms | **87 ms** | **2.2×** |
| 3,000 | 617 ms | **255 ms** | **2.4×** |

It also removes \~25 Python-owned Qt objects per card from RAM and makes `invalidate_caches` (`:222`) cheap on theme changes (each item now only re-points to the shared set). Patch in §14.4-B.

**N10 — Layout moves every node through the full per-node signal path, so each edge is re-pathed twice or more.** `cProfile` of `auto_layout(3000)`: 5,940 `setPos` calls → `PlantNodeItem.itemChange` (`node_item.py:823`) → `moved.emit` → `LineageScene._on_node_moved` (`canvas_view.py:677`) → **32,518 `EdgeItem.update_path` calls for \~6,000 edges**, i.e. each edge recomputed \~5× (once per endpoint move, plus the centring pass) and `QGraphicsPathItem.setPath` (which re-indexes the item and recomputes its bounding rect) accounts for **4.83 s of 6.24 s** under the profiler. During any bulk operation (`_do_auto_layout` `:1039`, `_do_auto_organize` `:958`, and equally `paste_nodes` `:857`, `undo` `:738`) the per-move edge update is pure overhead. Suppressing it with a flag and re-pathing each touched edge exactly once afterwards:

| N | `auto_layout` before | after (verified) | `auto_organize` before | after (verified) |
| --- | --- | --- | --- | --- |
| 1,000 | 92 ms | **51 ms** | 96 ms | **65 ms** |
| 3,000 | 294 ms | **172 ms** | 359 ms | **180 ms** |

Correctness check: edge geometry after the bulk layout is identical to a fresh `_rebuild_edges()` (`True`). The remaining time is `setPos`/`itemChange` on the cards; a further step would be `setItemIndexMethod(NoIndex)` during the bulk move and one re-index afterwards (not measured).

**N11 — `add_node` rebuilds all edges for a one-node change.** `canvas_view.py:753–762` calls `_rebuild_edges()` (`:639`) which removes and recreates every `EdgeItem`. A new plant only adds edges for its own parents. Incremental version (verified, edge count identical to a full rebuild: 5,996 = 5,996):

| Operation @ 3,000 nodes | before | after |
| --- | --- | --- |
| `add_node` with 2 parents | 2,597 ms (first call after load) | **9.7 ms** |
| `add_node` with no parents | 204 ms | **7.4 ms** (**27×**) |

The first-call figure is a single cold run and is dominated by removing and recreating 6,000 scene items right after a load; treat the second row as the steady-state comparison. The same helper (`_add_edges_for_child`) should be used by `paste_nodes` (`:857`), `apply_connection` (`:807`), and `remove_connection`/`remove_node` (`:814`, `:787`) should remove only the affected edges via `_node_edge_index` instead of rebuilding.

### 14.4 Patches

#### A. `fileio.py` — revised `atomic_write_json` (replaces `dumps_bytes` in §13.3) — proposed

```python
# fileio.py
import os, json, tempfile
try:
    import orjson
except ImportError:
    orjson = None

def atomic_write_json(path: str, obj, *, pretty: bool = False) -> None:
    """Serialise first, write to a temp file in the same folder, fsync, os.replace.
    Never calls json.dumps() on a big object: its C encoder holds the GIL for the
    whole document and freezes the UI when run from the save thread (measured 517 ms)."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            if orjson is not None:
                fh.write(orjson.dumps(obj, option=orjson.OPT_INDENT_2 if pretty else 0))
            else:                                   # streamed: yields the GIL between chunks
                w = io.TextIOWrapper(fh, encoding="utf-8", write_through=False)
                json.dump(obj, w, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))
                w.flush(); w.detach()
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
```

(add `import io`). Call sites: `database.py:177, 229, 293, 340`, `lineage_images.py:75`, `notes_store.py:83` (already atomic, switch to the shared helper), `settings.py:429, 443`, `data_bundle.py:179`; `data_bundle.py:73, 75` use `zf.writestr(..., json.dumps(...))` on small manifests, which is fine. Add `orjson` to `requirements.txt` as an *optional* dependency. With `orjson` the save thread holds the GIL for \~22 ms; without it the streamed fallback keeps the UI at ≤ 9 ms gaps.

#### B. Shared card style (`node_item.py`) — verified

```python
# node_item.py, new module-level helper before `class PlantNodeItem` (line 141)
_STYLE_MEMO: dict = {}

def _shared_style(p, mother, father):
    key = (id(p), mother, father)
    hit = _STYLE_MEMO.get(key)
    if hit is not None and hit[0] is p:
        return hit[1]
    qc = {k: QColor(v) for k, v in p.items() if isinstance(v, str) and v.startswith("#")}
    pens = {
        "node_border": QPen(qc.get("node_border", QColor("#2e4a34"))),
        "node_name":   QPen(qc.get("node_name",   QColor("#e8f5e8"))),
        "text_muted":  QPen(qc.get("text_muted",  QColor("#90b898"))),
        "year_fg":     QPen(qc.get("year_fg",     QColor("#f0b84a"))),
        "tag_border":  QPen(qc.get("tag_border",  QColor("#3a7050"))),
        "tag_fg":      QPen(qc.get("tag_fg",      QColor("#70d888"))),
        "placeholder_icon": QPen(qc.get("placeholder_icon", QColor("#3a6040"))),
        "port":        QPen(qc.get("node_name", QColor("#e8f5e8")), 1.5),
        "mother_role": QPen(QColor(mother)), "father_role": QPen(QColor(father)),
    }
    brushes = {
        "node_bg": QBrush(qc.get("node_bg", QColor("#1a2a1e"))),
        "accent":  QBrush(qc.get("accent",  QColor("#4ecb6a"))),
        "year_bg": QBrush(qc.get("year_bg", QColor("#3d2e00"))),
        "tag_bg":  QBrush(qc.get("tag_bg",  QColor("#1a3a26"))),
        "placeholder": QBrush(qc.get("placeholder", QColor("#1e2e22"))),
    }
    if len(_STYLE_MEMO) > 16: _STYLE_MEMO.clear()
    _STYLE_MEMO[key] = (p, (qc, pens, brushes))
    return _STYLE_MEMO[key][1]

# node_item.py:243–286  _get_cached_palette  -> replace the whole body of the `if` with:
if self._palette_cache is None:
    sc = self.scene()
    p  = sc.theme_palette if sc is not None and hasattr(sc, "theme_palette") else _DEFAULT_PALETTE
    mo = getattr(sc, "mother_line_color", "#e87060") if sc is not None else "#e87060"
    fa = getattr(sc, "father_line_color", "#60a0e8") if sc is not None else "#60a0e8"
    self._palette_cache = p
    self._qcolor_cache, self._pen_cache, self._brush_cache = _shared_style(p, mo, fa)
return self._palette_cache
```

The shared pens/brushes must stay immutable (never call `setWidth`/`setColor` on them in paint code); a grep of `node_item.py` shows none do. A palette dict that is *mutated in place* (instead of replaced) would defeat the `id()`-keyed memo; `set_theme_palette` (`canvas_view.py:1179`) assigns a new dict, which is the case handled here. The accent brush/path caches (`_accent_cache`, `:222`) stay per item because the accent depends on the node.

#### C. Bulk-move guard (`canvas_view.py`) — verified

```python
# canvas_view.py:677  LineageScene._on_node_moved
def _on_node_moved(self, node_id: str) -> None:
    if self._bulk_move:                 # init False in __init__ (line ~394)
        self._drag_dirty = True
        return
    for e in self._node_edge_index.get(node_id, []):
        e.update_path()
    self._drag_dirty = True

# helper (new, next to _on_node_moved)
@contextlib.contextmanager
def _bulk_moving(self):
    self._bulk_move = True
    try:
        yield
    finally:
        self._bulk_move = False
        seen = set()
        for lst in self._node_edge_index.values():
            for e in lst:
                if id(e) not in seen:
                    seen.add(id(e)); e.update_path()

# use it around the bulk bodies:
#   _do_auto_layout   (line 1039)  -> wrap the existing body in `with self._bulk_moving():`
#   _do_auto_organize (line 958)
#   paste_nodes       (line 857), undo (738) when they move >1 node
```

#### D. Incremental edges (`canvas_view.py`) — verified for `add_node`

```python
# new method next to _rebuild_edges (line 639); body is the inner loop of _rebuild_edges for ONE child
def _add_edges_for_child(self, cn) -> None:
    ci = self._node_items.get(cn.id)
    if not ci: return
    for pid in cn.parent_ids:
        pi = self._node_items.get(pid)
        if not pi: continue
        role = cn.parent_roles.get(pid, "")
        if role: self._role_map[pid] = role
        if self.color_code_lines and role == "mother": col = self.mother_line_color
        elif self.color_code_lines and role == "father": col = self.father_line_color
        else: col = pi.node_color()
        e = EdgeItem(pi, ci, col); self.addItem(e); self._edges.append(e)
        self._node_edge_index.setdefault(pid, []).append(e)
        self._node_edge_index.setdefault(cn.id, []).append(e)

# add_node (line 753): replace `self._rebuild_edges()` by
if node.parent_ids:
    self._add_edges_for_child(node)
```

Proposed (not run): in `remove_node` (`:787`), `remove_connection` (`:814`) pop and `removeItem` only the edges in `_node_edge_index[node_id]` / the matching pair, and in `apply_connection` (`:807`) call `_add_edges_for_child(child)` for the new pair; remember to fix the `_role_map` key problem (F12) at the same time.

### 14.5 Fix register additions

| # | Location | Change | Status / gain |
| --- | --- | --- | --- |
| F55 | `node_item.py:243–286`, new helper before `:141` | Shared pens/brushes/colours (§14.4-B) | **verified**, `load_tree` 2.2–2.4× |
| F56 | `canvas_view.py:677, 958, 1039, 857, 738` | Bulk-move guard + one edge update per edge (§14.4-C) | **verified**, `auto_layout` 1.7×, `auto_organize` 1.4–2.0× |
| F57 | `canvas_view.py:753–762`, `:787`, `:807`, `:814`, `:857` | Incremental edge add/remove (§14.4-D) | `add_node` **verified** 204 → 7.4 ms at 3,000 nodes; others proposed |
| F58 | `fileio.py` (new), `database.py:177, 229, 293, 340`, `lineage_images.py:75`, `settings.py:429, 443`, `data_bundle.py:179`, `notes_store.py:83` | `atomic_write_json` using `orjson` or streamed `json.dump`; **never** `json.dumps` of a large document in a thread (E1) | proposed; E1 measured |
| F59 | `requirements.txt`, `build_exe.py` | Add optional `orjson`; include it in the PyInstaller build when available | proposed |
| F60 | `main.py:52–54` | Keep `warmup_decode_threadpool` after `show()`; **do not** bother lazy-importing dialogs (E2) | trivial |

### 14.6 Updated verified-results summary (patched copy vs uploaded zip)

| Metric | Uploaded zip | Patched copy |
| --- | --- | --- |
| Grid view with plants | `NameError` | renders (F1) |
| Canvas frame, 300 cards, zoom 1.0 / 0.3 / 0.05 | 15.9 / 64.2 / 555 ms | 5.7 / 19.9 / 12.7 ms (F17–F19) |
| Scroll step, zoom 0.05 | 553 ms | 14 ms |
| Port hit-test miss, 1,000 nodes | 3.0 ms | 0.010 ms (F20) |
| `load_tree`, 1,000 / 3,000 nodes | 190 / 617 ms | 87 / 255 ms (F55) |
| `auto_layout`, 1,000 / 3,000 | 92 / 294 ms | 51 / 172 ms (F56) |
| `auto_organize`, 1,000 / 3,000 | 96 / 359 ms | 65 / 180 ms (F56) |
| `add_node` @3,000 | 204 ms | 7.4 ms (F57) |
| Failed background save | photos lost | photos restored and re-saved (F2) |
| Year labels after layout | stale | correct (F6) |
| Notes after data-folder switch | old notes | correct (F9) |
| `get_generation` with dangling parent | `ValueError` | `0` (F8) |

Not yet run at the time of this pass: F3/F58 (atomic writes), F4, F5, F7, F23–F33, F37–F54 apart from those listed. Since then F24, F31 and F33 have landed behind the regression scripts in `tools/verification/`; F23, F30, F32 and the remaining items are still outstanding.

---

## 15. Sixth pass: interaction costs, a regression in my own proposal, and a UI-thread parse

Same environment and caveats as §9. This pass measured *interaction* paths (zoom, pan, drag, selection, save-complete handlers) on the original zip and on the patched copy that carries the verified fixes F1–F2, F6–F9, F17–F20, F55–F57. Patches in this section were applied and re-measured unless marked proposed.

### 15.1 Interaction benchmark: original vs patched (300 cards, 560 edges, 1600×1000 viewport)

| Interaction (per step) | Original zip | Patched copy | Note |
| --- | --- | --- | --- |
| Pan step (scroll bar +40 px, render frame), zoom 0.4 | 30 ms | **11 ms** | cached cards blit instead of repaint |
| Drag one card by (5, 3) px, render frame, zoom 0.6 | 19.0 ms | **8.5 ms** | only the moved card and its edges repaint |
| **Zoom step** (×1.08, render frame), zoom 0.4→1.0 | **20 ms** (max 29) | **24 ms** (max 35) | **regression, §15.2** |
| Select 300 cards one by one / `clearSelection` | 33 / 24 ms | 38 / 20 ms | unchanged; selection is not a hotspot |
| `PropertiesPanel.update_node` per selection (`ui_main.py:469`) | 2.1 ms | — | fine, no action |

### 15.2 N12 — `DeviceCoordinateCache` makes zooming \~15–20% slower unless it is suspended while zooming

A device-coordinate cache is valid only for one transform scale; every zoom step invalidates all 300 caches and each card is re-rendered *and* re-stored. Measured at both 300 and 3,000 cards (frame cost depends on visible cards, not total): zoom-step frames **22 ms uncached vs 26 ms cached**. This is a side effect of my F17 recommendation, so F17 must ship together with the guard below.

Mitigation (**verified**): turn item caching off at the start of a wheel zoom, and turn it back on once the wheel has been idle for 180 ms.

| Measurement | Result |
| --- | --- |
| Toggle cache mode on all cards, N = 300 / 3,000 | 0.6 / 1.3 ms (off), 0.1 / 1.4 ms (on) — negligible |
| Zoom steps with the guard | same as uncached (≈22 ms) |
| Re-enable cache + first settled frame / following frames | 14–16 ms / **5.5 ms** |
| Functional check with a real `QWheelEvent` (Ctrl + 120) | cache mode `DeviceCoordinateCache` → `NoCache` during the zoom → back to `DeviceCoordinateCache` 180 ms after the last event; transform scale changed as expected |

Patch (`canvas_view.py`):

```python
# 1. imports (lines 28–31): QTimer is NOT imported in this module today
from PyQt6.QtCore import (
    Qt, QTimer, QRectF, QPointF, QRect, QSize, QSizeF,
    pyqtSignal, pyqtSlot, QRunnable, QThreadPool, QObject,
)

# 2. LineageView, new methods placed directly above _zoom_wheel (line 1532)
def _suspend_item_cache(self) -> None:
    if not getattr(self, "_cache_suspended", False):
        self._cache_suspended = True
        for it in self._scene._node_items.values():
            it.setCacheMode(QGraphicsItem.CacheMode.NoCache)
        if getattr(self, "_cache_timer", None) is None:
            t = self._cache_timer = QTimer(self)
            t.setSingleShot(True); t.setInterval(180)
            t.timeout.connect(self._resume_item_cache)
    self._cache_timer.start()                 # restart on every wheel tick

def _resume_item_cache(self) -> None:
    self._cache_suspended = False
    for it in self._scene._node_items.values():
        it.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
    self.viewport().update()

# 3. _zoom_wheel (line 1545): call it immediately before the transform changes
self._suspend_item_cache()
self.scale(factor, factor)
```

Apply the same call before any other programmatic transform change: `fit_view` (`:1690`) and any `resetTransform()`/`scale()` caller (the only `self.scale(` in `canvas_view.py` is `:1545`; `fit_view` uses `fitInView`). Items created while the guard is active get the cache from `PlantNodeItem.__init__` (F17), which is harmless.

### 15.3 N13 — every background save ends with a full re-parse of the saved file on the UI thread

Call chain (all line numbers verified in the zip):

1. `database.py:229` writes the file; `database.py:231` calls `_invalidate_meta(filepath)`, deleting the mtime cache entry.
2. `ui_main.py:951` (`_on_debounce_save_finished`) → `self._sidebar.touch_file_row(path)` (`ui_main.py:286`).
3. `touch_file_row` → `get_lineage_meta(path)` (`database.py:113`) → cache miss → `json.load(open(path))` of the **whole** `.plf` (including every inline thumbnail) just to read name, count, category, modified date, sort index — all of which are already in memory in `prepared["doc"]`.

Measured on a 300-node lineage with thumbnails (46 MB file): **113–115 ms** of pure UI-thread JSON parsing after *every* debounced save (every 1.5 s of editing/dragging); it grows linearly with file size and with the 1,000-node lineages in §4.4 it would exceed a second.

Fix (**verified**: metadata written at save time compared equal to what the parser produces, `True`): replace the invalidation with a cache update built from the document that was just written.

```python
# database.py, write_prepared_save, replacing lines 231 (_invalidate_meta) — same try block
d = prepared["doc"]
try:
    _meta_cache[filepath] = (os.path.getmtime(filepath), {
        "name":        d.get("name", "Unknown"),
        "description": d.get("description", ""),
        "node_count":  len(d.get("nodes", {})),
        "modified":    d.get("modified_at", "")[:10],
        "category":    d.get("category", ""),
        "sort_index":  int(d.get("sort_index", 0)),
    })
except OSError:
    _invalidate_meta(filepath)
```

The same change belongs in the synchronous `save_lineage` (`database.py:149–189`) which also ends with `_invalidate_meta`. To avoid duplicating the dict, factor it into `_meta_from_doc(d)` and use it in `get_lineage_meta` (`:113–146`) as well, so the two cannot drift. (With F30's `.plf.meta` header this becomes unnecessary for *cold* starts; this fix is the cheap part for the hot path.)

### 15.4 Checked and found fine (no action)

- `PropertiesPanel.update_node`: 2.1 ms per selection.
- Selecting 300 cards programmatically: 33–38 ms total; `clearSelection`: 20–24 ms.
- Python GC tuning (§14.2).
- `ui_main._on_tree_changed` (`:2049`): four cheap UI updates plus a timer restart; `setWindowTitle` per call is an OS call but not measurable here.

### 15.5 Fix register additions

| # | Location | Change | Status / gain |
| --- | --- | --- | --- |
| F61 | `database.py:229–231` (`write_prepared_save`), `:149–189` (`save_lineage`), `:113–146` (`get_lineage_meta`) | Update `_meta_cache` from the document just written instead of `_invalidate_meta`; share one `_meta_from_doc(d)` helper | **verified**; removes a 113–115 ms UI-thread parse per debounced save on a 46 MB lineage |
| F62 | `canvas_view.py:28–31` (add `QTimer`), `:1532–1545` (`_zoom_wheel`), `:1690` (`fit_view`) | Suspend item caching during zoom, resume after 180 ms idle (§15.2) | **verified**; removes the zoom regression caused by F17 (26 → \~22 ms per step) |
| F63 | `ui_main.py:951` (`_on_debounce_save_finished`) | After F61, `touch_file_row` is O(1); also skip the `touch_file_row` loop over `self._list.count()` by keeping a `{path: row}` dict in the sidebar (`ui_main.py:299–316`) | proposed; minor |
| F64 | `canvas_view.py:738` (`undo`) | Restore only nodes whose serialised dict differs from the snapshot (diff by id): update changed items in place, add/remove the rest, reuse pixmaps when `thumb` is unchanged; today it calls `load_tree` (full item rebuild + re-decode of every photo) for a single drag | proposed; not measured end-to-end (rebuild itself measured at 88 ms for 500 nodes plus several seconds of re-decode with photos, §4.1-8) |

### 15.6 Cumulative verified results (uploaded zip → patched copy)

| Metric | Uploaded zip | Patched copy |
| --- | --- | --- |
| Grid view with ≥1 plant | `NameError` | renders |
| Canvas frame, 300 cards, zoom 1.0 / 0.3 / 0.05 | 15.9 / 64.2 / 555 ms | 5.7 / 19.9 / 12.7 ms |
| Pan step | 30 ms (zoom 0.4); 553 ms (zoom 0.05) | 11 ms; 14 ms |
| Drag one card, frame | 19.0 ms | 8.5 ms |
| Zoom step | 20–22 ms | 22 ms with F62 (26 ms without it) |
| `load_tree` 1,000 / 3,000 | 190 / 617 ms | 87 / 255 ms |
| `auto_layout` 1,000 / 3,000 | 92 / 294 ms | 51 / 172 ms |
| `auto_organize` 1,000 / 3,000 | 96 / 359 ms | 65 / 180 ms |
| `add_node` @ 3,000 | 204 ms | 7.4 ms |
| UI-thread work after each debounced save (46 MB lineage) | ≈114 ms parse | O(1) |
| Port hit-test miss @ 1,000 | 3.0 ms | 0.010 ms |
| Failed background save | photos lost | photos restored, re-saved |
| Year labels / notes folder switch / dangling-parent layout | wrong / wrong / exception | correct |

Outstanding at the time of this pass: atomic writes (F3/F58), thumbnail and image-store restructuring (F23, F24, F30, F31), virtualised grid (F33), card-stylesheet consolidation (F32), `upsert_many` (§10.3), undo diffing (F64). Since then F24, F31 and F33 have landed; F23, F30, F32, `upsert_many` and F64 remain the largest next wins.

---

## 16. Seventh pass: more proposals promoted to verified, and the real cost of card selection

Same environment and caveats as §9. This pass implemented several previously "proposed" items in the patched copy and re-measured them against the uploaded zip.

### 16.1 Corrections to earlier sections

| # | Earlier statement | Measurement | Correct guidance |
| --- | --- | --- | --- |
| E4 | §4.2-6, §9.1 row 15: `_select_all` is slow because of the O(N²) `any()` scan (330 ms at 5,000) | Ctrl+A on **1,500 cards from a clean state: 1,774 ms** in the uploaded zip, **1,693 ms** with the `any()` scan already fixed; deselect-all **1,672 / 1,575 ms**. The scan itself is \~29 ms at 1,500 | The dominant cost is `MiniPlantCard._apply_style` (`grid_view.py:801–856`): **≈1.1 ms per card** because each state change runs `setStyleSheet` + `unpolish`/`polish` on the card and its text area. The `any()` fix is still correct but secondary. Fix is §16.3. At 5,000 cards Ctrl+A or Deselect would freeze the UI for \~5–6 s. |
| E5 | §2 C1: an exception in a slot/virtual is only logged by the app's excepthook | While testing the painted card, a `NameError` inside `paintEvent` ended the test process with **"Aborted"** (no excepthook installed in my harness). PyQt6 calls `qFatal()` for unhandled exceptions in virtual overrides unless `sys.excepthook` is replaced | The app installs a hook in `log_setup.py`, so the user-visible symptom of C1 depends on that hook; either way the grid never renders. Keep the hook, and add `faulthandler.enable()` plus a test that exercises every `paintEvent` path (the pytest-qt checklist in §13.14). |

### 16.2 Items promoted from "proposed" to **verified**

| Fix | Change | Result (uploaded zip → patched copy) |
| --- | --- | --- |
| F3 / F58 `fileio.atomic_write_json` wired into `database.save_lineage` (`:176`), `database.write_prepared_save` (`:229`), `lineage_images.save_all_images` (`:74`), `settings.save_settings` (`:429`) | temp file + `fsync` + `os.replace`, serialise first, `orjson` when available else streamed `json.dump`, per-path lock | **Unserialisable value mid-save: old file byte-identical `False` → `True`**, no stray `.tmp` files; round trip load OK. **46 MB lineage save: 535 ms (indent=2) → 68 ms** with `orjson` |
| F25 `plant_db.search` haystack cache (`plant_db.py:575–596`) | `_hay[id] = (meta, lowercased haystack)` keyed on the identity of the cached meta dict, so `upsert` (which replaces the dict) invalidates automatically | `search("plant 042")` at 5,000 plants (100 hits, includes building 100 `PlantNode`s): **3.72 → 0.85 ms**; a rename is found immediately and the old name no longer matches |
| F5 `_select_all` respects the filter (`grid_view.py:1928–1936`, `_rebuild_grid` `:1616`) | `self._visible_plants = plants` stored after filtering/sorting, used by `_select_all` | with a search showing 10 of 1,500 plants: Ctrl+A selects **1,500 → 10** |
| F38 resize only when column count changes (`grid_view.py:1817`, `_rebuild_grid` `:1661`) | compare `n_cols` before starting `_filter_timer` | four small resizes: **4 → 0 rebuilds**; a resize that changes the column count: still 1 |
| F37 keep thumbnails on theme change (`grid_view.py:952`) | remove `QPixmapCache.clear()` | probe entry survives `apply_card_settings(palette=…)`: `False` → `True` |

Implementation notes for `atomic_write_json`: it falls back from `orjson` to the stdlib encoder when `orjson` raises `TypeError` (so the stdlib error text is what you see in the log); `OPT_NON_STR_KEYS` is set because `tree.to_dict()` may contain non-string keys; `.plf` files become compact (not indented) JSON — `json.load` reads both, so old and new files interoperate, but hand-editing a `.plf` is less convenient. `orjson` serialises `NaN`/`Infinity` as `null`.

### 16.3 N14 — card chrome should be painted, not restyled

`MiniPlantCard._apply_style` (`grid_view.py:801–856`) builds one of four stylesheets (normal/hover/selected/selected+hover) and calls `setStyleSheet`, then explicitly `unpolish`/`polish`es the card and its `card_text_area` child. It runs on hover enter/leave (`:858–862`), on selection (`set_selected`, `:854`), on every selection-badge change, and for each visible card on rebuild (guarded by `_style_state`, which prevents repeats but not first-time changes).

Microbenchmark (1,500 cards, 3 labels each): **(a)** current approach, `setStyleSheet` per card **451 ms**; **(b)** dynamic property + one container stylesheet + re-polish **121 ms**; **(c)** `paintEvent`-driven state + `update()` **33 ms** (13.7×).

Implemented in the real class and measured with the real grid (1,500 plants):

| Operation | Uploaded zip | With painted chrome |
| --- | --- | --- |
| Ctrl+A on 1,500 cards (clean state) | 1,774 ms | **523 ms** (3.4×) |
| Deselect all | 1,672 ms | **331 ms** (5.0×) |

The remaining \~0.3 s is Qt repainting 1,500 widgets and the selection-badge update; it disappears entirely with the virtualised grid (F33), where selection is a model operation and only visible rows repaint.

Visual check: I rendered the grid in the original and the patched copy (selected and unselected cards) and compared the images side by side. The chrome matches: same border radius and colours, selected border in the accent colour, text panel with rounded bottom corners. The only visible difference is a 1–2 px shift of the text block, caused by the stylesheet border previously insetting the layout; I compensated with `lay.setContentsMargins(2, 2, 2, 2)`.

Patch (`grid_view.py`):

```python
# 1. imports (line 31): add QRectF; (line 30) QPen is already imported
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, QPoint, QRectF

# 2. new class directly above `class MiniPlantCard` (line 557)
class _CardTextArea(QWidget):
    """Text panel with rounded bottom corners, painted directly."""
    def __init__(self):
        super().__init__(); self._c = QColor("#1c2a1c")
    def set_color(self, c: QColor) -> None:
        if c != self._c: self._c = c; self.update()
    def paintEvent(self, event) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect(); p.setClipRect(r); p.setPen(Qt.PenStyle.NoPen); p.setBrush(self._c)
        p.drawRoundedRect(QRectF(r.left(), r.top() - 10, r.width(), r.height() + 10), 8, 8)
        p.end()

# 3. MiniPlantCard.__init__ (line 580–581): replace the stylesheet cache by the chrome tuple
self._style_state = None
self._chrome = (QColor("#1a2a1e"), QColor("#2e4a34"), 2)      # (background, border, border width)

# 4. MiniPlantCard._build: layout margins (line 612) and the text area class (line 632)
lay.setContentsMargins(2, 2, 2, 2)
...
text_w = _CardTextArea()           # was: QWidget(); keep setObjectName("card_text_area")

# 5. replace _apply_style (lines 801–856) and add paintEvent
def _apply_style(self, hovered: bool, selected: bool) -> None:
    state = (bool(hovered), bool(selected))
    if state == self._style_state: return
    self._style_state = state
    accent   = self._pal.get("accent",      "#4ecb6a")
    bg       = self._pal.get("node_bg",     "#1a2a1e")
    border   = self._pal.get("node_border", "#2e4a34")
    panel_bg = self._pal.get("panel_bg2",   "#1c2a1c")
    bw = 2
    if selected:   border, bw = accent, 3; bg, panel_bg = QColor(accent).darker(300), QColor(accent).darker(350)
    elif hovered:  border = accent
    self._chrome = (QColor(bg), QColor(border), bw)
    self._text_area.set_color(QColor(panel_bg))
    self.update()

def paintEvent(self, event) -> None:
    p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    bgc, bc, bw = self._chrome
    p.setBrush(bgc); p.setPen(QPen(bc, bw)); h = bw / 2.0
    p.drawRoundedRect(QRectF(self.rect()).adjusted(h, h, -h, -h), 10, 10)
    p.end()
```

Follow-ups: `apply_card_settings` (`:941`) must reset `card._style_state = None` for cached cards after a palette change so the next `_apply_style` repaints with the new colours (it already clears `_card_cache`, so new cards start correct). The per-label `setStyleSheet` calls (F32, `:624–680`) remain separate and are still worth doing.

### 16.4 Dead code worth deleting

| Location | What | Why |
| --- | --- | --- |
| `settings.py:434–455` (`_UNDO_FILE`, `save_undo_history`, `load_undo_history`) | persists up to 50 full-tree dicts (with inline thumbnails) to `undo_history.json` | never called anywhere in the project; if it were wired up it would be a large synchronous write per edit. Delete |
| `grid_view.py:1274` | first of two `_delete_selected_table_rows` | shadowed by `:1890` (F13) |
| `grid_view.py:1553` | `_dedup_plants` | unused, and has an operator-precedence bug (F14) |
| `plant_db.py:99–107` | `_revision`, `_bump_revision_locked`, `get_revision` | never called (F26): either wire them in or delete them |
| `_perf_smoke_test.py`, `_perf_bench.py` | benchmark scripts shipped in the app folder | move to `tools/`; they import private module variables and don't seed photos |

### 16.5 Fix register additions

| # | Location | Change | Status / gain |
| --- | --- | --- | --- |
| F65 | `grid_view.py:557` (new `_CardTextArea`), `:580–581`, `:612`, `:632`, `:801–856` | Painted card chrome (§16.3) | **verified**; Ctrl+A 1,774 → 523 ms, deselect 1,672 → 331 ms per 1,500 cards; hover enter/leave no longer re-polish |
| F66 | `settings.py:434–455` | Delete the unused undo-history persistence | trivial |
| F67 | `grid_view.py:941–960` | After a palette change reset `_style_state` on cached cards | proposed; needed with F65 |
| F68 | `database.py:176, 229`, `lineage_images.py:74`, `settings.py:429` and others listed in F3 | `atomic_write_json` | **verified** (§16.2) |
| F69 | `requirements.txt`, `build_exe.py:171–252` | Add `orjson`; bundle it | proposed |

### 16.6 What the patched copy now contains

Verified and applied: F1, F2, F3/F58 (for `.plf`, sidecar and settings), F5, F6, F8, F9, F17, F18, F19, F20, F24, F25, F31, F33, F37, F38, F55, F56, F57 (`add_node`), F61, F62, F65. Remaining items are proposals; the largest are the two-size thumbnail split (F23), `.plf.meta` headers (F30), card stylesheet consolidation (F32), `upsert_many`, undo diffing (F64) and the remaining non-`.plf` write sites (`data_bundle.py:179`, `notes_store.py:83`, `database.py:293, 340`).

To reproduce any number in this document you need only Python 3.12+, `PyQt6`, `orjson` and `QT_QPA_PLATFORM=offscreen`; each experiment redirected the data directory to a temp folder so no real data was touched.

---

## 17. Final status, supersession map and next steps

This section closes the document. It is the **authoritative status** for every fix ID; where an earlier section disagrees with it, this section (and §11–§16 for line numbers and measurements) wins.

### 17.1 Implementation status of every fix ID

Legend: **Applied + tested** = in `BreedingCharts_v1_24_perf_patched.zip` and exercised by a headless script in `tools/verification/`; **Applied** = in the zip, simple enough that only a code-level check was done; **Proposal** = described in this document with file and line references but *not* in the zip.

| Status | Fix IDs |
| --- | --- |
| **Applied + tested** | F1 (grid constants), F2 (failed-save photo restore), F3/F58/F68 (atomic writes for `.plf`, sidecar, settings), F5 (select-all respects filter), F6 (year-row revision), F7 (open does not write), F8 (`get_generation`), F9 (notes cache), F10 (sheet parents), F15 (decode token), F17–F19 (card cache, background cache, dot guard), F20 (port hit-test), F22 (scaled JPEG decode), F24 (thumbnail BLOB table, batch `get_thumbs`), F25 (search haystacks), F26 (revision wiring), F27 (`remove_many`), F29 (coalesced settings), F31 (per-node SQLite image store, sidecar-first saves, read-connection reuse), F33 (virtualised rows, progressive build, bounded card recycling), F37, F38, F45, F51 (JPEG for photos), F55, F56, F57 (`add_node`), F61, F62, F65 (painted card chrome) |
| **Applied** | F4 (wait for running save), F13, F14, F16, F41, F48, F66, F59/F69 (`orjson` listed in `requirements.txt`) |
| **Proposal** | F11 (barycenter), F12 (role-map key), F21 (`IMG_SCALE`), F23 (two-size thumbnail pair), F28 (evict after lineage upsert), F30 (`.plf.meta` header), F32 (card stylesheets → container sheet), F34/F35/F36 (paint micro-caches), F39/F40 (grid decode pool, event filters), F42 (incremental edge updates beyond `add_node`), F43/F64 (undo diffing), F44 (edge paint caches), F46 (comment `tree_changed`), F47 (`_ensure_conn` lock), F49 (settings diffing), F50 (autocomplete cache), F52 (notes storage), F53 (`build_exe.py`), F54 (post-show warm-up), F60, F63, F67 |

Why the proposals were left out of the zip: each one changes a data format, a storage layout, a widget architecture, or the visual quality of the canvas, so each needs an owner decision and a migration or regression test rather than a drop-in patch. F21 in particular trades image sharpness at high zoom for 4× less RAM; it should be a setting, not a silent change.

### 17.2 Supersession map (what to trust)

| Earlier statement (section) | Superseded by |
| --- | --- |
| Approximate line numbers in §2–§10 | Verified line numbers in §11.2, §12, §13, §14–§16 |
| "Background `json.dump` stutters the UI" (§4.4-1, §9.2-1) | E1 (§14.1): streaming `json.dump` keeps the UI responsive; one-shot `json.dumps` freezes it |
| "Write compact `json.dumps` then one write" (§8.2-6, §13.3) | §14.4-A and §16.2: `atomic_write_json` (orjson or streamed) |
| "`import ui_main` costs \~390 ms; lazy-import dialogs" (§8.3-2, F54) | E2 (§14.1): \~95 ms with bytecode cached; do not bother |
| "`_select_all` is slow because of an O(N²) scan" (§4.2-6, §9.1 row 15) | E4 (§16.1): the dominant cost is per-card restyling (≈1.1 ms/card), fixed by F65 |
| "Exception in C1 is only logged" (§2 C1) | E5 (§16.1): PyQt6 aborts on unhandled exceptions in virtual methods unless an excepthook is installed (the app installs one) |
| "Render hints / optimisation flags help" (§8.4) | §9.2-2: no measurable effect; item caching is what works |
| "Sidebar/PlantDB filter rebuild is slow" (§8.2-2) | §9.2-4: 2 ms for 5,000 items, not a priority |
| Status columns in §12 and §13.1 saying "proposed" for items now in the zip | §17.1 |
| **E6 (new).** "Atomic save of a 46 MB lineage: 68 ms" (§16.2) | That was a best case on a quiet disk. Re-measured breakdown (4 runs): `orjson.dumps` 25–37 ms (one 264 ms outlier from machine noise), file write 12–300 ms, `fsync` 41–74 ms, `os.replace` 0–28 ms; **total roughly 100–450 ms** versus 550–630 ms for the original unsynchronised `json.dump(indent=2)`. The CPU part is \~30 ms; the rest is storage latency, which is the price of crash-safety. Expect 1.3–5× faster than before depending on the disk, not a fixed 8× |

### 17.3 Verified results, final form (uploaded zip → patched zip)

All numbers from the same sandbox; variance between runs on this shared machine is roughly ±25%, so read these as ratios.

| Area | Uploaded zip | Patched zip |
| --- | --- | --- |
| Grid view with any plant | `NameError` | renders |
| Canvas frame, 300 cards, zoom 1.0 / 0.3 / 0.05 | 15.9 / 64.2 / 555 ms | 5.7–7.5 / 16–20 / 12–13 ms |
| Canvas pan step (zoom 0.05) | 553 ms | 12–14 ms |
| Canvas drag frame / pan step (zoom 0.4–0.6) | 19 / 30 ms | 8.5–12.5 / 11–15 ms |
| Mouse-move port test, 1,000 nodes | 3.0 ms | 0.004–0.010 ms |
| `load_tree` 1,000 / 3,000 nodes | 190 / 617 ms | 87–95 / 255–272 ms |
| `auto_layout` 1,000 / 3,000 | 92 / 294 ms | 51–59 / 172–196 ms |
| `add_node` @ 3,000 | 204 ms | 7–30 ms (machine-load dependent; edge set identical) |
| Ctrl+A / deselect on 1,500 grid cards | 1,774 / 1,672 ms | 523–666 / 331–504 ms |
| Ctrl+A with an active search | selects all 1,500 (incl. hidden) | selects the 10 visible |
| 4 small window resizes | 4 grid rebuilds | 0 |
| Theme change | clears thumbnail cache | keeps it |
| Re-showing the grid without DB changes | rebuild | skipped (revision unchanged) |
| Opening a lineage | marks dirty, writes file 1.5 s later, pushes undo | no write, no undo entry |
| Five rapid theme/setting changes | 5 synchronous settings writes | 1 write |
| Delete 25 selected plants | 25 delete transactions | 1 `remove_many` |
| Thumbnail from a 12 MP JPEG | 140–155 ms | 73–84 ms (same 1200×900, mean channel difference 1.2/255) |
| Pasted photo format | PNG always | JPEG q92 unless real transparency |
| Unserialisable value mid-save | existing file destroyed | existing file byte-identical |
| Failed background save | photos lost | photos restored and re-saved on retry |
| Year labels / notes folder switch / dangling-parent layout | wrong / wrong / exception | correct |
| Save of a 46 MB lineage | 535–570 ms (no fsync) | \~100–450 ms (with fsync, atomic) |
| UI-thread parse after each background save (46 MB) | 113–132 ms | 0 |

Post-audit implementation measurements (current repo, 600 plants / 80 notes / 120 lineage nodes unless noted, offscreen 1400×900): grid scroll 60 px × 40 steps 38.5 ms median (first 161.8 ms); 50% scroll jump + settle 30.6 ms (first 82.4 ms); 1,500 plants stay at 18 live cards with a 41,114 px scroll extent; a 120-plant export rendered 1,892×3,646 px, restored 45 live cards and decoded 3 off-screen photos; `save_lineage` 17.8 ms and `load_lineage` 3.1 ms. These replace the earlier grid and persistence estimates where they conflict.

### 17.4 What to do next, in order

1. **Try the patched zip on a copy of your data** (back up the data folder first). Open your lineages, switch to Grid and Spreadsheet, select/delete a few plants, paste a photo, zoom and pan a large lineage, and quit and relaunch. The things most worth eyeballing: card selection/hover look in the grid (F65), no layout change on open (F7), and photos surviving a save/reopen (F2, F51).
2. **Add `orjson`** (`pip install orjson`) to the environment you build from, then add it to the PyInstaller build (F53/F59). Without it saves still work but use the slower streamed encoder.
3. **Decide on three behaviours**: (a) whether "auto-layout on open" should keep re-laying-out every open (current) or only for lineages without stored positions; (b) whether the canvas image resolution (`IMG_SCALE`, F21) should become a setting; (c) whether `.plf` files may stay compact (not hand-editable).
4. **Continue the structural work, in this order**, each behind the regression checklist in §13.14 and the scripts in `tools/verification/`. F24, F31 and F33 are now done:
   1. F30 `.plf.meta` header (small, safe, large win for big libraries);
   2. F23 two-size thumbnail pair (further RAM/startup relief for photo databases);
   3. F32 card stylesheet consolidation;
   4. F42/F44/F64 incremental edges, edge paint caches, undo diffing;
   5. F11/F12 correctness fixes in auto-organise and the role map.
5. **Keep the benchmarks**: `tools/verification/` plus a photo-seeded stress dataset (§8.5) turns every regression in this document into a failing test.

### 17.5 Limits of this audit

- All measurements were headless on a shared Linux sandbox with Qt's software rasteriser and synthetic photos; Windows, GPU-backed viewports, antivirus/OneDrive file locking, and your real data volumes were not tested.
- The zip was exercised by scripts and a boot-and-switch-views smoke test, not by an interactive session; visual parity of the painted card chrome was checked on rendered screenshots (selected and unselected) with a 1–2 px text offset noted.
- Several estimates (e.g. QPixmapCache accounting for item caches, undo-diff gains, per-node image store at application level) are labelled as such where they appear.
- Line numbers refer to the uploaded `BreedingCharts_v1_24_notes_overhaul` zip; the patched zip's line numbers differ slightly.
