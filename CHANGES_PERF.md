# Performance / safety patches applied to BreedingCharts v1.24 (experimental)

Each item corresponds to an ID in the audit document ("BreedingCharts v1.24 — Performance & Defect Audit").
All were exercised headless (Qt offscreen) against the original zip (scripts in tools/verification/).
None has been run on a real Windows desktop with your data. **Back up your data folder before trying this build.**

| ID | File(s) | Change |
|----|---------|--------|
| F1  | grid_view.py | define `_CARDS_FIRST_PAINT` / `_CARDS_PER_TICK` (grid raised NameError) |
| F2  | ui_main.py | failed background save restores evicted photos into RAM so the retry re-saves them |
| F3/F58 | fileio.py (new), database.py, lineage_images.py, settings.py | atomic writes (temp + fsync + os.replace); orjson if installed, else streamed json |
| F4  | ui_main.py | `_save_current_silently` waits for a running background save (no two writers) |
| F5  | grid_view.py | Ctrl+A selects only the visible (filtered) plants; O(N) membership |
| F6  | canvas_view.py | year-label memo keyed on a revision counter (labels were stale) |
| F7  | ui_main.py, canvas_view.py | opening a lineage no longer marks it dirty / writes it / pushes an undo entry (layout still runs in memory) |
| F8  | models.py | `get_generation` no longer raises on dangling parent ids |
| F9  | notes_store.py | `reset_cache` really resets the sorted cache |
| F10/F13/F14/F41 | grid_view.py | sheet parent columns, duplicate method removed, dedup precedence, O(1) id lookups |
| F15 | canvas_view.py | per-item decode token: stale image-decode results are ignored |
| F16 | main.py | single `__version__` (1.24) for log line and application version |
| F17-F20 | node_item.py, canvas_view.py | DeviceCoordinateCache on cards, CacheBackground, grid-dot density guard, port hit-test via scene.items(rect) |
| F22 | image_utils.py | thumbnails decoded with QImageReader scaled decode (~2x faster, same dimensions) |
| F25 | plant_db.py | cached lower-case search haystacks |
| F26 | plant_db.py, grid_view.py | metadata revision counter wired in; grid skips rebuild when the DB is unchanged |
| F27 | grid_view.py | bulk deletes use one `remove_many` transaction |
| F11 | canvas_view.py | barycenter sweep uses working positions updated after every row sort, so later passes actually improve crossings; stops early when stable |
| F28 | ui_main.py | lineage-side upserts evict the plant's base64 image list after the DB rows are flushed (saves RAM; no feature change) |
| F29 | ui_main.py | settings writes coalesced (500 ms); direct write on dialog accept and on close |
| F30 | database.py, data_bundle.py, ui_main.py | atomic `<file>.plf.meta` sidecar; sidebar metadata reads ~1 ms even for 46 MB files, with mtime fallback and deletion on lineage delete |
| F37/F38 | grid_view.py | keep thumbnails on theme change; skip re-flow when column count unchanged |
| F39 | grid_view.py | image-decode queue uses `collections.deque.popleft()` (O(1) instead of O(n)) |
| F42 | canvas_view.py | connect/disconnect/remove/delete/paste update only affected edges instead of rebuilding all N edges |
| F44 | canvas_view.py | arrow path and 12 px hit shape cached per edge; QBrush created lazily (no per-frame/per-edge allocation) |
| F45 | canvas_view.py | `fit_view` iterates node items, not `scene.items()` |
| F46 | canvas_view.py | comment drag/resize emits one `tree_changed` on release instead of every mouse-move |
| F47 | plant_db.py | `_ensure_conn()` takes the re-entrant DB lock, preventing concurrent open/migrations |
| F49 | ui_main.py | `_apply_settings` diffs old/new settings and only re-applies styles/scene/grid/sidebar sections that changed |
| F50 | dialogs.py | `AutocompleteDB` caches its lists and invalidates on `plant_db.get_revision()` |
| F48 | database.py | `get_data_dir` calls `makedirs` once per directory |
| F51 | image_utils.py, ui_main.py, node_item.py, dialogs.py | pasted/dropped photos stored as JPEG q92 (PNG only if the image really has transparency) |
| F55 | node_item.py | one shared pen/brush/colour set for all cards |
| F56 | canvas_view.py | bulk-move guard: one edge update per edge during auto-layout / auto-organize |
| F57 | canvas_view.py | incremental edge creation in `add_node` |
| F61 | database.py | sidebar metadata cache filled at save time (no re-parse of the saved file) |
| F62 | canvas_view.py | item caches suspended while zooming, resumed after 180 ms idle |
| F63 | ui_main.py | sidebar keeps a `{path: row}` map for O(1) `touch_file_row` updates |
| F65 | grid_view.py | card chrome painted in paintEvent instead of setStyleSheet/polish |
| F66 | settings.py | removed unused undo-history persistence |

Optional dependency: `pip install orjson` (much faster saves; falls back to streamed json without it).

## NOT included (design changes with higher regression risk — see the document)
Thumbnail/metadata split (F24), per-node image store (F31), virtualised grid (F33), `upsert_many`,
undo diffing (F64), role-map key (F12), card stylesheet consolidation (F32), build_exe changes (F53),
IMG_SCALE reduction (F21, a visual-quality trade-off), enum/elide micro-caches (F34/F35),
grid decode pool and per-card event-filter removal (F39/F40 remainder).

## Behaviour changes to be aware of
* `.plf` and `.plf.images` files are written as compact JSON (older builds can still read them).
* Pasted/dropped photos are stored as JPEG instead of PNG unless they contain real transparency.
* Opening a lineage no longer rewrites it. "Auto-layout on open" still re-lays-out in memory each time, so a
  hand-arranged layout is only persisted if you then edit something.
* Settings are written ~0.5 s after the last change (and immediately on close).
* The Grid View card border/selection is painted by the card itself; appearance matches the old look within 1-2 px.

Verification scripts: tools/verification/ (headless; see its README).
