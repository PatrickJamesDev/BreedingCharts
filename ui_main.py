"""
ui_main.py — Main application window.

v1.2.0 additions:
  • Sidebar: sort dropdown (A-Z, Z-A, Newest, Oldest, Most Plants, Custom)
  • Sidebar: drag-to-reorder lineages (custom sort mode, persisted)
  • Sidebar: category grouping with section headers
  • Sidebar: right-click → Set Category
  • Tour: highlights relevant toolbar buttons on each step
  • Export PNG: applies watermark if enabled in settings
  • Ctrl+V in PlantDialog pastes clipboard image anywhere (handled in dialogs.py)
  • Background thread loader for large .plf files with progress indicator
  • Export PNG wires watermark_text / watermark_opacity from settings
"""
from __future__ import annotations
import base64, logging, os
from typing import Optional, List

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QListWidget, QListWidgetItem,
    QToolBar, QStatusBar, QFileDialog, QMessageBox, QInputDialog,
    QFrame, QApplication, QLineEdit, QDialog, QComboBox, QGroupBox,
    QStackedWidget, QProgressBar,
)
from PyQt6.QtGui import QAction, QKeySequence, QFont, QColor, QPixmap
from PyQt6.QtCore import (
    Qt, QSize, QTimer, QPointF, pyqtSignal, QThread, pyqtSlot, QObject, QByteArray,
)
import perf

from models import LineageTree, PlantNode, CommentNote
from database import (
    save_lineage, load_lineage, get_lineage_files,
    get_lineage_meta, default_filepath, export_view_to_png,
    save_sidebar_order, load_sidebar_order, apply_sidebar_order,
    get_data_dir, set_data_dir, prepare_save, write_prepared_save,
)
from canvas_view import LineageScene, LineageView
from node_item import PlantNodeItem
from dialogs import (
    PlantDialog, NewLineageDialog, ConnectDialog,
    AboutDialog, PlantDBDialog, RoleDialog, AutocompleteDB,
    BatchEditDialog, DuplicateDetectorDialog,
    CrossLineageConnectDialog, PedigreeCertDialog, ExportBundleDialog,
)
from grid_view import PlantGridView
from notes_view import NotesGridView
import notes_store
from settings import (
    AppSettings, load_settings, save_settings, build_stylesheet, THEMES, SettingsDialog,
)
from tour import TourDialog
import plant_db
import data_bundle

from ui_common import flash_status

log = logging.getLogger(__name__)


# ── Background loader ─────────────────────────────────────────────────────────

class _LoadWorkerSignals(QObject):
    finished = pyqtSignal(object, str)   # (LineageTree | None, filepath)
    error    = pyqtSignal(str)


class _LoadWorker(QThread):
    """Loads a .plf file in a background thread to keep the UI responsive."""
    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self._filepath = filepath
        self.signals   = _LoadWorkerSignals()

    def run(self) -> None:
        try:
            tree = load_lineage(self._filepath)
            self.signals.finished.emit(tree, self._filepath)
        except Exception as e:
            self.signals.error.emit(str(e))


# ── Background debounced-save worker ───────────────────────────────────────
# The 1.5s debounced autosave can fire mid-interaction (mid-drag, mid-typing
# in a dialog), so unlike the explicit Save/Save-As actions, it should never
# block the UI thread — even on the rarer save that also has to read+rewrite
# the images sidecar. prepare_save() (fast, main-thread) builds a plain-data
# snapshot; this worker does the actual disk I/O off-thread.

class _SaveWorkerSignals(QObject):
    finished = pyqtSignal(bool)   # ok


class _SaveWorker(QThread):
    def __init__(self, prepared: dict, filepath: str, parent=None):
        super().__init__(parent)
        self._prepared = prepared
        self._filepath = filepath
        self.signals   = _SaveWorkerSignals()

    def run(self) -> None:
        try:
            ok = write_prepared_save(self._prepared, self._filepath)
        except Exception as e:
            log.error("_SaveWorker: %s", e)
            ok = False
        self.signals.finished.emit(ok)


# ── Sidebar ───────────────────────────────────────────────────────────────────

class DatabaseSidebar(QWidget):
    open_requested          = pyqtSignal(str)
    new_lineage_requested   = pyqtSignal()
    delete_lineage_requested= pyqtSignal(str)    # restored v1.2.0
    rename_lineage_requested= pyqtSignal(str)    # restored v1.2.0
    set_category_requested  = pyqtSignal(str)    # v1.2.0
    sort_changed             = pyqtSignal(str)    # v1.3.1 — so MainWindow can persist the choice

    # Sort-mode constants
    SORT_NAME_ASC   = "name_asc"
    SORT_NAME_DESC  = "name_desc"
    SORT_DATE_DESC  = "date_desc"
    SORT_DATE_ASC   = "date_asc"
    SORT_PLANTS     = "plants_desc"
    SORT_CUSTOM     = "custom"

    _SORT_LABELS = [
        ("A → Z",          SORT_NAME_ASC),
        ("Z → A",          SORT_NAME_DESC),
        ("Newest first",   SORT_DATE_DESC),
        ("Oldest first",   SORT_DATE_ASC),
        ("Most plants",    SORT_PLANTS),
        ("Custom (drag)",  SORT_CUSTOM),
    ]

    def __init__(self, sort_mode: str = SORT_NAME_ASC,
                 show_categories: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar_panel")
        self.setFixedWidth(245)
        self._all_files:     list  = []   # [(path, meta)]
        self._sort_mode      = sort_mode
        self._show_categories= show_categories
        self._custom_order:  List[str] = load_sidebar_order()
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 10, 8, 8)
        lay.setSpacing(4)

        # Header row
        hdr_frame = QFrame()
        hdr_frame.setFixedHeight(34)
        hdr_frame.setStyleSheet("background: transparent;")
        hdr_lay = QHBoxLayout(hdr_frame)
        hdr_lay.setContentsMargins(2, 0, 2, 0); hdr_lay.setSpacing(4)
        hdr_icon = QLabel("📁")
        hdr_icon.setStyleSheet("font-size:14px; background:transparent; padding:0;")
        hdr_icon.setFixedSize(22, 26)
        hdr_text = QLabel("Lineage Database")
        hdr_text.setObjectName("panel_header")
        hdr_text.setStyleSheet(
            "font-weight:bold; font-size:13px; background:transparent; padding:0;")
        hdr_lay.addWidget(hdr_icon)
        hdr_lay.addWidget(hdr_text, 1)
        lay.addWidget(hdr_frame)

        # Search bar
        self._search = QLineEdit()
        self._search.setObjectName("sidebar_search")
        self._search.setPlaceholderText("🔍  Filter…")
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)

        # Sort combo
        sort_row = QHBoxLayout(); sort_row.setContentsMargins(0,0,0,0); sort_row.setSpacing(4)
        sort_lbl = QLabel("Sort:")
        sort_lbl.setObjectName("sidebar_caption")
        sort_lbl.setFixedWidth(28)
        sort_row.addWidget(sort_lbl)
        self._sort_combo = QComboBox()
        self._sort_combo.setObjectName("sidebar_sort_combo")
        self._sort_combo.setFixedHeight(24)
        for label, key in self._SORT_LABELS:
            self._sort_combo.addItem(label, key)
        # Restore saved mode
        for i, (_, key) in enumerate(self._SORT_LABELS):
            if key == self._sort_mode:
                self._sort_combo.setCurrentIndex(i); break
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        sort_row.addWidget(self._sort_combo, 1)
        lay.addLayout(sort_row)

        # List widget — drag-drop enabled for custom ordering
        self._list = QListWidget()
        self._list.setSpacing(1)
        self._list.itemClicked.connect(self._on_open)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        lay.addWidget(self._list)

        # Bottom button row
        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        btn_new = QPushButton("➕ New")
        btn_new.setObjectName("sidebar_action_btn")
        btn_new.setFixedHeight(26)
        btn_new.clicked.connect(self.new_lineage_requested.emit)
        btn_ref = QPushButton("⟳ Refresh")
        btn_ref.setObjectName("sidebar_action_btn")
        btn_ref.setFixedHeight(26)
        btn_ref.clicked.connect(self.refresh)
        btn_row.addWidget(btn_new); btn_row.addWidget(btn_ref)
        lay.addLayout(btn_row)
        self.refresh()

    # ── Sort ──────────────────────────────────────────────────────────────────

    def set_sort_mode(self, mode: str) -> None:
        self._sort_mode = mode
        for i, (_, key) in enumerate(self._SORT_LABELS):
            if key == mode:
                self._sort_combo.setCurrentIndex(i); break
        self._filter(self._search.text())

    def _on_sort_changed(self, idx: int) -> None:
        self._sort_mode = self._sort_combo.currentData()
        # Enable/disable drag based on mode
        can_drag = (self._sort_mode == self.SORT_CUSTOM)
        mode = (QListWidget.DragDropMode.InternalMove
                if can_drag else QListWidget.DragDropMode.NoDragDrop)
        self._list.setDragDropMode(mode)
        self._filter(self._search.text())
        self.sort_changed.emit(self._sort_mode)

    def _sorted_files(self, files: list) -> list:
        """Sort (path, meta) pairs according to current sort mode."""
        m = self._sort_mode
        if m == self.SORT_NAME_ASC:
            return sorted(files, key=lambda x: x[1]["name"].lower())
        elif m == self.SORT_NAME_DESC:
            return sorted(files, key=lambda x: x[1]["name"].lower(), reverse=True)
        elif m == self.SORT_DATE_DESC:
            return sorted(files, key=lambda x: x[1]["modified"], reverse=True)
        elif m == self.SORT_DATE_ASC:
            return sorted(files, key=lambda x: x[1]["modified"])
        elif m == self.SORT_PLANTS:
            return sorted(files, key=lambda x: x[1]["node_count"], reverse=True)
        elif m == self.SORT_CUSTOM:
            # Use custom order; unknowns go last
            order_map = {name: i for i, name in enumerate(self._custom_order)}
            return sorted(files, key=lambda x: order_map.get(
                os.path.basename(x[0]), len(self._custom_order)))
        return files

    def _on_rows_moved(self, *_) -> None:
        """User dragged a row — save the new order."""
        if self._sort_mode != self.SORT_CUSTOM:
            return
        order = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item:
                path = item.data(Qt.ItemDataRole.UserRole)
                if path:
                    order.append(os.path.basename(path))
        self._custom_order = order
        save_sidebar_order([p for p, _ in self._all_files
                            if os.path.basename(p) in set(order)])

    # ── Refresh / filter ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        self._all_files = []
        for path in get_lineage_files(get_data_dir()):
            self._all_files.append((path, get_lineage_meta(path)))
        self._filter(self._search.text())

    def touch_file_row(self, path: str) -> None:
        """Update just one row's metadata after an autosave.

        ``refresh()`` rescans the whole lineages directory and rebuilds every
        QListWidgetItem.  An autosave only changes a file's modified time, so
        doing that full rebuild on every debounced save (up to once per 1.5 s
        while typing or dragging) was a large, entirely avoidable UI cost.
        This updates the in-memory metadata and the single existing item text
        in place, leaving the rest of the list untouched.
        """
        if not path:
            return
        meta = get_lineage_meta(path)
        for i, (p, _m) in enumerate(self._all_files):
            if p == path:
                self._all_files[i] = (p, meta)
                break
        else:
            # Not in the list yet (e.g. saved under a new name) — fall back.
            self.refresh()
            return

        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == path:
                n = meta["node_count"]
                cat_tag = f" [{meta['category']}]" if meta.get("category") else ""
                item.setText(
                    f"  {meta['name']}{cat_tag}\n"
                    f"  {n} plant{'s' if n != 1 else ''}  ·  {meta['modified']}")
                return

    @staticmethod
    def _fuzzy_match(query: str, text: str) -> bool:
        if not query: return True
        t = text.lower(); i = 0
        for ch in query.lower():
            idx = t.find(ch, i)
            if idx == -1: return False
            i = idx + 1
        return True

    def _filter(self, text: str) -> None:
        q = text.strip()
        self._list.clear()

        # Apply search filter
        visible = [(p, m) for p, m in self._all_files
                   if not q or self._fuzzy_match(q, m["name"])
                   or self._fuzzy_match(q, m.get("category", ""))]

        # Sort
        visible = self._sorted_files(visible)

        if self._show_categories and not q:
            self._render_with_categories(visible)
        else:
            self._render_flat(visible)

        if self._list.count() == 0:
            ph = QListWidgetItem("No results." if q else "No lineages found.")
            ph.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(ph)

    def _render_flat(self, items: list) -> None:
        for path, meta in items:
            self._list.addItem(self._make_item(path, meta))

    def _render_with_categories(self, items: list) -> None:
        """Group items by category, inserting non-selectable section headers."""
        # Collect categories
        categorised: dict[str, list] = {}
        for path, meta in items:
            cat = meta.get("category", "") or "Uncategorised"
            categorised.setdefault(cat, []).append((path, meta))

        # Uncategorised last
        order = sorted(k for k in categorised if k != "Uncategorised")
        if "Uncategorised" in categorised:
            order.append("Uncategorised")

        for cat in order:
            entries = categorised[cat]
            # Section header
            hdr = QListWidgetItem(f"  {cat}")
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            hdr.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            hdr.setForeground(QColor("#4ecb6a"))
            self._list.addItem(hdr)
            for path, meta in entries:
                self._list.addItem(self._make_item(path, meta))

    def _make_item(self, path: str, meta: dict) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, path)
        n = meta["node_count"]
        cat_tag = f" [{meta['category']}]" if meta.get("category") else ""
        item.setText(
            f"  {meta['name']}{cat_tag}\n"
            f"  {n} plant{'s' if n != 1 else ''}  ·  {meta['modified']}"
        )
        item.setFont(QFont("Segoe UI", 9))
        return item

    # ── Open / context menu ───────────────────────────────────────────────────

    def _on_open(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path: self.open_requested.emit(path)

    def _on_context_menu(self, pos) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        a_new = menu.addAction("➕  Create New Lineage")
        menu.addSeparator()
        item = self._list.itemAt(pos)
        a_rename = a_del = a_cat = None
        path = item.data(Qt.ItemDataRole.UserRole) if item else None
        if path:
            a_rename = menu.addAction("✏️  Rename This Lineage")
            a_cat    = menu.addAction("🏷️  Set Category…")
            a_del    = menu.addAction("🗑️  Delete This Lineage")
        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen == a_new:
            self.new_lineage_requested.emit()
        elif a_rename and chosen == a_rename:
            self.rename_lineage_requested.emit(path)
        elif a_cat and chosen == a_cat:
            self.set_category_requested.emit(path)
        elif a_del and chosen == a_del:
            self.delete_lineage_requested.emit(path)


# ── Properties panel ──────────────────────────────────────────────────────────

class PropertiesPanel(QWidget):
    edit_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("props_panel"); self.setFixedWidth(260)
        self._current_node_id: Optional[str] = None
        self._current_tree:    Optional[LineageTree] = None
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self); lay.setContentsMargins(8,8,8,8); lay.setSpacing(8)
        hdr = QLabel("🔍  Properties"); hdr.setObjectName("panel_header"); lay.addWidget(hdr)
        self._frame = QFrame(); self._frame.setObjectName("props_frame")
        fl = QVBoxLayout(self._frame); fl.setContentsMargins(12,12,12,12); fl.setSpacing(6)
        self._img_label = QLabel(); self._img_label.setObjectName("node_img_label")
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setFixedHeight(130); fl.addWidget(self._img_label)
        self._name_lbl    = QLabel("—"); self._name_lbl.setObjectName("node_name_label")
        self._name_lbl.setWordWrap(True); fl.addWidget(self._name_lbl)
        self._species_lbl = QLabel(""); self._species_lbl.setObjectName("node_species_label")
        fl.addWidget(self._species_lbl)
        self._year_lbl    = QLabel(""); self._year_lbl.setObjectName("node_year_label")
        fl.addWidget(self._year_lbl)
        self._desc_lbl    = QLabel(""); self._desc_lbl.setObjectName("node_desc_label")
        self._desc_lbl.setWordWrap(True); fl.addWidget(self._desc_lbl)
        self._tags_lbl    = QLabel(""); self._tags_lbl.setObjectName("node_tags_label")
        self._tags_lbl.setWordWrap(True); fl.addWidget(self._tags_lbl)
        self._gen_lbl     = QLabel(""); self._gen_lbl.setObjectName("node_gen_label")
        fl.addWidget(self._gen_lbl)
        self._roles_group = QGroupBox("Parent Roles")
        self._roles_group.setLayout(QFormLayout()); fl.addWidget(self._roles_group)
        self._roles_group.setVisible(False)
        fl.addStretch()
        self._edit_btn = QPushButton("✏️  Edit Plant"); self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(
            lambda: self._current_node_id and self.edit_requested.emit(self._current_node_id))
        fl.addWidget(self._edit_btn)
        lay.addWidget(self._frame); lay.addStretch()
        self._show_empty()

    def _show_empty(self) -> None:
        self._name_lbl.setText("Select a plant"); self._species_lbl.setText("")
        self._year_lbl.setText(""); self._desc_lbl.setText("Click a node to view details.")
        self._tags_lbl.setText(""); self._gen_lbl.setText("")
        self._img_label.clear(); self._img_label.setText("—")
        self._edit_btn.setEnabled(False); self._roles_group.setVisible(False)

    def update_node(self, node: PlantNode, tree: LineageTree) -> None:
        self._current_node_id = node.id; self._current_tree = tree
        self._name_lbl.setText(node.name or "Unnamed")
        sp = f"{node.species}  {node.cultivar}".strip() if node.species else ""
        self._species_lbl.setText(sp)
        self._year_lbl.setText(f"📅 {node.year}" if node.year else "")
        desc = node.description or ""
        self._desc_lbl.setText(desc[:200] + ("…" if len(desc)>200 else ""))
        self._tags_lbl.setText("  ".join(f"[{t}]" for t in node.tags))
        try:
            gen = tree.get_generation(node.id)
            p = len(node.parent_ids); o = len(tree.get_offspring(node.id))
            self._gen_lbl.setText(f"Gen {gen}  •  {p} parent(s)  •  {o} offspring")
        except Exception: self._gen_lbl.setText("")
        b64 = node.thumb or (node.images[0] if node.images else None)
        if b64:
            try:
                pix = QPixmap(); pix.loadFromData(base64.b64decode(b64))
                if not pix.isNull():
                    self._img_label.setPixmap(
                        pix.scaled(QSize(244,125), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation))
                    self._edit_btn.setEnabled(True)
                    self._rebuild_role_combos(node, tree)
                    return
            except Exception as e: log.warning("props image: %s", e)
        self._img_label.clear(); self._img_label.setText("🌿")
        self._edit_btn.setEnabled(True)
        self._rebuild_role_combos(node, tree)

    def _rebuild_role_combos(self, node: PlantNode, tree: LineageTree) -> None:
        layout = self._roles_group.layout()
        while layout.count():
            it = layout.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._role_combos: dict = {}
        if not node.parent_ids: self._roles_group.setVisible(False); return
        self._roles_group.setVisible(True)
        for pid in node.parent_ids:
            parent = tree.nodes.get(pid)
            if not parent: continue
            combo = QComboBox(); combo.addItems(["Unknown","♀ Mother","♂ Father"])
            role = node.parent_roles.get(pid,"")
            if role=="mother": combo.setCurrentIndex(1)
            elif role=="father": combo.setCurrentIndex(2)
            combo.currentIndexChanged.connect(
                lambda idx, p=pid, n=node: self._on_role_changed(n, p, idx))
            self._role_combos[pid] = combo
            layout.addRow(parent.name[:18]+":", combo)

    def _on_role_changed(self, node: PlantNode, pid: str, idx: int) -> None:
        node.parent_roles[pid] = {0:"",1:"mother",2:"father"}.get(idx,"")
        if self._current_tree: self._current_tree.touch()

    def clear(self) -> None:
        self._current_node_id = None; self._current_tree = None; self._show_empty()


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self, settings: AppSettings = None):
        super().__init__()
        self.setWindowTitle("🌿  BreedingCharts")

        self._settings: AppSettings = settings or load_settings()
        self._tree:     Optional[LineageTree] = None
        self._filepath: Optional[str]         = None
        self._unsaved:  bool                  = False
        self._loading:  bool                  = False
        self._pending_drop_image: Optional[str] = None
        # Path to restore after the window has painted (see __init__), and
        # the worker doing it.  Keeping a dedicated worker (rather than
        # reusing _load_worker) lets File → Open cancel the restore cleanly.
        self._initial_load_path: Optional[str] = None
        self._initial_load_worker: Optional[_LoadWorker] = None

        self._actions: dict[str, QAction] = {}
        self._scene = LineageScene()
        self._view  = LineageView(self._scene)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)

        # Debounced "save-on-change": fires 1.5 s after the last tree_changed.
        # This is how Figma/Notion work — every edit is persisted almost instantly
        # without blocking the UI on every keystroke or node drag.
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(1500)   # ms after last change
        self._debounce_timer.timeout.connect(self._debounce_save)
        self._save_worker: Optional[_SaveWorker] = None   # background debounce save

        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._connect_signals()
        self._apply_settings(self._settings)

        # Restore window size/position (and which monitor) from last session.
        # saveGeometry()/restoreGeometry() capture this in one blob, including
        # maximized state — falls back to the old fixed-size behaviour on
        # first run or if the saved blob can't be applied (e.g. corrupted).
        geo = getattr(self._settings, "window_geometry", "")
        restored = False
        if geo:
            try:
                restored = self.restoreGeometry(QByteArray.fromBase64(geo.encode("ascii")))
            except Exception as e:
                log.warning("Could not restore window geometry: %s", e)
        if not restored:
            if self._settings.window_maximized:
                self.showMaximized()
            else:
                self.resize(1480, 900)

        self._status("Ready — create a new lineage or open one from the sidebar.  Press ? for shortcuts.")

        # Reopen the last lineage asynchronously.  Parsing a large .plf on the
        # UI thread here blocked the window from painting at launch, so the
        # app could appear frozen for several seconds on a big database.  The
        # existing _LoadWorker already does this off-thread for File → Open;
        # scheduling it for the first event-loop tick lets the window paint,
        # then loads in the background.
        last = self._settings.last_open_filepath
        if last and os.path.isfile(last):
            self._initial_load_path = last
            QTimer.singleShot(0, self._load_initial_file)

        # First-run convenience: if no lineage is being restored and no
        # lineage files exist yet, silently create a blank "My Lineage" so
        # the user can start adding plants immediately without an extra dialog.
        if (self._initial_load_path is None and self._tree is None
                and not get_lineage_files(get_data_dir())):
            tree = LineageTree(name="My Lineage")
            filepath = default_filepath(tree.name, directory=get_data_dir())
            save_lineage(tree, filepath)
            self._load_tree(tree, filepath=filepath)
            self._unsaved = False
            self._update_title()
            self._sidebar.refresh()
            self._status("Welcome! A default lineage has been created — add your first plant with ＋ New Plant.")

        # Reopen on whichever view (lineage canvas / grid / spreadsheet) was
        # active when the app was last closed. Comes after the lineage is
        # loaded above (and already auto-laid-out there, if that setting is
        # on) so switching to Grid/Spreadsheet here has real data to show.
        self._restore_last_view()

        # ── First-run tour ─────────────────────────────────────────────────────
        if not self._settings.tour_shown:
            QTimer.singleShot(400, self._show_tour)

    # ── UI layout ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget(); self.setCentralWidget(central)
        root = QHBoxLayout(central); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        self._sidebar = DatabaseSidebar(
            sort_mode=self._settings.sidebar_sort,
            show_categories=self._settings.sidebar_show_categories,
        )
        self._props   = PropertiesPanel()
        self._props.setVisible(self._settings.show_props_panel)

        p = THEMES.get(self._settings.theme, THEMES["dark"])
        self._grid_view = PlantGridView(
            palette=p, parent=self,
            sort_mode=getattr(self._settings, "grid_sort", "name"))
        self._notes_view = NotesGridView(palette=p, parent=self)
        self._main_stack = QStackedWidget()
        self._main_stack.addWidget(self._view)        # index 0 — lineage canvas
        self._main_stack.addWidget(self._grid_view)   # index 1 — plant grid / spreadsheet
        self._main_stack.addWidget(self._notes_view)  # index 2 — notes
        self._main_stack.setCurrentIndex(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._sidebar)
        self._splitter.addWidget(self._main_stack)
        self._splitter.addWidget(self._props)
        self._splitter.setStretchFactor(0,0); self._splitter.setStretchFactor(1,1)
        self._splitter.setStretchFactor(2,0); self._splitter.setHandleWidth(2)
        root.addWidget(self._splitter)
        self._statusbar = QStatusBar(); self.setStatusBar(self._statusbar)

    def _build_menu(self) -> None:
        mb = self.menuBar()

        def act(label: str, slot, action_id: str="", shortcut=None) -> QAction:
            a = QAction(label, self); a.triggered.connect(slot)
            if shortcut is not None:
                a.setShortcut(QKeySequence(shortcut) if isinstance(shortcut,str)
                              else QKeySequence(shortcut))
            if action_id: self._actions[action_id] = a
            return a

        fm = mb.addMenu("File")
        fm.addAction(act("New Lineage…",           self._new_lineage,  "new_lineage"))
        fm.addAction(act("Rename Current Lineage…", self._rename_current_lineage, "rename_lineage"))
        fm.addAction(act("Open…",                  self._open_dialog,
                         shortcut=QKeySequence.StandardKey.Open))
        fm.addAction(act("Save",                   self._save,         "save"))
        fm.addAction(act("Save As…",               self._save_as,      "save_as"))
        fm.addSeparator()
        fm.addAction(act("Change Data Folder…",    self._change_data_folder, "data_folder"))
        fm.addSeparator()
        fm.addAction(act("Export Data Bundle (Lineages + Database)…",
                         self._export_data_bundle, "export_bundle"))
        fm.addAction(act("Import Data Bundle…",
                         self._import_data_bundle, "import_bundle"))
        fm.addSeparator()
        fm.addAction(act("Export Canvas to PNG…",  self._export_png,   "export_png"))
        fm.addAction(act("Export Canvas to SVG…",  self._export_svg,   "export_svg"))
        fm.addAction(act("Export Canvas to PDF…",  self._export_pdf,   "export_pdf"))
        fm.addAction(act("Export Plant List to CSV…", self._export_csv, "export_csv"))
        fm.addAction(act("Import .csv…",           self._import_csv,   "import_csv"))
        fm.addAction(act("Import Plant Database (JSON)…",
                         self._import_plant_db, "import_plant_db"))
        fm.addSeparator()
        fm.addAction(act("Quit",                   self.close,
                         shortcut=QKeySequence.StandardKey.Quit))

        em = mb.addMenu("Edit")
        self._act_undo = act("Undo", self._undo, "undo")
        self._act_undo.setEnabled(False)
        em.addAction(self._act_undo)
        em.addSeparator()
        em.addAction(act("Add Plant…",                 self._add_plant,       "add_plant"))
        em.addAction(act("Paste Image from Clipboard", self._global_paste,    "paste_image"))
        em.addAction(act("Delete Selected Node",       self._delete_selected, "delete_node"))
        em.addSeparator()
        em.addAction(act("Copy Selected Nodes",        self._copy_nodes,      "copy_nodes"))
        em.addAction(act("Paste Nodes",                self._paste_nodes,     "paste_nodes"))
        em.addSeparator()
        em.addAction(act("Connect Two Nodes…",         self._connect_dialog,  "connect"))
        em.addAction(act("Add Cross-Lineage Parent…",  self._cross_lineage_connect, "cross_lineage"))
        em.addAction(act("Add Comment Box",            self._add_comment,     "add_comment"))
        em.addSeparator()
        em.addAction(act("Batch Edit Selected Plants…",self._batch_edit,      "batch_edit"))
        em.addAction(act("Find Duplicate Plants…",     self._find_duplicates, "find_dupes"))
        em.addAction(act("Pedigree Certificate…",      self._pedigree_cert,   "pedigree_cert"))
        em.addSeparator()
        em.addAction(act("Open Plant Database…",       self._open_plant_db,   "plant_db"))
        em.addAction(act("Open / Toggle Grid View…",   self._open_grid_view,  "grid_view"))

        vm = mb.addMenu("View")
        vm.addAction(act("Auto-Layout",         self._auto_layout,    "auto_layout"))
        vm.addAction(act("Auto-Organize",        self._auto_organize,  "auto_organize"))
        vm.addAction(act("Fit Canvas",          self._view.fit_view,"fit_view"))
        vm.addSeparator()
        self._act_props = QAction("Show Properties Panel", self)
        self._act_props.setCheckable(True)
        self._act_props.setChecked(self._settings.show_props_panel)
        self._act_props.triggered.connect(self._toggle_props_panel)
        vm.addAction(self._act_props)
        vm.addSeparator()
        vm.addAction(act("Refresh Sidebar",     self._sidebar.refresh))

        sm = mb.addMenu("Settings")
        sm.addAction(act("⚙️  All Settings…",    self._open_settings, "open_settings"))
        sm.addSeparator()
        self._theme_actions: dict[str, QAction] = {}
        for tid, td in THEMES.items():
            a = QAction(td["label"], self); a.setCheckable(True)
            a.setChecked(tid == self._settings.theme)
            a.triggered.connect(lambda _, t=tid: self._quick_set_theme(t))
            sm.addAction(a); self._theme_actions[tid] = a

        hm = mb.addMenu("Help")
        hm.addAction(act("Keyboard Shortcuts  (?)", self._show_shortcuts_overlay))
        hm.addAction(act("Restart Tour…",           self._show_tour))
        hm.addSeparator()
        dm = hm.addMenu("🐛  Debug")
        self._act_debug = dm.addAction("Enable Performance Logging")
        self._act_debug.setCheckable(True)
        self._act_debug.setChecked(perf.is_enabled())
        self._act_debug.toggled.connect(self._toggle_debug)
        dm.addAction("Show Timing Stats…", self._show_perf_stats)
        dm.addAction("Reset Timing Stats",  perf.reset_stats)
        dm.addSeparator()
        dm.addAction("Show RAM Usage…", self._show_ram_usage)
        hm.addSeparator()
        hm.addAction(act("About…", self._about))

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main"); tb.setIconSize(QSize(18,18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False); self.addToolBar(tb)
        def ta(l,s,t=""): a=QAction(l,self); a.triggered.connect(s); a.setToolTip(t); return a
        tb.addAction(ta("＋  New Plant",  self._add_plant,      "Add plant"))
        tb.addAction(ta("💾  Save",        self._save,           "Save"))
        tb.addSeparator()
        tb.addAction(ta("↩  Undo",        self._undo,           "Undo"))
        tb.addSeparator()
        tb.addAction(ta("🌿  Plant DB",    self._open_plant_db,  "Plant database"))
        self._act_grid_view = ta("⊞  Grid View", self._open_grid_view,
                                  "Switch to card grid view of all plants  (Ctrl+Tab)")
        tb.addAction(self._act_grid_view)
        self._act_sheet_view = ta("📊  Spreadsheet", self._open_spreadsheet_view,
                                   "Switch to spreadsheet view of all plants")
        tb.addAction(self._act_sheet_view)
        self._act_notes_view = ta("📝  Notes", self._open_notes_view,
                                   "Switch to Notes — breeder's log & free-form notes")
        tb.addAction(self._act_notes_view)
        tb.addSeparator()
        tb.addAction(ta("⇄  Connect",     self._connect_dialog, "Connect nodes"))
        tb.addAction(ta("💬  Comment",     self._add_comment,    "Add comment"))
        tb.addAction(ta("⚡  Auto-Layout",  self._auto_layout,    "Auto-layout nodes by generation"))
        tb.addAction(ta("✦  Auto-Organize", self._auto_organize,  "Minimize crossing lines"))
        tb.addSeparator()
        tb.addAction(ta("📤  Export PNG",  self._export_png,     "Export to PNG"))

    def _connect_signals(self) -> None:
        self._scene.node_selected.connect(self._on_node_selected)
        self._scene.tree_changed.connect(self._on_tree_changed)
        self._scene.edit_node_requested.connect(self._open_edit_dialog)
        self._scene.add_parent_requested.connect(self._add_parent)
        self._scene.add_offspring_requested.connect(self._add_offspring)
        self._scene.connect_requested.connect(self._on_connect_requested)
        self._scene.connect_mode_changed.connect(self._on_connect_mode_changed)
        self._scene.add_plant_at.connect(self._add_plant_at)
        self._scene.add_comment_at.connect(self._add_comment_at)
        self._scene.open_plant_db_requested.connect(self._open_plant_db)
        self._sidebar.open_requested.connect(self._open_file)
        self._sidebar.new_lineage_requested.connect(self._new_lineage)
        self._sidebar.delete_lineage_requested.connect(self._delete_lineage_file)
        self._sidebar.rename_lineage_requested.connect(self._rename_lineage)
        self._sidebar.set_category_requested.connect(self._set_lineage_category)  # v1.2.0
        self._sidebar.sort_changed.connect(self._on_sidebar_sort_changed)
        self._props.edit_requested.connect(self._open_edit_dialog)
        self._grid_view.add_to_lineage.connect(self._import_from_db)
        self._grid_view.switch_to_lineage_view.connect(self._show_lineage_view)
        self._grid_view.sort_changed.connect(self._on_grid_sort_changed)
        self._notes_view.switch_to_lineage_view.connect(self._show_lineage_view)

        # Ctrl+Tab — toggle between lineage canvas and grid/database view
        from PyQt6.QtGui import QShortcut
        sc = QShortcut(QKeySequence("Ctrl+Tab"), self)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(self._open_grid_view)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, on_change=self._apply_settings,
                              on_data_dir_changed=self._apply_new_data_dir, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings(dlg.result_settings)
            save_settings(self._settings); self._status("Settings saved.")

    def _apply_settings(self, s: AppSettings) -> None:
        self._settings = s
        # Re-applying an identical application-wide stylesheet is not free:
        # Qt re-polishes every existing widget, which measured at ~0.6 s once
        # the window is fully built.  main.py already sets this stylesheet
        # before the window is constructed, so on the normal startup path this
        # is a no-op — and widgets created afterwards pick the sheet up
        # automatically at polish time, so skipping it loses nothing.
        app = QApplication.instance()
        sheet = build_stylesheet(s)
        if app.styleSheet() != sheet:
            app.setStyleSheet(sheet)
        p = THEMES.get(s.theme, THEMES["dark"])
        self._view.set_theme_palette(p)
        self._view.set_font_size(s.font_size)
        self._view.set_font_family(s.font_family)
        self._view.set_show_grid(s.show_grid)

        # Pass the palette into apply_settings so it can update theme colours
        # AND font/card/edge settings in a single pass — avoids the two
        # separate _rebuild_edges() calls that caused visible lag.
        self._scene.apply_settings(s, palette=p)

        # Batch all three card-style parameters into one call so _rebuild_grid
        # runs at most once (vs three times when called separately).
        self._grid_view.apply_card_settings(
            palette   = p,
            font      = getattr(s, "card_font_family", "Georgia"),
            font_size = getattr(s, "card_font_size",   9),
        )
        self._notes_view.set_palette(p)

        self._act_props.setChecked(s.show_props_panel)
        self._props.setVisible(s.show_props_panel)
        for tid, a in self._theme_actions.items(): a.setChecked(tid == s.theme)
        for aid, action in self._actions.items():
            ks = s.keybinds.get(aid,"")
            action.setShortcut(QKeySequence(ks) if ks else QKeySequence())
        if s.autosave: self._autosave_timer.start(s.autosave_mins*60*1000)
        else:          self._autosave_timer.stop()
        # v1.2.0 — sync sidebar sort/category settings
        self._sidebar.set_sort_mode(getattr(s, "sidebar_sort", "name_asc"))
        self._sidebar._show_categories = getattr(s, "sidebar_show_categories", True)
        self._sidebar._filter(self._sidebar._search.text())

    def _quick_set_theme(self, tid: str) -> None:
        self._settings.theme = tid; self._apply_settings(self._settings)
        self._schedule_settings_save()
        self._status(f"Theme: {THEMES[tid]['label'].strip()}")

    def _toggle_grid(self, checked: bool) -> None:
        self._settings.show_grid = checked
        self._view.set_show_grid(checked); self._schedule_settings_save()

    def _toggle_images(self, checked: bool) -> None:
        self._settings.show_images = checked
        self._scene.show_images = checked; self._scene.update()
        self._schedule_settings_save()

    def _toggle_props_panel(self, checked: bool) -> None:
        self._settings.show_props_panel = checked
        self._props.setVisible(checked); self._schedule_settings_save()

    def _on_grid_sort_changed(self, mode: str) -> None:
        """Persist the Grid View sort choice so it's remembered next launch."""
        self._settings.grid_sort = mode
        self._schedule_settings_save()

    def _on_sidebar_sort_changed(self, mode: str) -> None:
        """Persist the sidebar sort choice so it's remembered next launch."""
        self._settings.sidebar_sort = mode
        self._schedule_settings_save()

    # ── Autosave ──────────────────────────────────────────────────────────────

    def _schedule_settings_save(self) -> None:
        """F29: coalesce bursts of setting changes into one atomic write."""
        t = getattr(self, "_settings_save_timer", None)
        if t is None:
            t = self._settings_save_timer = QTimer(self)
            t.setSingleShot(True); t.setInterval(500)
            t.timeout.connect(lambda: save_settings(self._settings))
        t.start()

    def _autosave(self) -> None:
        """Periodic autosave (interval-based, legacy).  Still runs as a safety net."""
        if self._tree and self._filepath and self._unsaved:
            if save_lineage(self._tree, self._filepath):
                self._unsaved = False
                self._update_title()
                self._statusbar.showMessage("Autosaved ✓", 2000)

    def _debounce_save(self) -> None:
        """Save triggered 1.5 s after the last tree_changed signal.
        This is the primary persistence path — changes are never lost for more
        than 1.5 s without any user action required.

        Runs the actual disk write in a background thread: prepare_save()
        (fast — just copies data out of the tree) happens here on the UI
        thread, then _SaveWorker does the I/O off-thread so a save landing
        mid-drag or mid-typing never causes a visible stutter, including the
        less common save that also has to merge the images sidecar.
        """
        if not (self._tree and self._filepath and self._unsaved):
            return
        if self._save_worker is not None and self._save_worker.isRunning():
            # A previous debounce save is still writing — don't overlap.
            # _unsaved stays True; reschedule so this is retried shortly
            # after the current write finishes rather than waiting for the
            # next edit (or the periodic safety-net autosave) to trigger it.
            self._debounce_timer.start()
            return
        with perf.timed("prepare_save"):
            prepared = prepare_save(self._tree)
        # Any node whose images were part of this snapshot are now safely
        # captured for the background writer — clear the live tree's
        # "needs merge" bookkeeping now (main-thread-only, matches what
        # save_lineage()'s synchronous evict_saved_images() does) so a fresh
        # edit right after this point is correctly tracked as new dirt.
        if prepared["need_image_merge"]:
            for nid in list(self._tree._image_loaded_ids):
                node = self._tree.nodes.get(nid)
                if node is not None:
                    node.images = []
            self._tree._image_loaded_ids.clear()
            self._tree._images_maybe_stale = False

        # Optimistically clear the dirty flag now, since `prepared` fully
        # captures the tree as of this instant. If a further edit lands
        # while the background write is still in flight, _on_tree_changed()
        # will set _unsaved back to True and restart the debounce timer as
        # usual — so we must NOT unconditionally clear it again in the
        # finished handler below, or an edit made mid-write could get
        # silently marked "saved" when it wasn't.
        self._unsaved = False
        self._update_title()

        self._save_worker = _SaveWorker(prepared, self._filepath, parent=self)
        self._inflight_prepared = prepared      # F2: kept until the write is confirmed
        self._save_worker.signals.finished.connect(self._on_debounce_save_finished)
        self._save_worker.start()

    def _on_debounce_save_finished(self, ok: bool) -> None:
        self._inflight_prepared = None if ok else getattr(self, "_inflight_prepared", None)
        if ok:
            # Update only the saved lineage's row.  A full sidebar refresh
            # here rescanned the whole lineages directory and rebuilt every
            # list item on every autosave (up to once per 1.5 s while
            # editing), which was a major, needless source of UI stutter.
            self._sidebar.touch_file_row(self._filepath)
            self._statusbar.showMessage("Saved ✓", 1500)
        else:
            log.warning("Debounce save failed: %s", self._filepath)
            # F2: the images were evicted from RAM when the save was prepared;
            # put back every node that is still empty so the retry re-merges them.
            prepared = getattr(self, "_inflight_prepared", None)
            if prepared and self._tree is not None:
                for nid, imgs in prepared.get("images_snapshot", {}).items():
                    node = self._tree.nodes.get(nid)
                    if node is not None and not node.images:
                        node.images = list(imgs)
                        self._tree._image_loaded_ids.add(nid)
                self._tree._images_maybe_stale = True
            self._unsaved = True             # force a retry on next change
            self._update_title()

    def _save_current_silently(self) -> None:
        # F4: never write the same lineage from two threads at once
        if self._save_worker is not None and self._save_worker.isRunning():
            self._save_worker.wait(5000)
        if self._tree and self._filepath and self._unsaved:
            if save_lineage(self._tree, self._filepath):
                self._unsaved = False

    def _delete_lineage_file(self, path: str) -> None:
        import os
        meta = get_lineage_meta(path)
        name = meta.get("name", os.path.basename(path))
        reply = QMessageBox.question(
            self, "Delete Lineage",
            f"Permanently delete '{name}'?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(path)
            import lineage_images
            lineage_images.delete_sidecar(path)
        except Exception as e:
            QMessageBox.critical(self, "Delete Error", str(e))
            return
        # If we just deleted the currently open file, clear the view
        if self._filepath == path:
            self._tree     = None
            self._filepath = None
            self._unsaved  = False
            self._scene.filepath = None
            self._scene.load_tree(LineageTree(name=""))
            self._update_title()
        self._sidebar.refresh()
        self._status(f"Deleted: {name}")

    def _rename_lineage(self, path: str) -> None:
        """Rename a lineage file (called from sidebar context menu)."""
        meta     = get_lineage_meta(path)
        old_name = meta.get("name", os.path.basename(path))
        new_name, ok = QInputDialog.getText(
            self, "Rename Lineage", "New name:", text=old_name)
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        tree = load_lineage(path)
        if tree is None:
            QMessageBox.critical(self, "Rename Error", "Could not load the lineage file."); return
        tree.name = new_name
        if not save_lineage(tree, path):
            QMessageBox.critical(self, "Rename Error", "Could not save the lineage file."); return
        # Reflect the rename in the currently open view if it's the same file
        if self._filepath == path and self._tree:
            self._tree.name = new_name
            self._update_title()
            self._update_node_count()
        self._sidebar.refresh()
        self._status(f"Renamed to: {new_name}")

    def _rename_current_lineage(self) -> None:
        """Rename whichever lineage is currently open (File menu entry)."""
        if self._tree is None or self._filepath is None:
            flash_status(self, "Open a lineage first."); return
        self._rename_lineage(self._filepath)

    def _set_lineage_category(self, path: str) -> None:
        """Set or change the category/species tag for a lineage (v1.2.0)."""
        meta     = get_lineage_meta(path)
        old_cat  = meta.get("category", "")
        new_cat, ok = QInputDialog.getText(
            self, "Set Category",
            "Category (species, project group, etc.):\n"
            "Leave blank to remove grouping.",
            text=old_cat)
        if not ok: return
        new_cat = new_cat.strip()
        tree = load_lineage(path)
        if tree is None:
            QMessageBox.critical(self, "Error", "Could not load lineage."); return
        tree.category = new_cat
        save_lineage(tree, path)
        # If this is the open lineage, update in-memory tree too
        if self._filepath == path and self._tree:
            self._tree.category = new_cat
        self._sidebar.refresh()
        self._status(f"Category set to '{new_cat}'" if new_cat else "Category cleared.")

    def _new_lineage(self) -> None:
        self._save_current_silently()
        dlg = NewLineageDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        tree = LineageTree(name=dlg.lineage_name, description=dlg.lineage_desc)
        # Save into the configured data directory, not the app directory
        filepath = default_filepath(tree.name, directory=get_data_dir())
        save_lineage(tree, filepath)
        self._load_tree(tree, filepath=filepath)
        self._unsaved = False
        self._update_title()
        self._sidebar.refresh()
        self._status(f"Created and saved: {tree.name}")

    def _load_initial_file(self) -> None:
        """Restore the last-open lineage without blocking the first paint.

        Replaces the old synchronous ``load_lineage`` call in __init__.  This
        path deliberately avoids the modal progress bar / sidebar disabling
        used by _open_file: at startup there is nothing to interact with yet,
        and forcing the user back to the canvas here would undo whichever
        view (grid/notes) was restored.
        """
        path = self._initial_load_path
        self._initial_load_path = None
        if not (path and os.path.isfile(path)):
            return
        worker = _LoadWorker(path, parent=self)
        self._initial_load_worker = worker
        worker.signals.finished.connect(self._on_initial_load_finished)
        worker.signals.error.connect(
            lambda msg: self._status(f"Could not reopen last lineage: {msg}"))
        worker.start()

    @pyqtSlot(object, str)
    def _on_initial_load_finished(self, tree, filepath: str) -> None:
        # File → Open (or another restore) superseded this startup load.
        if self._initial_load_worker is None:
            return
        self._initial_load_worker = None
        if tree is None:
            self._status(f"Could not reopen: {os.path.basename(filepath)}")
            return
        self._load_tree(tree, filepath=filepath)
        self._status(f"Reopened: {os.path.basename(filepath)}")

    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Lineage", "", "BreedingCharts Files (*.plf);;All Files (*)")
        if path: self._open_file(path)

    def _open_file(self, path: str) -> None:
        """Open a lineage file. Uses a background thread for large files so the
        UI stays responsive while JSON is parsed."""
        self._save_current_silently()

        # Show a non-modal progress indicator in the status bar
        self._statusbar.showMessage(f"Loading {os.path.basename(path)}…  ⏳")
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)     # indeterminate / bouncing
        self._progress.setMaximumWidth(160)
        self._progress.setMaximumHeight(16)
        self._statusbar.addPermanentWidget(self._progress)
        QApplication.processEvents()       # paint the status bar immediately

        # Disable UI while loading to prevent re-entrance
        self._sidebar.setEnabled(False)

        # A user-initiated open supersedes any startup restore still in
        # flight.  Clearing the reference makes its finished handler ignore
        # the stale result instead of overwriting the file just opened.
        self._initial_load_worker = None

        # Launch background worker
        self._load_worker = _LoadWorker(path, parent=self)
        self._load_worker.signals.finished.connect(self._on_load_finished)
        self._load_worker.signals.error.connect(self._on_load_error)
        self._load_worker.start()

    @pyqtSlot(object, str)
    def _on_load_finished(self, tree, filepath: str) -> None:
        """Called on main thread when background load completes."""
        self._statusbar.removeWidget(self._progress)
        self._sidebar.setEnabled(True)
        if tree is None:
            QMessageBox.critical(self, "Error", f"Could not load:\n{filepath}")
            self._status("Load failed.")
            return
        self._load_tree(tree, filepath=filepath)
        self._status(f"Opened: {os.path.basename(filepath)}  ({len(tree.nodes)} plants)")
        # If the user clicked a lineage from the Grid View, switch back to
        # the canvas so the newly-opened lineage is immediately visible.
        if self._main_stack.currentIndex() == 1:
            self._show_lineage_view()

    @pyqtSlot(str)
    def _on_load_error(self, msg: str) -> None:
        self._statusbar.removeWidget(self._progress)
        self._sidebar.setEnabled(True)
        QMessageBox.critical(self, "Load Error", msg)
        self._status("Load failed.")

    def _show_lineage_view(self) -> None:
        """Switch to the lineage canvas view and reset toolbar button labels."""
        self._main_stack.setCurrentIndex(0)
        self._reset_view_toggle_labels()
        self._save_last_view()

    def _reset_view_toggle_labels(self) -> None:
        """Reset the Grid View / Spreadsheet / Notes toolbar buttons to
        their default "enter this view" labels. Each of those buttons
        doubles as a "go back" button while its own view is active (see
        _open_grid_view / _open_spreadsheet_view / _open_notes_view), so
        whenever we land on any *other* view, all three need resetting —
        not just the one that happens to be losing focus."""
        self._act_grid_view.setText("⊞  Grid View")
        self._act_grid_view.setToolTip("Switch to card grid view of all plants  (Ctrl+Tab)")
        self._act_sheet_view.setText("📊  Spreadsheet")
        self._act_sheet_view.setToolTip("Switch to spreadsheet view of all plants")
        self._act_notes_view.setText("📝  Notes")
        self._act_notes_view.setToolTip("Switch to Notes — breeder's log & free-form notes")

    def _save_last_view(self) -> None:
        """Persist which view (lineage canvas / card grid / spreadsheet /
        notes) is active so the app can reopen on the same view next
        launch. Safe to call during startup restoration too — it's a
        no-op if the value hasn't actually changed."""
        idx = self._main_stack.currentIndex()
        if idx == 2:
            view = "notes"
        elif idx == 1:
            view = "sheet" if self._grid_view.current_mode() == "sheet" else "grid"
        else:
            view = "lineage"
        if self._settings.last_active_view != view:
            self._settings.last_active_view = view
            self._schedule_settings_save()

    def _load_tree(self, tree: LineageTree, filepath: Optional[str]) -> None:
        self._loading  = True
        self._tree     = tree; self._filepath = filepath; self._unsaved = False
        self._scene.filepath = filepath
        self._scene.load_tree(tree)
        self._props.clear()
        self._update_title(); self._update_node_count(); self._update_undo_state()
        self._grid_view.mark_dirty()

        # Tell the grid view which lineage is open so it can show / update
        # the "Back to Lineage" button in its header.
        display_name = tree.name or (os.path.basename(filepath) if filepath else "Lineage")
        self._grid_view.set_lineage_open(display_name)

        # Auto-layout: run whenever a lineage with nodes is opened, if enabled.
        if getattr(self._settings, 'auto_layout_on_open', True) and len(tree.nodes) > 0:
            self._scene.auto_layout(record_undo=False)    # F7: still inside the loading guard

        self._view.fit_view()
        self._loading = False                              # F7: opening a file is not an edit
        # Remember last opened file
        if filepath:
            self._settings.last_open_filepath = filepath
            self._schedule_settings_save()

    def _save(self) -> bool:
        if self._tree is None: return False
        if self._filepath is None: return self._save_as()
        ok = save_lineage(self._tree, self._filepath)
        if ok:
            self._unsaved = False; self._update_title()
            self._sidebar.refresh(); self._status("Saved ✓")
        else:
            QMessageBox.critical(self, "Save Error", "Could not write file.")
        return ok

    def _save_as(self) -> bool:
        if self._tree is None: return False
        default = self._filepath or default_filepath(self._tree.name)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", default, "BreedingCharts Files (*.plf)")
        if not path: return False
        if not path.lower().endswith(".plf"): path += ".plf"
        self._filepath = path; self._scene.filepath = path; return self._save()

    def _export_png(self) -> None:
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        default_name = (self._tree.name or "lineage").replace(" ", "_") + ".png"
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", default_name, "PNG Image (*.png)")
        if path:
            p      = THEMES.get(self._settings.theme, THEMES["dark"])
            bg_col = p.get("canvas_bg", "#111816")
            wm_text = (self._settings.watermark_text
                       if getattr(self._settings, "watermark_enabled", False) else "")
            wm_op   = getattr(self._settings, "watermark_opacity", 0.35)
            ok = export_view_to_png(self._view, path, bg_color=bg_col,
                                    watermark_text=wm_text, watermark_opacity=wm_op)
            if ok: self._status(f"Exported: {path}")
            else:  QMessageBox.critical(self, "Export Error", "Could not write PNG.")

    def _export_svg(self) -> None:
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        default_name = (self._tree.name or "lineage").replace(" ", "_") + ".svg"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Canvas to SVG", default_name, "SVG Vector (*.svg)")
        if path:
            title = self._tree.name if self._tree else "BreedingCharts"
            ok = self._view.export_to_svg(path, title=title)
            if ok:   self._status(f"SVG exported: {path}")
            else:    QMessageBox.critical(self, "SVG Error",
                         "Could not export SVG.\nMake sure PyQt6-Qt6-Svg is installed.")

    def _export_pdf(self) -> None:
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        default_name = (self._tree.name or "lineage").replace(" ", "_") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Canvas to PDF", default_name, "PDF Document (*.pdf)")
        if path:
            title = self._tree.name if self._tree else "BreedingCharts"
            ok = self._view.export_to_pdf(path, title=title)
            if ok:   self._status(f"PDF exported: {path}")
            else:    QMessageBox.critical(self, "PDF Error",
                         "Could not export PDF.\nCheck the log for details.")

    def _batch_edit(self) -> None:
        """Apply tag/color/species changes to all currently selected canvas nodes."""
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        selected_items = [i for i in self._scene.selectedItems()
                          if hasattr(i, "node")]
        if not selected_items:
            flash_status(self, "Select plants on the canvas first (click + Ctrl+click to multi-select).")
            return
        nodes = [i.node for i in selected_items]
        dlg = BatchEditDialog(nodes, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        count = dlg.apply_to(nodes)
        if count:
            # Refresh all modified items and mark dirty
            for item in selected_items:
                item._card_h_cache = None
                item.prepareGeometryChange()
                item.update()
            self._unsaved = True
            self._update_title()
            self._status(f"Batch edit applied to {count} plant(s).")
        else:
            self._status("Batch edit: no changes made.")

    def _find_duplicates(self) -> None:
        """Open the duplicate plant detector across all lineage files."""
        import os
        dlg = DuplicateDetectorDialog(parent=self, app_dir=get_data_dir())
        dlg.exec()

    def _pedigree_cert(self) -> None:
        """Generate a pedigree certificate for the selected plant."""
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        selected = [i for i in self._scene.selectedItems() if hasattr(i, "node")]
        if not selected:
            flash_status(self, "Select a plant on the canvas first."); return
        node = selected[0].node
        dlg = PedigreeCertDialog(node, self._tree, parent=self)
        dlg.exec()

    def _cross_lineage_connect(self) -> None:
        """Add a cross-lineage plant as a parent of the selected node."""
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        selected = [i for i in self._scene.selectedItems() if hasattr(i, "node")]
        if not selected:
            flash_status(self, "Select the child plant on the canvas first."); return
        child_node = selected[0].node
        import os
        dlg = CrossLineageConnectDialog(parent=self, app_dir=get_data_dir())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        sel = dlg.selected
        if sel is None:
            return
        # Import the parent plant node into this tree as a ghost/reference node
        from database import load_lineage as _ll
        src_tree = _ll(sel["filepath"])
        if src_tree is None:
            QMessageBox.critical(self, "Error", "Could not load source lineage."); return
        src_node = src_tree.nodes.get(sel["plant_id"])
        if src_node is None:
            QMessageBox.critical(self, "Error", "Plant not found in source lineage."); return
        # Copy the plant into the current tree (keep original ID to allow de-duplication later)
        if src_node.id not in self._tree.nodes:
            import copy as _copy
            import lineage_images
            # Pull the source node's full-res images in before copying, so the
            # imported node gets its own independent copy rather than an
            # empty placeholder pointing at a sidecar file it'll never see.
            lineage_images.populate_node_images(src_tree, sel["filepath"], src_node.id)
            imported = _copy.deepcopy(src_node)
            imported.tags = list(imported.tags) + ["⛓ cross-lineage"]
            # Position near child
            imported.x = child_node.x - 240
            imported.y = child_node.y - 180
            self._tree.add_node(imported)
            if imported.images or imported.thumb:
                lineage_images.mark_images_authoritative(self._tree, imported.id)
            self._scene.load_tree(self._tree)   # full rebuild to show new node
        # Now connect
        if src_node.id not in child_node.parent_ids:
            ok = self._tree.connect_parent(src_node.id, child_node.id)
            if sel.get("role") and src_node.id in self._tree.nodes:
                self._tree.nodes[child_node.id].parent_roles[src_node.id] = sel["role"]
            if ok:
                self._scene.load_tree(self._tree)
                self._unsaved = True
                self._update_title()
                self._status(f"Cross-lineage parent '{src_node.name}' added.")
            else:
                QMessageBox.warning(self, "Connect", "Could not connect (cycle or already linked).")

    def _export_csv(self) -> None:
        """Export the current lineage's plant list to a CSV file."""
        import csv
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        default_name = (self._tree.name or "lineage").replace(" ", "_") + "_plants.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Plant List to CSV", default_name,
            "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        nodes = list(self._tree.nodes.values())
        if not nodes:
            flash_status(self, "No plants to export."); return
        # Build parent name columns (up to 2 parents)
        def parent_names(node) -> tuple:
            ids = list(node.parent_ids)
            p1 = self._tree.nodes.get(ids[0]).name if len(ids) > 0 and ids[0] in self._tree.nodes else ""
            p2 = self._tree.nodes.get(ids[1]).name if len(ids) > 1 and ids[1] in self._tree.nodes else ""
            return p1, p2
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow([
                    "Name", "Species", "Cultivar", "Year",
                    "Parent 1", "Parent 2",
                    "Color", "Tags", "Description",
                ])
                for node in sorted(nodes, key=lambda n: (n.name or "").lower()):
                    p1, p2 = parent_names(node)
                    writer.writerow([
                        node.name,
                        node.species,
                        node.cultivar,
                        node.year or "",
                        p1, p2,
                        node.color or "",
                        ", ".join(node.tags) if node.tags else "",
                        (node.description or "").replace("\n", " "),
                    ])
            self._status(f"Exported {len(nodes)} plants → {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not write CSV:\n{e}")

    # ── Plant CRUD ────────────────────────────────────────────────────────────

    def _add_plant(self) -> None:
        """Add a new plant.

        Behaviour depends on the active view:
        • **Grid view** (index 1): add to the plant *database* only.
          The user can add a DB plant to the open lineage separately via the
          right-click → "Add to Lineage" context menu.  This keeps the two
          operations cleanly separated — Ctrl+N in grid view never touches
          the canvas.
        • **Lineage view** (index 0): add to both the database *and* the
          currently open lineage (places the card at the centre of the
          viewport).  Requires a lineage to be open.
        """
        in_grid_view = self._main_stack.currentIndex() == 1

        # In grid view there's no requirement to have a lineage open
        if not in_grid_view and self._tree is None:
            flash_status(self, "Create or open a lineage first.")
            return

        node = PlantNode()
        dlg  = PlantDialog(node, self, is_new=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # Always persist to the global plant database
        plant_db.upsert(node)
        self._grid_view.mark_dirty()

        if in_grid_view:
            # Grid view: database-only add — refresh the grid so the card appears
            self._grid_view._refresh()
            self._status(f"Added to database: {node.name}")
        else:
            # Lineage view: also place the node on the canvas
            vr = self._view.mapToScene(self._view.viewport().rect()).boundingRect()
            node.x = vr.center().x(); node.y = vr.center().y()
            self._scene.add_node(node)
            self._update_node_count()
            self._status(f"Added to lineage: {node.name}")

    def _add_plant_at(self, x: float, y: float) -> None:
        if self._tree is None:
            flash_status(self, "Create or open a lineage first."); return
        node = PlantNode(); node.x = x; node.y = y
        pending = getattr(self._view, "_pending_drop_image", None) or self._pending_drop_image
        if getattr(self._view, "_pending_drop_image", None):
            self._view._pending_drop_image = None
        self._pending_drop_image = None
        if pending:
            try:
                with open(pending, "rb") as fh: raw = fh.read()
                node.images.append(base64.b64encode(raw).decode())
            except Exception as e: log.error("drop image: %s", e)
        dlg = PlantDialog(node, self, is_new=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._scene.add_node(node); plant_db.upsert(node)
            self._update_node_count(); self._status(f"Added: {node.name}")

    def _add_comment_at(self, x: float, y: float) -> None:
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        text, ok = QInputDialog.getMultiLineText(self, "Add Comment", "Comment text:", "")
        if not ok or not text.strip(): return
        note = CommentNote(text=text.strip(), x=x - 110, y=y - 45)
        self._scene.add_comment(note); self._status("Comment added.")

    def _open_edit_dialog(self, node_id: str) -> None:
        if not self._tree or node_id not in self._tree.nodes: return
        node = self._tree.nodes[node_id]

        # Full-resolution images are stored lazily — load this node's real
        # images (if any) before the dialog reads node.images, and mark it
        # touched so whatever the user ends up with gets persisted on save.
        import lineage_images
        lineage_images.populate_node_images(self._tree, self._filepath, node_id)

        # Pre-compute current role so the combo shows the right value
        initial_role = ""
        for child in self._tree.nodes.values():
            r = child.parent_roles.get(node_id, "")
            if r:
                initial_role = r
                break

        dlg = PlantDialog(node, self, initial_role=initial_role)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._scene.update_node(node)
            # Only update the DB if this node's UUID is already a DB entry.
            # Lineage-only clones (e.g. from an older app version that used
            # clone_for_lineage with a new UUID) must NOT create new DB entries
            # just because the user changed a year or name in the lineage.
            if plant_db.has_node(node_id):
                plant_db.upsert(node)
                self._grid_view.mark_dirty()

            # Save the role chosen in the dialog back to ALL children of this node
            role = dlg.get_role()
            if role:
                for child in self._tree.nodes.values():
                    if node_id in child.parent_ids:
                        child.parent_roles[node_id] = role
                self._tree.touch()
                self._scene._rebuild_edges()

            if self._tree: self._props.update_node(node, self._tree)
            self._status(f"Updated: {node.name}")

    def _delete_selected(self) -> None:
        if not self._tree: return
        sel = [i for i in self._scene.selectedItems() if isinstance(i, PlantNodeItem)]
        if not sel: self._status("Select a node first."); return
        names = ", ".join(i.node.name for i in sel)
        reply = QMessageBox.question(
            self, "Delete", f"Delete  '{names}'?\nAll connections will be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Yes:
            self._scene.delete_selected(); self._props.clear()
            self._update_node_count(); self._status(f"Deleted: {names}")

    def _add_parent(self, child_id: str) -> None: self._add_relation(child_id, role="child")
    def _add_offspring(self, parent_id: str) -> None: self._add_relation(parent_id, role="parent")

    def _add_relation(self, anchor_id: str, role: str) -> None:
        if not self._tree: return
        existing = [(f"{n.name}  ({n.species or '—'})", nid)
                    for nid, n in self._tree.nodes.items() if nid != anchor_id]
        labels = ["➕  Create new plant…"] + [lbl for lbl, _ in existing]
        choice, ok = QInputDialog.getItem(
            self, "Add Relation", "Choose or create a plant:", labels, 0, False)
        if not ok: return
        if choice == "➕  Create new plant…":
            node = PlantNode()
            dlg  = PlantDialog(node, self, is_new=True)
            if dlg.exec() != QDialog.DialogCode.Accepted: return
            self._scene.add_node(node); plant_db.upsert(node); other_id = node.id
        else:
            idx = labels.index(choice) - 1; other_id = existing[idx][1]
        if role == "child":
            ok2 = self._scene.apply_connection(other_id, anchor_id)
            if ok2: self._ask_role(other_id, anchor_id)
        else:
            ok2 = self._scene.apply_connection(anchor_id, other_id)
            if ok2: self._ask_role(anchor_id, other_id)
        if not ok2:
            QMessageBox.warning(self, "Connection Error",
                                "Could not connect — cycle or already 2 parents.")
        self._update_node_count()

    def _connect_dialog(self) -> None:
        if not self._tree:
            flash_status(self, "Open a lineage first."); return
        sel = self._scene.selectedItems()
        if not sel or not isinstance(sel[0], PlantNodeItem):
            flash_status(self, "Select a node first."); return
        anchor_id = sel[0].node.id
        dlg = ConnectDialog(self._tree, anchor_id, self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        if dlg.selected_id is None: return
        if dlg.direction == "parent":
            ok = self._scene.apply_connection(dlg.selected_id, anchor_id)
            if ok: self._ask_role(dlg.selected_id, anchor_id)
        else:
            ok = self._scene.apply_connection(anchor_id, dlg.selected_id)
            if ok: self._ask_role(anchor_id, dlg.selected_id)
        if not ok:
            QMessageBox.warning(self, "Connection Error",
                                "Could not connect — cycle or already 2 parents.")

    def _on_connect_mode_changed(self, direction: str) -> None:
        if direction == "top":
            self._status("Connect mode — click another plant's bottom port (or any card) to add a PARENT. Right-click to cancel.")
        elif direction == "bottom":
            self._status("Connect mode — click another plant's top port (or any card) to add a CHILD. Right-click to cancel.")
        else:
            self._status("Connection cancelled.")

    def _on_connect_requested(self, from_id: str, to_id: str, direction: str) -> None:
        """
        Port semantics (no confirmation popup needed — direction is unambiguous):
          direction == "top":    from_id dragged from its TOP port  → from is the CHILD
          direction == "bottom": from_id dragged from its BOTTOM port → from is the PARENT
        """
        if not self._tree: return
        nf = self._tree.nodes.get(from_id)
        nt = self._tree.nodes.get(to_id)
        if not nf or not nt: return

        if direction == "top":
            # Dragged from top port: "I am looking for a parent"
            parent_id, child_id = to_id, from_id
        else:
            # Dragged from bottom port: "I am looking for a child"
            parent_id, child_id = from_id, to_id

        ok = self._scene.apply_connection(parent_id, child_id)
        if ok:
            self._ask_role(parent_id, child_id)
        else:
            QMessageBox.warning(self, "Error", "Could not connect — cycle or 2 parents already.")

    def _ask_role(self, parent_id: str, child_id: str) -> None:
        if not self._tree: return
        parent = self._tree.nodes.get(parent_id)
        child  = self._tree.nodes.get(child_id)
        if not parent or not child: return
        dlg = RoleDialog(parent.name, child.name, self); dlg.exec()
        child.parent_roles[parent_id] = dlg.role
        self._tree.touch()
        # _rebuild_edges repopulates _role_map and recolours edges; a single
        # scene.update() then repaints all cards with the new role badges —
        # no need to call item.update() or item.refresh() in a manual loop.
        self._scene._rebuild_edges()
        self._scene.update()
        self._maybe_auto_name_f1(child_id)
        if self._props._current_node_id == child_id:
            self._props.update_node(child, self._tree)
        elif self._props._current_node_id == parent_id:
            self._props.update_node(parent, self._tree)

    def _maybe_auto_name_f1(self, child_id: str) -> None:
        if not self._settings.auto_name_f1: return
        if not self._tree: return
        child = self._tree.nodes.get(child_id)
        if not child or len(child.parent_ids) < 2: return
        mother_id = child.mother_id()
        father_id = child.father_id()
        if not mother_id or not father_id: return
        mother = self._tree.nodes.get(mother_id)
        father = self._tree.nodes.get(father_id)
        if not mother or not father: return
        default_names = {"Unnamed Plant", "Unnamed", "", "New Plant"}
        if child.name not in default_names: return
        auto_name = f"{mother.name} × {father.name} F1"
        child.name = auto_name
        item = self._scene._node_items.get(child_id)
        if item: item.refresh()
        self._tree.touch()
        self._status(f"Auto-named: {auto_name}")

    # ── Undo ──────────────────────────────────────────────────────────────────

    def _undo(self) -> None:
        self._scene.undo(); self._update_undo_state(); self._status("Undo")

    def _update_undo_state(self) -> None:
        self._act_undo.setEnabled(bool(self._scene._undo_stack))

    # ── Copy / Paste ──────────────────────────────────────────────────────────

    def _copy_nodes(self) -> None:
        n = self._scene.copy_selected_nodes()
        if n: self._status(f"Copied {n} node(s) — Ctrl+Shift+V to paste")
        else: self._status("Select nodes first (Ctrl+drag), then copy.")

    def _paste_nodes(self) -> None:
        if not self._tree:
            flash_status(self, "Open a lineage first."); return
        n = self._scene.paste_nodes(QPointF(80, 80))
        if n: self._update_node_count(); self._status(f"Pasted {n} node(s)")
        else: self._status("Nothing to paste — copy nodes first.")

    # ── Comment ───────────────────────────────────────────────────────────────

    def _add_comment(self) -> None:
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        text, ok = QInputDialog.getMultiLineText(self, "Add Comment", "Comment text:", "")
        if not ok or not text.strip(): return
        vr = self._view.mapToScene(self._view.viewport().rect()).boundingRect()
        note = CommentNote(text=text.strip(), x=vr.center().x()-110, y=vr.center().y()-45)
        self._scene.add_comment(note); self._status("Comment added — drag corner to resize.")

    # ── Plant database ────────────────────────────────────────────────────────

    def _open_plant_db(self) -> None:
        dlg = PlantDBDialog(self)
        dlg.import_requested.connect(self._import_from_db); dlg.exec()
        # Always mark the grid dirty so it reloads any adds/edits/deletes
        # that happened inside the DB dialog.
        self._grid_view.mark_dirty()
        if self._grid_view.isVisible():
            self._grid_view._refresh()

    def _restore_last_view(self) -> None:
        """Reopen on whichever view (lineage canvas / grid / spreadsheet /
        notes) was active when the app was last closed. Reuses the normal
        switch methods so toolbar labels/tooltips end up in the correct
        state too."""
        view = getattr(self._settings, "last_active_view", "lineage")
        if view == "grid":
            self._open_grid_view()
        elif view == "sheet":
            self._open_spreadsheet_view()
        elif view == "notes":
            self._open_notes_view()
        # "lineage" needs no action — _main_stack already defaults to index 0.

    def _open_grid_view(self) -> None:
        """Switch to the card grid view. Clicking again while already
        there toggles back to the lineage canvas."""
        if self._main_stack.currentIndex() == 1 and self._grid_view.current_mode() == "cards":
            self._show_lineage_view()
            self._status("Lineage View")
            return
        self._main_stack.setCurrentIndex(1)
        self._grid_view.show_card_grid()
        self._reset_view_toggle_labels()
        self._act_grid_view.setText("⊞  Lineage View")
        self._act_grid_view.setToolTip("Switch back to lineage view  (Ctrl+Tab)")
        self._status("Grid View — right-click a plant to Add to Lineage")
        self._save_last_view()

    def _open_spreadsheet_view(self) -> None:
        """Switch to the spreadsheet view. Clicking again while already
        there toggles back to the lineage canvas."""
        if self._main_stack.currentIndex() == 1 and self._grid_view.current_mode() == "sheet":
            self._show_lineage_view()
            self._status("Lineage View")
            return
        self._main_stack.setCurrentIndex(1)
        self._grid_view.show_spreadsheet()
        self._reset_view_toggle_labels()
        self._act_sheet_view.setText("📊  Lineage View")
        self._act_sheet_view.setToolTip("Switch back to lineage view")
        self._status("Spreadsheet View")
        self._save_last_view()

    def _open_notes_view(self) -> None:
        """Switch to Notes. Clicking again while already there toggles
        back to the lineage canvas."""
        if self._main_stack.currentIndex() == 2:
            self._show_lineage_view()
            self._status("Lineage View")
            return
        self._main_stack.setCurrentIndex(2)
        self._reset_view_toggle_labels()
        self._act_notes_view.setText("📝  Lineage View")
        self._act_notes_view.setToolTip("Switch back to lineage view  (Ctrl+Tab)")
        self._status("Notes — click a note to open it, or ＋ New Note to add one")
        self._save_last_view()

    def _import_from_db(self, node: PlantNode) -> None:
        if self._tree is None:
            flash_status(self, "Open a lineage first."); return
        # Nodes from grid_view._all_plants have images=[] for RAM efficiency.
        # Populate them now so the lineage file stores the actual image data.
        # (self._scene.add_node(), called below, marks node.id as
        # authoritative in the lineage's own lazy-image tracking whenever
        # node.images/thumb is set, so the sidecar merge on save picks it up.)
        plant_db.populate_images(node)
        vr = self._view.mapToScene(self._view.viewport().rect()).boundingRect()
        node.x = vr.center().x(); node.y = vr.center().y()
        self._scene.add_node(node); self._update_node_count()
        # The plant database's copy is now redundant — this lineage node
        # holds its own list independently — so free it rather than keeping
        # the same image bytes resident in two places for the rest of the
        # session.
        plant_db.evict_images(node.id)
        self._status(f"Imported '{node.name}' from plant database.")

    # ── Global paste ──────────────────────────────────────────────────────────

    def _global_paste(self) -> None:
        cb = QApplication.clipboard(); img = cb.image()
        has_image = not img.isNull()
        clip_path: Optional[str] = None
        if not has_image:
            txt = cb.text().strip()
            if txt and txt.lower().endswith((".png",".jpg",".jpeg",".webp",".bmp")):
                clip_path = txt
        if not has_image and clip_path is None:
            self._status("No image on clipboard."); return
        if self._scene.paste_image_to_selected():
            self._status("Image pasted onto selected plant."); return
        if self._tree is None:
            flash_status(self, "Create or open a lineage first."); return
        node = PlantNode()
        if has_image:
            try:
                from PyQt6.QtCore import QBuffer, QByteArray
                buf = QByteArray(); qbuf = QBuffer(buf)
                qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
                img.save(qbuf, *__import__("image_utils").photo_format(img)); qbuf.close()
                if buf.size() > 0: node.images.append(base64.b64encode(bytes(buf)).decode())
            except Exception as e: log.error("_global_paste: %s", e)
        elif clip_path:
            try:
                with open(clip_path,"rb") as fh: node.images.append(base64.b64encode(fh.read()).decode())
            except Exception: pass
        dlg = PlantDialog(node, self, is_new=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vr = self._view.mapToScene(self._view.viewport().rect()).boundingRect()
            node.x = vr.center().x(); node.y = vr.center().y()
            self._scene.add_node(node); plant_db.upsert(node)
            self._update_node_count(); self._status(f"Created '{node.name}' from pasted image.")

    def _import_csv(self) -> None:
        """Import a CSV file as a new sheet in the Spreadsheet View."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import CSV Files", "",
            "CSV Files (*.csv);;Tab-Separated (*.tsv *.txt);;All Files (*)")
        if not paths:
            return
        # Switch to Grid View → Spreadsheet
        if self._main_stack.currentIndex() != 1:
            self._main_stack.setCurrentIndex(1)
            self._act_grid_view.setText("⊞  Lineage View")
        # Make sure the spreadsheet page is active inside grid view
        self._grid_view.show_spreadsheet()
        for path in paths:
            self._grid_view.import_csv(path)
        self._status(f"Imported {len(paths)} CSV file(s) into Spreadsheet View.")

    def _export_data_bundle(self) -> None:
        """Let the user pick which lineages (and whether to include the
        whole plant database) go into a shareable .bcbundle file."""
        dlg = ExportBundleDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        paths = dlg.selected_lineage_paths()
        include_db = dlg.include_plant_db()
        if not paths and not include_db:
            flash_status(self, "Nothing selected to export."); return

        default_name = "BreedingCharts_Export" + data_bundle.BUNDLE_EXT
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export Data Bundle", default_name,
            f"BreedingCharts Bundle (*{data_bundle.BUNDLE_EXT});;All Files (*)")
        if not out_path:
            return
        if not out_path.lower().endswith(data_bundle.BUNDLE_EXT):
            out_path += data_bundle.BUNDLE_EXT

        # Flush the currently-open lineage so an export right after editing
        # includes the latest state rather than the last autosave.
        self._save_current_silently()

        ok = data_bundle.export_bundle(out_path, paths, include_plant_db=include_db)
        if ok:
            QMessageBox.information(self, "Export Complete",
                f"Exported {len(paths)} lineage(s)"
                f"{' + the plant database' if include_db else ''} to:\n{out_path}\n\n"
                "Send this file to whoever you're sharing with — importing it "
                "on their end will never overwrite anything they already have.")
        else:
            QMessageBox.critical(self, "Export Failed",
                f"Could not write the bundle to:\n{out_path}")

    def _import_data_bundle(self) -> None:
        """Import a .bcbundle previously exported by _export_data_bundle
        (on this machine or another). Always additive — see
        data_bundle.py's module docstring for the exact merge rules."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Data Bundle", "",
            f"BreedingCharts Bundle (*{data_bundle.BUNDLE_EXT} *.zip);;All Files (*)")
        if not path:
            return

        result = data_bundle.import_bundle(path)
        if not result.ok:
            QMessageBox.critical(self, "Import Failed",
                f"Could not import that bundle:\n{result.error}")
            return

        self._refresh_all_views()

        lines = []
        if result.plants_added:
            lines.append(f"{result.plants_added} new plant(s) added to the database.")
        if result.lineages_added:
            names = ", ".join(result.added_lineage_names)
            lines.append(f"{result.lineages_added} new lineage(s) added: {names}")
        if result.lineages_skipped:
            lines.append(f"{result.lineages_skipped} lineage(s) were already present "
                         "and were skipped.")
        if not lines:
            lines.append("Nothing new to add — everything in that bundle is already here.")
        QMessageBox.information(self, "Import Complete", "\n".join(lines))

    def _import_plant_db(self) -> None:
        """Manually import a plant_database.json file and merge it into the
        current database.

        This is the recovery path for users whose plants went missing after
        upgrading (the automatic migration only covers the standard legacy
        location; this handles files kept anywhere on disk).
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Plant Database", "",
            "Plant Database JSON (plant_database.json *.json);;All Files (*)")
        if not path:
            return

        added = plant_db.import_from_file(path)

        if added < 0:
            QMessageBox.critical(self, "Import Failed",
                f"Could not read or parse the file:\n{path}\n\n"
                "Make sure it is a valid plant_database.json created by BreedingCharts.")
            return

        self._refresh_all_views()

        if added == 0:
            flash_status(self, "No new plants found — everything in that file is "
                               "already in your database.", msec=4000)
        else:
            flash_status(self,
                f"Imported {added} new plant{'s' if added != 1 else ''} — "
                f"switch to Grid View to see them.", msec=4500)

    def _show_tour(self) -> None:
        """Show (or restart) the guided tour with live widget highlighting."""
        # Collect toolbar button widgets by searching toolbar actions by text
        widget_map = {
            "sidebar":       self._sidebar,
            "canvas":        self._view,
        }
        # Find toolbar buttons by their action text
        for tb in self.findChildren(QToolBar):
            for action in tb.actions():
                txt = action.text().replace("\u2009", " ").strip()
                btn = tb.widgetForAction(action)
                if btn is None:
                    continue
                if "New Plant" in txt:
                    widget_map["btn_add_plant"] = btn
                elif "Connect" in txt:
                    widget_map["btn_connect"] = btn
                elif "Grid View" in txt or "Lineage View" in txt:
                    widget_map["btn_grid_view"] = btn
                elif "Export PNG" in txt:
                    widget_map["btn_export"] = btn

        dlg = TourDialog(self, widget_map=widget_map)
        dlg.exec()
        self._settings.tour_shown = True
        self._schedule_settings_save()

    # ── Auto-layout ───────────────────────────────────────────────────────────

    def _auto_layout(self) -> None:
        if self._tree is None: return
        self._scene.auto_layout()
        self._view.fit_view()
        self._status("Auto-layout applied — mother left, father right.")

    def _auto_organize(self) -> None:
        if self._tree is None: return
        self._scene.auto_organize()
        self._view.fit_view()
        self._status("Auto-organized — crossing lines minimized.")

    # ── Key events ────────────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._scene.selectedItems():
                self._delete_selected(); event.accept(); return
        if event.key() == Qt.Key.Key_Question:
            self._show_shortcuts_overlay(); event.accept(); return
        if event.key() == Qt.Key.Key_F and not event.modifiers():
            self._view.fit_view(); event.accept(); return
        super().keyPressEvent(event)

    def _show_shortcuts_overlay(self) -> None:
        """Quick-reference keyboard shortcut overlay dialog."""
        from PyQt6.QtWidgets import QTextBrowser
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard Shortcuts")
        dlg.setMinimumSize(460, 440)
        lay = QVBoxLayout(dlg); lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("⌨️  Keyboard Shortcuts")
        title.setStyleSheet("font-size:15px; font-weight:bold; margin-bottom:8px;")
        lay.addWidget(title)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(False)
        tb.setStyleSheet("font-size:13px; border:none; background:transparent;")
        shortcuts = [
            ("General", [
                ("Ctrl+S",          "Save current lineage"),
                ("Ctrl+Z",          "Undo last action"),
                ("Ctrl+Shift+V",    "Paste copied nodes"),
                ("Ctrl+V",          "Paste image from clipboard"),
                ("Del / Backspace", "Delete selected node"),
                ("F",               "Fit canvas to view"),
                ("?",               "Show this shortcut overlay"),
            ]),
            ("Canvas", [
                ("Ctrl+F",               "Search plants on canvas"),
                ("Click port (top)",    "Start connection — this node seeks a parent"),
                ("Click port (bottom)", "Start connection — this node seeks a child"),
                ("Right-click edge",    "Delete that connection"),
                ("Right-click canvas",  "Add plant / comment at position"),
                ("Right-click node",    "Node context menu"),
                ("Middle-drag",         "Pan canvas"),
                ("Scroll",              "Pan canvas (Shift = horizontal)"),
                ("Ctrl+Scroll",         "Zoom in / out"),
                ("Ctrl+drag",           "Rubber-band select multiple nodes"),
            ]),
            ("Grid view", [
                ("Click card",         "Select plant"),
                ("Ctrl+click card",    "Add to / remove from selection"),
                ("Del / Backspace",    "Delete all selected plants"),
                ("Double-click card",  "Edit plant"),
            ]),
        ]
        html = "<table style='border-collapse:collapse; width:100%;'>"
        for section, rows in shortcuts:
            html += (f"<tr><td colspan='2' style='padding:10px 4px 4px 0; "
                     f"font-weight:bold; color:#6bcc80; font-size:13px;'>{section}</td></tr>")
            for key, desc in rows:
                html += (f"<tr><td style='padding:3px 16px 3px 8px; white-space:nowrap; "
                         f"font-family:monospace; font-size:12px; "
                         f"color:#f0c070;'>{key}</td>"
                         f"<td style='padding:3px 4px; font-size:12px;'>{desc}</td></tr>")
        html += "</table>"
        tb.setHtml(html)
        lay.addWidget(tb, 1)
        ok = QPushButton("Close"); ok.setFixedWidth(80)
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    # ── Scene callbacks ───────────────────────────────────────────────────────

    def _on_node_selected(self, node_id: str) -> None:
        if node_id and self._tree and node_id in self._tree.nodes:
            if self._settings.show_props_panel:
                self._props.update_node(self._tree.nodes[node_id], self._tree)
        else:
            self._props.clear()

    def _on_tree_changed(self) -> None:
        if self._loading: return
        self._unsaved = True
        self._update_title()
        self._update_node_count()
        self._update_undo_state()
        # Restart the debounce timer so we save 1.5 s after the *last* change,
        # not after the first one.  This covers rapid edits (dragging, typing)
        # without hammering the disk on every event.
        if self._filepath:
            self._debounce_timer.start()   # restart if already running

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _update_title(self) -> None:
        if self._tree:
            dot = " •" if self._unsaved else ""
            self.setWindowTitle(f"🌿  {self._tree.name}{dot}  —  BreedingCharts")
        else:
            self.setWindowTitle("🌿  BreedingCharts")

    def _update_node_count(self) -> None:
        if self._tree:
            n = len(self._tree.nodes)
            self._statusbar.showMessage(
                f"{self._tree.name}  •  {n} plant{'s' if n!=1 else ''}", 0)

    def _status(self, msg: str) -> None: self._statusbar.showMessage(msg, 6000)
    def _about(self) -> None: AboutDialog(self).exec()

    def _refresh_all_views(self) -> None:
        """Refresh every view that shows data pulled from disk: the
        sidebar's list of lineages, the card grid / spreadsheet of all
        plants, and Notes. Call this after anything that changes what's on
        disk out from under the currently-displayed views — changing the
        data folder, importing a data bundle, importing a plant database
        JSON — so nothing has to wait for the user to notice and click the
        sidebar's own Refresh button."""
        self._sidebar.refresh()
        self._grid_view.mark_dirty()
        if self._main_stack.currentIndex() == 1:
            self._grid_view._refresh()
        self._notes_view.mark_dirty()
        if self._main_stack.currentIndex() == 2:
            self._notes_view.refresh()

    def _open_lineage_for_new_data_dir(self) -> None:
        """After switching to a different data folder, get some lineage
        open for it instead of leaving the canvas showing whatever was
        open from the previous folder — that combination (sidebar listing
        the new folder's lineages, canvas showing one that isn't even in
        it) is exactly the kind of stale/inconsistent state this is meant
        to avoid. Mirrors the startup fallback: first lineage found in the
        new folder, or a fresh starter lineage if it's empty."""
        files = get_lineage_files(get_data_dir())
        if files:
            tree = load_lineage(files[0])
            if tree:
                self._load_tree(tree, filepath=files[0])
                self._status(f"Opened: {os.path.basename(files[0])}")
                return
        tree = LineageTree(name="My Lineage")
        filepath = default_filepath(tree.name, directory=get_data_dir())
        save_lineage(tree, filepath)
        self._load_tree(tree, filepath=filepath)
        self._unsaved = False
        self._update_title()
        self._status("This folder is empty — a starter lineage 'My Lineage' has been created.")

    def _change_data_folder(self) -> None:
        """Let the user choose a new folder for all lineage + database files.

        Why this matters: by storing data outside the app directory, software
        updates no longer require manually copying .plf files.  The chosen
        folder is written to data_dir.txt in the app directory and to the
        app settings so it persists across launches.

        This is one of two entry points to the same folder switch — the
        other is the "Browse…" button on the Data Folder page in Settings.
        Both funnel into _apply_new_data_dir() so behavior (refreshing the
        sidebar/grid/notes, reopening a lineage for the new folder) is
        identical no matter which one someone uses.
        """
        current = get_data_dir()
        new_dir = QFileDialog.getExistingDirectory(
            self, "Choose Data Folder (for lineages + plant database)",
            current,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if not new_dir or new_dir == current:
            return

        if not self._confirm_data_folder_change(current, new_dir):
            return
        self._apply_new_data_dir(new_dir)

    def _confirm_data_folder_change(self, current: str, new_dir: str) -> bool:
        reply = QMessageBox.question(
            self, "Change Data Folder",
            f"<b>New folder:</b><br>{new_dir}<br><br>"
            f"BreedingCharts will look for and save lineages there from now on.<br>"
            f"Existing files in <code>{current}</code> are <b>not</b> moved automatically — "
            f"copy them across manually if needed.<br><br>"
            f"Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        return reply == QMessageBox.StandardButton.Yes

    def _apply_new_data_dir(self, new_dir: str) -> bool:
        """Actually switch to a new data folder and bring every view up to
        date with it. This is the one and only place that logic lives —
        previously the Settings dialog's own Data Folder browser called
        database.set_data_dir() directly and stopped there, so the
        sidebar, grid, and notes kept showing the *old* folder's data
        until something else happened to refresh them. Returns True on
        success."""
        # Flush any pending edits to the lineage that's about to be left
        # behind — it still lives in the old folder and remains valid
        # there, we just don't want to silently lose an in-progress edit.
        self._save_current_silently()

        ok = set_data_dir(new_dir)
        if not ok:
            QMessageBox.critical(self, "Error",
                f"Could not set data folder:\n{new_dir}\n\nCheck write permissions.")
            return False

        # Invalidate the plant database + notes caches so the next access
        # reads from the new location.
        plant_db.reset_cache()
        notes_store.reset_cache()

        # Persist in settings too so main.py can re-apply on next launch
        self._settings.data_dir = new_dir
        self._schedule_settings_save()

        # Automatically bring every view up to date with the new folder —
        # equivalent to pressing Refresh, but the user shouldn't have to.
        self._refresh_all_views()
        self._open_lineage_for_new_data_dir()
        self._status(f"Data folder changed to:  {new_dir}")
        return True

    def _toggle_debug(self, enabled: bool) -> None:
        """Enable/disable live performance logging at runtime."""
        import os as _os
        if enabled:
            log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "debug.log")
            perf.enable(log_filepath=log_path)
            self._status(f"Debug logging enabled → debug.log")
        else:
            perf.disable()
            self._status("Debug logging disabled.")

    def _show_perf_stats(self) -> None:
        """Show an in-app dialog with accumulated timing statistics."""
        table = perf.format_stats_table()
        dlg = QDialog(self)
        dlg.setWindowTitle("Performance Timing Statistics")
        dlg.setMinimumSize(720, 400)
        lay = QVBoxLayout(dlg)
        from PyQt6.QtWidgets import QTextEdit, QDialogButtonBox
        te = QTextEdit()
        te.setReadOnly(True)
        te.setFontFamily("Courier New")
        te.setPlainText(table if table != "No timing data collected yet."
                        else "No timing data yet.\n\nEnable debug logging (Help → Debug → "
                             "Enable Performance Logging), perform some operations, "
                             "then re-open this dialog.")
        lay.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _build_ram_breakdown_text(self) -> str:
        """Assemble a human-readable RAM breakdown across every subsystem
        that keeps meaningful data resident, so it's clear WHERE memory is
        actually going rather than just a single opaque total."""
        import plant_db
        b = perf.format_bytes
        lines: list[str] = []

        rss = perf.process_rss_bytes()
        lines.append(f"Process working set (total):  {b(rss)}")
        lines.append("")

        db = plant_db.debug_ram_stats()
        lines.append("Plant database (Grid View):")
        lines.append(f"  Metadata cache            {db['meta_plant_count']:>5} plants   "
                      f"~{b(db['meta_bytes_approx'])}")
        lines.append(f"  Plants with a photo       {db['has_image_count']:>5}")
        lines.append(f"  Full-res images resident  {db['images_resident_plant_count']:>5} plants   "
                      f"~{b(db['images_resident_bytes_approx'])}")
        if db['images_resident_plant_count'] > 0:
            lines.append("    (should drop back toward 0 shortly after closing an edit "
                          "dialog or importing a plant — if this count keeps climbing "
                          "and never comes back down, that's the leak to look at)")
        lines.append("")

        gv = self._grid_view.debug_ram_stats()
        lines.append("Grid View widgets:")
        lines.append(f"  Cached card widgets       {gv['cached_card_widgets']:>5}")
        lines.append(f"  Plants in current list    {gv['all_plants_loaded']:>5}")
        lines.append("")

        lines.append("Current Lineage:")
        if self._tree is not None:
            loaded = len(self._tree._image_loaded_ids)
            loaded_bytes = sum(
                len(s)
                for nid in self._tree._image_loaded_ids
                for s in (self._tree.nodes.get(nid).images if self._tree.nodes.get(nid) else [])
            )
            lines.append(f"  Nodes                     {len(self._tree.nodes):>5}")
            lines.append(f"  Full-res images resident  {loaded:>5} nodes   ~{b(loaded_bytes)}")
        else:
            lines.append("  (none open)")
        lines.append("")

        from PyQt6.QtGui import QPixmapCache
        mode = "manual" if getattr(self._settings, "pixmap_cache_mb", 0) > 0 else "auto"
        lines.append("Qt pixmap cache (card thumbnails on screen):")
        lines.append(f"  Configured limit          ~{QPixmapCache.cacheLimit() / 1024:.0f} MB "
                      f"({mode}; doesn't crash if exceeded — Qt evicts oldest entries "
                      "and redecodes them if scrolled back into view)")
        if mode == "auto":
            lines.append(f"  Recommended for this DB   ~{plant_db.recommended_pixmap_cache_kb() / 1024:.0f} MB "
                          "(applied at next launch, since the DB can grow during a session)")

        return "\n".join(lines)

    def _show_ram_usage(self) -> None:
        """Show an in-app dialog breaking RAM usage down by subsystem.

        Unlike the timing stats, this doesn't require debug logging to be
        enabled — it's a live snapshot computed on demand.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("RAM Usage Breakdown")
        dlg.setMinimumSize(640, 460)
        lay = QVBoxLayout(dlg)
        from PyQt6.QtWidgets import QTextEdit, QDialogButtonBox
        te = QTextEdit()
        te.setReadOnly(True)
        te.setFontFamily("Courier New")
        te.setPlainText(self._build_ram_breakdown_text())
        lay.addWidget(te)
        btn_row = QHBoxLayout()
        btn_refresh = QPushButton("⟳ Refresh")
        btn_refresh.clicked.connect(lambda: te.setPlainText(self._build_ram_breakdown_text()))
        btn_row.addWidget(btn_refresh)
        btn_row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        btn_row.addWidget(bb)
        lay.addLayout(btn_row)
        dlg.exec()

    def closeEvent(self, event) -> None:
        self._settings.window_maximized = self.isMaximized()
        try:
            self._settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        except Exception as e:
            log.warning("Could not save window geometry: %s", e)
        save_settings(self._settings)
        # Let any in-flight background debounce-save finish before the
        # synchronous quit-save below — both write the same file, and
        # letting them race could corrupt it or have the stale in-flight
        # write clobber the newer one.
        if self._save_worker is not None and self._save_worker.isRunning():
            self._save_worker.wait(3000)
        self._save_current_silently()
        if self._unsaved:
            reply = QMessageBox.question(
                self, "Quit", "Could not autosave. Save before quitting?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if reply == QMessageBox.StandardButton.Save:
                if not self._save(): event.ignore(); return
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore(); return
        event.accept()
