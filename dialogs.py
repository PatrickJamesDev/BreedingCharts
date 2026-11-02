"""
dialogs.py — All modal dialogs.

Changes:
  • PlantDialog Info tab: Mother/Father role combo under Cultivar
  • SuggestLineEdit: custom inline autocomplete for Name, Species, Cultivar
    (replaces QCompleter which is unreliable in modal dialogs on PyQt6)
  • PlantDBDialog: 1.75× size, Add New Plant button, sort by Name/Species,
    imported plant highlighted, drag-drop image to create new plant
  • Word-wrap comment editor (no word splitting)
  • RoleDialog unchanged
"""
from __future__ import annotations
import base64, logging, os
from typing import List, Optional, TYPE_CHECKING

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QTextEdit, QPushButton, QListWidget, QListWidgetItem,
    QFileDialog, QColorDialog, QDialogButtonBox, QGroupBox, QApplication,
    QFrame, QMessageBox, QScrollArea, QSizePolicy, QComboBox,
    QAbstractItemView, QCheckBox,
)
from PyQt6.QtGui import QPixmap, QColor, QFont, QImage, QIcon
from PyQt6.QtCore import Qt, QSize, QBuffer, QByteArray, QObject, QEvent, pyqtSignal, QPoint, QTimer

from ui_common import FadeDialog, flash_status, mark_field_invalid, clear_field_invalid

if TYPE_CHECKING:
    from models import PlantNode, LineageTree

log = logging.getLogger(__name__)


# ── Autocomplete database ─────────────────────────────────────────────────────

class AutocompleteDB:
    """Maintains in-memory lists of known names/species/cultivars."""
    _names:     List[str] = []
    _species:   List[str] = []
    _cultivars: List[str] = []

    @classmethod
    def load_from_plant_db(cls) -> None:
        try:
            import plant_db
            all_plants = plant_db.get_all()
            cls._names     = sorted(set(p.name     for p in all_plants if p.name))
            cls._species   = sorted(set(p.species  for p in all_plants if p.species))
            cls._cultivars = sorted(set(p.cultivar for p in all_plants if p.cultivar))
        except Exception as e:
            log.warning("AutocompleteDB.load: %s", e)

    @classmethod
    def words_for(cls, field: str) -> List[str]:
        if field == "name":     return cls._names
        elif field == "species": return cls._species
        else:                   return cls._cultivars


# ── Custom suggest dropdown ───────────────────────────────────────────────────

class SuggestLineEdit(QLineEdit):
    """QLineEdit that shows an inline dropdown list of matching suggestions.

    Unlike QCompleter, the dropdown is a child widget of the nearest
    QDialog ancestor, so it is always visible above the dialog content and
    never clipped by the operating system's window manager.
    """

    def __init__(self, text: str = "", words: List[str] = None,
                 palette: dict = None, parent=None):
        super().__init__(text, parent)
        self._words:   List[str] = words or []
        self._palette: dict      = palette or {}
        self._popup:   Optional[QListWidget] = None
        self._dialog_parent: Optional[QWidget] = None   # found lazily
        self.textEdited.connect(self._on_text_edited)

    def set_words(self, words: List[str]) -> None:
        self._words = words

    # ── popup lifecycle ───────────────────────────────────────────────────────

    def _find_dialog_parent(self) -> Optional[QWidget]:
        """Walk up to the first QDialog so we can parent the popup there."""
        w = self.parent()
        while w is not None:
            if isinstance(w, QDialog):
                return w
            w = w.parent() if hasattr(w, "parent") else None
        return self  # fallback

    def _ensure_popup(self) -> QListWidget:
        if self._popup is not None:
            return self._popup
        if self._dialog_parent is None:
            self._dialog_parent = self._find_dialog_parent()

        p = self._palette
        bg  = p.get("panel_bg", "#ffffff")
        txt = p.get("text",     "#111111")
        sel = p.get("accent",   "#3a7bd5")      # solid blue highlight like the image
        bdr = p.get("border",   "#c8c8c8")

        lw = QListWidget(self._dialog_parent)
        lw.setWindowFlags(Qt.WindowType.Widget)
        lw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lw.setStyleSheet(
            f"QListWidget {{"
            f"  background: {bg}; color: {txt};"
            f"  border: 1.5px solid {bdr};"
            f"  border-top: none;"
            f"  font-size: 13px; padding: 2px 0;"
            f"}}"
            f"QListWidget::item {{ padding: 6px 12px; }}"
            f"QListWidget::item:selected,"
            f"QListWidget::item:hover:selected {{"
            f"  background: {sel}; color: #ffffff;"
            f"}}"
            f"QListWidget::item:hover:!selected {{"
            f"  background: {p.get('btn_hover', '#e8f0e9')};"
            f"}}"
        )
        lw.itemClicked.connect(self._on_item_clicked)
        lw.hide()
        self._popup = lw
        return lw

    def _show_popup(self, matches: List[str]) -> None:
        lw = self._ensure_popup()
        lw.clear()
        for m in matches[:12]:
            lw.addItem(m)

        # Position popup below this widget in dialog-parent coordinates
        dlg = self._dialog_parent or self._find_dialog_parent()
        origin = self.mapTo(dlg, QPoint(0, self.height()))
        lw.move(origin)
        lw.setFixedWidth(self.width())

        row_h = 34
        lw.setFixedHeight(min(len(matches), 8) * row_h + 4)
        lw.raise_()
        lw.show()

    def _hide_popup(self) -> None:
        if self._popup:
            self._popup.hide()

    # ── signals ───────────────────────────────────────────────────────────────

    def _on_text_edited(self, text: str) -> None:
        text = text.strip()
        if not text or not self._words:
            self._hide_popup(); return
        tl = text.lower()
        # Exact starts-with first, then contains
        starts = [w for w in self._words if w.lower().startswith(tl)]
        contains = [w for w in self._words if tl in w.lower() and not w.lower().startswith(tl)]
        matches = starts + contains
        if matches:
            self._show_popup(matches)
        else:
            self._hide_popup()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self.setText(item.text())
        self._hide_popup()
        self.setFocus()

    # ── keyboard navigation ───────────────────────────────────────────────────

    def event(self, event) -> bool:
        """Override event() (not keyPressEvent) so we see Tab before the
        parent QDialog's focus-traversal logic consumes it."""
        from PyQt6.QtCore import QEvent
        if (event.type() == QEvent.Type.KeyPress and
                self._popup and self._popup.isVisible()):
            if event.key() == Qt.Key.Key_Tab:
                cur = self._popup.currentItem() or self._popup.item(0)
                if cur:
                    self._on_item_clicked(cur)
                return True   # consumed
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        lw = self._popup
        if lw and lw.isVisible():
            key = event.key()
            if key == Qt.Key.Key_Down:
                row = lw.currentRow()
                lw.setCurrentRow(min(row + 1, lw.count() - 1))
                return
            if key == Qt.Key.Key_Up:
                row = lw.currentRow()
                lw.setCurrentRow(max(row - 1, 0))
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                cur = lw.currentItem() or lw.item(0)
                if cur:
                    self._on_item_clicked(cur)
                return
            if key == Qt.Key.Key_Escape:
                self._hide_popup(); return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        # Small delay so item clicks register before the popup hides
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(150, self._hide_popup)
        super().focusOutEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._popup and self._popup.isVisible():
            dlg = self._dialog_parent or self._find_dialog_parent()
            origin = self.mapTo(dlg, QPoint(0, self.height()))
            self._popup.move(origin)
            self._popup.setFixedWidth(self.width())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64_to_pixmap(b64: str) -> Optional[QPixmap]:
    try:
        raw = base64.b64decode(b64); pix = QPixmap()
        pix.loadFromData(raw); return None if pix.isNull() else pix
    except Exception as e: log.warning("_b64_to_pixmap: %s", e); return None


def _qimage_to_b64(img: QImage) -> Optional[str]:
    try:
        buf = QByteArray(); qbuf = QBuffer(buf)
        qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
        ok = img.save(qbuf, *__import__("image_utils").photo_format(img)); qbuf.close()
        if ok and buf.size() > 0: return base64.b64encode(bytes(buf)).decode()
    except Exception as e: log.error("_qimage_to_b64: %s", e)
    return None


def _file_to_b64(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh: return base64.b64encode(fh.read()).decode()
    except Exception as e: log.error("_file_to_b64: %s", e); return None


class _ImageDropFilter(QObject):
    def __init__(self, on_enter, on_drop, parent=None):
        super().__init__(parent)
        self._on_enter = on_enter; self._on_drop = on_drop

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        if t == QEvent.Type.DragEnter:  self._on_enter(event); return True
        if t == QEvent.Type.DragMove:   event.acceptProposedAction(); return True
        if t == QEvent.Type.Drop:       self._on_drop(event); return True
        return False


# ── Plant editor ──────────────────────────────────────────────────────────────

class PlantDialog(FadeDialog):

    def __init__(self, node: PlantNode, parent=None, child_node: PlantNode = None,
                 initial_role: str = "", is_new: bool = False):
        """
        node         : the plant being edited. IMPORTANT: node.images must
                       already be populated by the caller before opening
                       this dialog — full-resolution images are stored
                       lazily (plant_db.populate_images() for the database
                       view, lineage_images.populate_node_images() for the
                       canvas view) and this dialog does not load them
                       itself, so it can be reused for either source.
        child_node   : if provided, role is read/written to child_node.parent_roles[node.id]
        initial_role : fallback role string if child_node is not provided
        is_new       : True when this dialog is creating a brand-new plant
                       (as opposed to editing an existing one). When True, the
                       Name field is focused and its placeholder text selected
                       so the user can start typing immediately.
        """
        super().__init__(parent)
        self.node       = node
        self.child_node = child_node
        self._initial_role = initial_role
        self._is_new    = is_new
        self._images: List[str] = list(node.images)
        self._original_main_b64 = self._images[0] if self._images else None
        self._icon_cache: dict = {}      # b64 -> QIcon, avoids re-decoding full-res on every list rebuild
        self._preview_cache: dict = {}   # b64 -> QPixmap (scaled preview size)
        AutocompleteDB.load_from_plant_db()
        # Try to get palette from parent window — walk up max 15 widget levels
        self._palette: dict = {}
        try:
            from PyQt6.QtWidgets import QWidget as _QW
            w = parent
            for _ in range(15):
                if w is None:
                    break
                if hasattr(w, '_settings'):
                    from settings import THEMES
                    self._palette = THEMES.get(w._settings.theme, THEMES["light"])
                    break
                if not isinstance(w, _QW):
                    break
                w = w.parent()
        except Exception:
            pass
        self.setWindowTitle(f"Edit Plant — {node.name or 'New'}")
        self.setMinimumWidth(600)
        self.setMinimumHeight(640)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self); root.setSpacing(12)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_info(),   "🌿  Info")
        self._tabs.addTab(self._tab_images(), "🖼️  Images")
        self._tabs.addTab(self._tab_notes(),  "📝  Notes")
        root.addWidget(self._tabs)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._commit); bb.rejected.connect(self.reject)
        root.addWidget(bb)

        if self._is_new:
            # Deferred via singleShot so it runs once the dialog is actually
            # visible (exec() starts its event loop right after __init__
            # returns) — focusing before that point can be silently dropped
            # by Qt's tab-widget/dialog default-focus logic.
            QTimer.singleShot(0, self._focus_name_field)

    def _focus_name_field(self) -> None:
        """Focus the Name field and select its placeholder text so a user
        adding a new plant can just start typing to replace it."""
        self._tabs.setCurrentIndex(0)   # make sure the Info tab (with Name) is showing
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def keyPressEvent(self, event) -> None:
        """Ctrl+V anywhere in the dialog pastes a clipboard image.

        • **Info tab** — image is added silently and the inline thumbnail
          updates; the user stays on the Info tab.
        • **Any other tab** — switches to the Images tab so the result is
          visible (original behaviour).
        • Text paste in QLineEdit / QTextEdit children is unaffected: those
          widgets consume Ctrl+V before it reaches this handler.
        """
        from PyQt6.QtCore import Qt as _Qt
        if event.modifiers() == _Qt.KeyboardModifier.ControlModifier and \
                event.key() == _Qt.Key.Key_V:
            img = QApplication.clipboard().image()
            if not img.isNull():
                on_info_tab = self._tabs.currentIndex() == 0
                self._paste_image()                   # always adds to self._images
                if on_info_tab:
                    self._update_info_img_preview()   # refresh thumbnail, stay here
                else:
                    self._tabs.setCurrentIndex(1)     # show Images tab result
                event.accept()
                return
        super().keyPressEvent(event)

    def _tab_info(self) -> QWidget:
        w = QWidget(); out = QVBoxLayout(w)
        out.setContentsMargins(0, 0, 0, 0); out.setSpacing(0)
        fw = QWidget(); form = QFormLayout(fw)
        form.setSpacing(10); form.setContentsMargins(16, 16, 16, 8)

        # Name with autocomplete
        self._name_edit = SuggestLineEdit(
            self.node.name,
            words=AutocompleteDB.words_for("name"),
            palette=self._palette, parent=self)
        form.addRow("Name *", self._name_edit)
        self._name_error_lbl = QLabel(""); self._name_error_lbl.setVisible(False)
        form.addRow("", self._name_error_lbl)
        self._name_edit.textChanged.connect(
            lambda: clear_field_invalid(self._name_edit, self._name_error_lbl))

        # Species with autocomplete
        self._species_edit = SuggestLineEdit(
            self.node.species,
            words=AutocompleteDB.words_for("species"),
            palette=self._palette, parent=self)
        form.addRow("Species", self._species_edit)

        # Cultivar with autocomplete
        self._cultivar_edit = SuggestLineEdit(
            self.node.cultivar,
            words=AutocompleteDB.words_for("cultivar"),
            palette=self._palette, parent=self)
        form.addRow("Cultivar", self._cultivar_edit)

        # Mother/Father role — shown always but only meaningful when plant is a parent
        self._role_combo = QComboBox()
        self._role_combo.addItems(["Unknown / Not set", "♀  Mother", "♂  Father"])
        # Determine current role from child_node if provided, else from initial_role
        current_role = ""
        if self.child_node:
            current_role = self.child_node.parent_roles.get(self.node.id, "")
        elif self._initial_role:
            current_role = self._initial_role
        if current_role == "mother":   self._role_combo.setCurrentIndex(1)
        elif current_role == "father": self._role_combo.setCurrentIndex(2)
        else:                          self._role_combo.setCurrentIndex(0)
        form.addRow("Role in cross", self._role_combo)

        self._desc_edit = QTextEdit(self.node.description)
        self._desc_edit.setFixedHeight(70)
        form.addRow("Description", self._desc_edit)

        self._year_edit = QLineEdit()
        self._year_edit.setPlaceholderText("e.g. 2024  (leave empty for none)")
        self._year_edit.setMaximumWidth(200)
        if self.node.year: self._year_edit.setText(str(self.node.year))
        form.addRow("Year", self._year_edit)
        self._year_error_lbl = QLabel(""); self._year_error_lbl.setVisible(False)
        form.addRow("", self._year_error_lbl)
        self._year_edit.textChanged.connect(
            lambda: clear_field_invalid(self._year_edit, self._year_error_lbl))

        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(80, 28)
        self._current_color = self.node.color or "#52a86a"
        self._refresh_color_btn(); self._color_btn.clicked.connect(self._pick_color)
        form.addRow("Node colour", self._color_btn)
        out.addWidget(fw)

        tg = QGroupBox("Trait Tags"); tgl = QVBoxLayout(tg); tgl.setSpacing(4)
        self._tag_list = QListWidget(); self._tag_list.setFixedHeight(80)
        self._tag_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for t in self.node.tags: self._tag_list.addItem(t)
        tr = QHBoxLayout()
        self._tag_input = QLineEdit(); self._tag_input.setPlaceholderText("Add tag…")
        self._tag_input.returnPressed.connect(self._add_tag)
        b_add = QPushButton("Add Tag"); b_add.setMinimumWidth(70); b_add.clicked.connect(self._add_tag)
        b_rem = QPushButton("Remove"); b_rem.setMinimumWidth(70); b_rem.clicked.connect(self._remove_tag)
        tr.addWidget(self._tag_input); tr.addWidget(b_add); tr.addWidget(b_rem)
        tgl.addWidget(self._tag_list); tgl.addLayout(tr)
        out.addWidget(tg)

        # ── Quick photo section ─────────────────────────────────────────────
        # Lets you paste / set the main card photo without leaving the Info tab.
        # Full image management (reorder, delete, browse) is on the Images tab.
        pg = QGroupBox("Photo  (main card image)")
        pl = QHBoxLayout(pg); pl.setSpacing(10); pl.setContentsMargins(10, 8, 10, 8)

        self._info_img_thumb = QLabel()
        self._info_img_thumb.setFixedSize(96, 72)
        self._info_img_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_img_thumb.setStyleSheet(
            "border: 1px dashed #556; border-radius: 4px; background: #1a2520; color: #607860; font-size: 22px;")
        self._update_info_img_preview()
        pl.addWidget(self._info_img_thumb)

        hint_lay = QVBoxLayout(); hint_lay.setSpacing(4)
        hint_lbl = QLabel("Ctrl+V  to paste from clipboard")
        hint_lbl.setStyleSheet("color: #80a898; font-size: 10px;")
        paste_btn = QPushButton("📋  Paste Image")
        paste_btn.setFixedHeight(28)
        paste_btn.clicked.connect(self._paste_image_info_tab)
        hint_lay.addWidget(hint_lbl)
        hint_lay.addWidget(paste_btn)
        hint_lay.addStretch()
        pl.addLayout(hint_lay)
        pl.addStretch()
        out.addWidget(pg)

        out.addStretch()
        return w

    def _update_info_img_preview(self) -> None:
        """Refresh the small photo thumbnail on the Info tab."""
        if not hasattr(self, '_info_img_thumb'):
            return
        if self._images:
            b64 = self._images[0]
            pix = self._icon_cache.get(b64)
            pix = pix.pixmap(96, 72) if pix is not None else _b64_to_pixmap(b64)
            if pix and not pix.isNull():
                scaled = pix.scaled(
                    96, 72,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self._info_img_thumb.setPixmap(scaled)
                self._info_img_thumb.setText("")
                return
        self._info_img_thumb.setPixmap(QPixmap())
        self._info_img_thumb.setText("🌿")

    def _paste_image_info_tab(self) -> None:
        """Paste button handler on the Info tab — adds image without switching tabs."""
        img = QApplication.clipboard().image()
        if img.isNull():
            flash_status(self, "No image on the clipboard.")
            return
        self._paste_image()             # adds to self._images, rebuilds Images list
        self._update_info_img_preview() # show result on Info tab

    def _tab_images(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setSpacing(10); lay.setContentsMargins(16, 16, 16, 16)
        note = QLabel("First image = main card image.  Drag files, paste, or browse to add.")
        note.setWordWrap(True); lay.addWidget(note)

        self._img_preview = QLabel()
        self._img_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_preview.setFixedHeight(180)
        self._img_preview.setStyleSheet("border:2px dashed #888; border-radius:8px;")
        self._img_preview.setAcceptDrops(True)
        self._drop_filter = _ImageDropFilter(
            self._img_drag_enter, self._img_drop, self._img_preview)
        self._img_preview.installEventFilter(self._drop_filter)
        lay.addWidget(self._img_preview)

        self._img_list = QListWidget()
        self._img_list.setViewMode(QListWidget.ViewMode.IconMode)
        self._img_list.setIconSize(QSize(95, 72))
        self._img_list.setFixedHeight(100)
        self._img_list.setSpacing(4)
        self._img_list.setMovement(QListWidget.Movement.Snap)
        self._img_list.currentRowChanged.connect(self._on_img_selected)
        lay.addWidget(self._img_list)

        btn_row = QHBoxLayout()
        for label, slot in [("📂  Browse…", self._browse_image),
                             ("📋  Paste", self._paste_image),
                             ("⭐  Set Main", self._set_as_main),
                             ("✕  Remove", self._remove_image)]:
            b = QPushButton(label); b.clicked.connect(slot); btn_row.addWidget(b)
        lay.addLayout(btn_row); lay.addStretch()
        self._rebuild_img_list()
        return w

    def _rebuild_img_list(self) -> None:
        self._img_list.clear()
        for b64 in self._images:
            icon = self._icon_cache.get(b64)
            if icon is None:
                pix = _b64_to_pixmap(b64)
                if pix:
                    icon = QIcon(pix.scaled(95, 72,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                    self._icon_cache[b64] = icon
            item = QListWidgetItem()
            if icon: item.setIcon(icon)
            else: item.setText("?")
            self._img_list.addItem(item)
        if self._images:
            self._img_list.setCurrentRow(0); self._show_preview(0)
        else:
            self._img_preview.setPixmap(QPixmap())
            self._img_preview.setText("No images — drag & drop, browse, or paste")

    def _show_preview(self, idx: int) -> None:
        if 0 <= idx < len(self._images):
            b64 = self._images[idx]
            pix = self._preview_cache.get(b64)
            if pix is None:
                decoded = _b64_to_pixmap(b64)
                if decoded:
                    pix = decoded.scaled(QSize(540, 175),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                    self._preview_cache[b64] = pix
            if pix:
                self._img_preview.setPixmap(pix); return
        self._img_preview.setPixmap(QPixmap()); self._img_preview.setText("No image")

    def _on_img_selected(self, row: int) -> None: self._show_preview(row)

    def _add_b64(self, b64: str) -> None:
        self._images.append(b64); self._rebuild_img_list()
        self._img_list.setCurrentRow(len(self._images)-1)

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if path:
            b64 = _file_to_b64(path)
            if b64: self._add_b64(b64)

    def _paste_image(self) -> None:
        img = QApplication.clipboard().image()
        if not img.isNull():
            b64 = _qimage_to_b64(img)
            if b64: self._add_b64(b64); return
        flash_status(self, "No image on the clipboard.")

    def _set_as_main(self) -> None:
        row = self._img_list.currentRow()
        if row > 0 and row < len(self._images):
            self._images.insert(0, self._images.pop(row)); self._rebuild_img_list()

    def _remove_image(self) -> None:
        row = self._img_list.currentRow()
        if 0 <= row < len(self._images):
            self._images.pop(row); self._rebuild_img_list()

    def _img_drag_enter(self, event) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
        else: event.ignore()

    def _img_drop(self, event) -> None:
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if path.lower().endswith((".png",".jpg",".jpeg",".webp",".bmp",".gif")):
                    b64 = _file_to_b64(path)
                    if b64: self._add_b64(b64)
                    event.acceptProposedAction(); return
        if md.hasImage():
            raw = md.imageData()
            if isinstance(raw, QImage) and not raw.isNull():
                b64 = _qimage_to_b64(raw)
                if b64: self._add_b64(b64); event.acceptProposedAction()

    def _tab_notes(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(QLabel("Extended notes (observations, conditions, results):"))
        self._notes_edit = QTextEdit(self.node.notes); lay.addWidget(self._notes_edit)
        return w

    def _pick_color(self) -> None:
        col = QColorDialog.getColor(QColor(self._current_color), self, "Node colour")
        if col.isValid(): self._current_color = col.name(); self._refresh_color_btn()

    def _refresh_color_btn(self) -> None:
        self._color_btn.setStyleSheet(
            f"background:{self._current_color}; border-radius:4px; border:none;")
        self._color_btn.setText("")

    def _add_tag(self) -> None:
        text = self._tag_input.text().strip()
        if text and not self._tag_list.findItems(text, Qt.MatchFlag.MatchExactly):
            self._tag_list.addItem(text); self._tag_input.clear()

    def _remove_tag(self) -> None:
        for item in self._tag_list.selectedItems():
            self._tag_list.takeItem(self._tag_list.row(item))

    def get_role(self) -> str:
        idx = self._role_combo.currentIndex()
        return {0: "", 1: "mother", 2: "father"}.get(idx, "")

    def _commit(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._tabs.setCurrentIndex(0)
            mark_field_invalid(self._name_edit, "Name cannot be empty.",
                                self._name_error_lbl)
            return
        year_text = self._year_edit.text().strip()
        year: Optional[int] = None
        if year_text:
            try:
                year = int(year_text)
                if not (1000 <= year <= 2200): raise ValueError
            except ValueError:
                self._tabs.setCurrentIndex(0)
                mark_field_invalid(self._year_edit, f"'{year_text}' is not a valid year.",
                                    self._year_error_lbl)
                return
        self.node.name        = name
        self.node.species     = self._species_edit.text().strip()
        self.node.cultivar    = self._cultivar_edit.text().strip()
        self.node.description = self._desc_edit.toPlainText().strip()
        self.node.notes       = self._notes_edit.toPlainText().strip()
        self.node.color       = self._current_color
        self.node.year        = year
        self.node.images      = list(self._images)
        self.node.photo_count = len(self._images)
        new_main = self._images[0] if self._images else None
        if new_main != self._original_main_b64 or (new_main and not self.node.thumb):
            import image_utils
            self.node.thumb = (image_utils.make_thumb_from_b64(new_main)
                                if new_main else None)
        elif not self._images:
            self.node.thumb = None
        self.node.tags        = [self._tag_list.item(i).text()
                                 for i in range(self._tag_list.count())]
        # Save role back into child_node if provided
        if self.child_node is not None:
            self.child_node.parent_roles[self.node.id] = self.get_role()
        self.accept()


# ── New lineage dialog ────────────────────────────────────────────────────────

class NewLineageDialog(FadeDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Lineage"); self.setFixedWidth(460)
        lay = QVBoxLayout(self); lay.setSpacing(10); lay.setContentsMargins(20,20,20,20)
        lay.addWidget(QLabel("Lineage name *"))
        self._name = QLineEdit(); self._name.setPlaceholderText("e.g. Baccatum Cross 2024")
        lay.addWidget(self._name)
        self._name_error_lbl = QLabel(""); self._name_error_lbl.setVisible(False)
        lay.addWidget(self._name_error_lbl)
        self._name.textChanged.connect(
            lambda: clear_field_invalid(self._name, self._name_error_lbl))
        lay.addWidget(QLabel("Description"))
        self._desc = QTextEdit(); self._desc.setFixedHeight(80); lay.addWidget(self._desc)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._commit); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _commit(self) -> None:
        if not self._name.text().strip():
            mark_field_invalid(self._name, "Lineage name is required.", self._name_error_lbl)
            return
        self.accept()

    @property
    def lineage_name(self) -> str: return self._name.text().strip()
    @property
    def lineage_desc(self) -> str: return self._desc.toPlainText().strip()


# ── Connect dialog ────────────────────────────────────────────────────────────

class ConnectDialog(FadeDialog):
    def __init__(self, tree: LineageTree, current_node_id: str, parent=None):
        super().__init__(parent)
        self.tree = tree; self.current_node_id = current_node_id
        self.selected_id: Optional[str] = None
        self.direction = "child"
        self.setWindowTitle("Connect Nodes"); self.setMinimumWidth(420)
        lay = QVBoxLayout(self); lay.setSpacing(8); lay.setContentsMargins(16,16,16,16)
        lay.addWidget(QLabel("Select a node to connect:"))
        self._list = QListWidget()
        for nid, node in tree.nodes.items():
            if nid == current_node_id: continue
            item = QListWidgetItem(f"{node.name}  ({node.species or '—'})")
            item.setData(Qt.ItemDataRole.UserRole, nid)
            self._list.addItem(item)
        lay.addWidget(self._list)
        self._list_error_lbl = QLabel(""); self._list_error_lbl.setVisible(False)
        lay.addWidget(self._list_error_lbl)
        self._list.itemSelectionChanged.connect(
            lambda: clear_field_invalid(self._list, self._list_error_lbl))
        lay.addWidget(QLabel("Make selected node a:"))
        dr = QHBoxLayout()
        self._btn_parent = QPushButton("⬆️  Parent of current")
        self._btn_child  = QPushButton("⬇️  Offspring of current")
        self._btn_parent.setCheckable(True); self._btn_child.setCheckable(True)
        self._btn_child.setChecked(True)
        self._btn_parent.clicked.connect(lambda: self._set_dir("parent"))
        self._btn_child.clicked.connect(lambda: self._set_dir("child"))
        dr.addWidget(self._btn_parent); dr.addWidget(self._btn_child); lay.addLayout(dr)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._commit); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _set_dir(self, d: str) -> None:
        self.direction = d
        self._btn_parent.setChecked(d == "parent")
        self._btn_child.setChecked(d == "child")

    def _commit(self) -> None:
        items = self._list.selectedItems()
        if not items:
            mark_field_invalid(self._list, "Select a node from the list above.",
                                self._list_error_lbl)
            return
        self.selected_id = items[0].data(Qt.ItemDataRole.UserRole); self.accept()


# ── Role dialog ───────────────────────────────────────────────────────────────

class RoleDialog(FadeDialog):
    def __init__(self, parent_name: str, child_name: str, parent=None):
        super().__init__(parent)
        self.role: str = ""
        self.setWindowTitle("Assign Parent Role"); self.setFixedWidth(360)
        lay = QVBoxLayout(self); lay.setSpacing(12); lay.setContentsMargins(20,20,20,20)
        lbl = QLabel(f"Is  <b>{parent_name}</b>  the Mother or Father of  <b>{child_name}</b>?")
        lbl.setWordWrap(True); lay.addWidget(lbl)
        row = QHBoxLayout()
        for label, role in [("♀  Mother","mother"),("♂  Father","father"),("Unknown","")]:
            b = QPushButton(label); b.setFixedHeight(42)
            b.clicked.connect(lambda _, r=role: self._pick(r))
            row.addWidget(b)
        lay.addLayout(row)

    def _pick(self, role: str) -> None:
        self.role = role; self.accept()


# ── Plant database dialog ─────────────────────────────────────────────────────

class PlantDBDialog(FadeDialog):
    import_requested = pyqtSignal(object)   # PlantNode

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🌿  Plant Database")
        # 1.75× larger than before
        self.setMinimumWidth(910)
        self.setMinimumHeight(840)
        self._all_plants: list = []
        self._filtered:   list = []
        self._sort_by = "name"
        self._last_imported_id: Optional[str] = None
        self._build()
        self._refresh()

    def _build(self) -> None:
        lay = QVBoxLayout(self); lay.setSpacing(10); lay.setContentsMargins(16,16,16,16)
        hdr = QLabel("Global Plant Database"); hdr.setObjectName("panel_header")
        lay.addWidget(hdr)

        # Search + sort row
        top_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Search by name, species, cultivar, tag…")
        self._search.textChanged.connect(self._filter)
        top_row.addWidget(self._search, 1)

        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Sort: Alphabetical", "Sort: Species", "Sort: Year"])
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        top_row.addWidget(self._sort_combo)
        lay.addLayout(top_row)

        self._list = QListWidget()
        self._list.setSelectionMode(self._list.SelectionMode.ExtendedSelection)
        self._list.currentRowChanged.connect(self._on_select)
        self._list.itemSelectionChanged.connect(self._on_multi_select_changed)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        lay.addWidget(self._list, 1)

        # Preview pane
        pv = QGroupBox("Preview"); pvl = QHBoxLayout(pv)
        self._pv_img  = QLabel(); self._pv_img.setFixedSize(160, 120)
        self._pv_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pv_img.setStyleSheet("border:1px solid #888; border-radius:4px;")
        pvl.addWidget(self._pv_img)
        self._pv_text = QLabel(); self._pv_text.setWordWrap(True)
        self._pv_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        pvl.addWidget(self._pv_text, 1); lay.addWidget(pv)

        btn_row = QHBoxLayout()
        self._btn_import = QPushButton("⬇️  Import into Lineage")
        self._btn_import.setEnabled(False); self._btn_import.clicked.connect(self._import)
        btn_add_new = QPushButton("➕  Add New Plant")
        btn_add_new.clicked.connect(self._add_new_plant)
        self._btn_delete = QPushButton("🗑️  Delete Plant")
        self._btn_delete.setEnabled(False)
        self._btn_delete.clicked.connect(self._delete_plant)
        btn_close = QPushButton("Close"); btn_close.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_import)
        btn_row.addWidget(btn_add_new)
        btn_row.addWidget(self._btn_delete)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

    def _refresh(self) -> None:
        import plant_db
        self._all_plants = plant_db.get_all()
        self._apply_sort()
        self._filter(self._search.text())

    def _apply_sort(self) -> None:
        if self._sort_by == "species":
            self._all_plants.sort(key=lambda p: (p.species.lower(), p.name.lower()))
        elif self._sort_by == "year":
            self._all_plants.sort(key=lambda p: (p.year or 0, p.name.lower()), reverse=True)
        else:
            self._all_plants.sort(key=lambda p: p.name.lower())

    def _on_sort_changed(self, idx: int) -> None:
        self._sort_by = {0: "name", 1: "species", 2: "year"}.get(idx, "name")
        self._apply_sort()
        self._filter(self._search.text())

    def _filter(self, text: str) -> None:
        q = text.strip()
        if q:
            # Filter the already-deserialized in-memory list — avoids re-creating
            # PlantNode objects from JSON on every keystroke (plant_db.search() does that).
            ql = q.lower()
            self._filtered = [
                p for p in self._all_plants
                if ql in " ".join([p.name, p.species, p.cultivar] + p.tags).lower()
            ]
            if self._sort_by == "species":
                self._filtered.sort(key=lambda p: (p.species.lower(), p.name.lower()))
            elif self._sort_by == "year":
                self._filtered.sort(key=lambda p: (p.year or 0, p.name.lower()), reverse=True)
            else:
                self._filtered.sort(key=lambda p: p.name.lower())
        else:
            self._filtered = list(self._all_plants)

        self._list.clear()
        for p in self._filtered:
            parts = [p.name]
            if p.species:   parts.append(p.species)
            if p.cultivar:  parts.append(p.cultivar)
            if p.year:      parts.append(str(p.year))
            item = QListWidgetItem("  ·  ".join(parts))
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            # Highlight the last imported plant
            if p.id == self._last_imported_id:
                item.setBackground(QColor("#2a5a3a"))
                item.setForeground(QColor("#a0f0b0"))
            self._list.addItem(item)
        self._pv_img.clear(); self._pv_text.clear()
        self._btn_import.setEnabled(False)
        self._btn_delete.setEnabled(False)

    def _on_select(self, row: int) -> None:
        if row < 0 or row >= len(self._filtered): return
        p = self._filtered[row]
        b64 = p.thumb or (p.images[0] if p.images else None)
        if b64:
            pix = _b64_to_pixmap(b64)
            if pix:
                self._pv_img.setPixmap(
                    pix.scaled(160, 120, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation))
            else: self._pv_img.setText("🌿")
        else: self._pv_img.setText("🌿")
        lines = [f"<b>{p.name}</b>"]
        if p.species:     lines.append(f"<i>{p.species}</i>")
        if p.cultivar:    lines.append(p.cultivar)
        if p.year:        lines.append(f"📅 {p.year}")
        if p.tags:        lines.append("  ".join(f"[{t}]" for t in p.tags))
        if p.description: lines.append(p.description[:180])
        self._pv_text.setText("<br>".join(lines))
        self._btn_import.setEnabled(True)
        self._btn_delete.setEnabled(True)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """Double-clicking a plant is equivalent to selecting it and hitting
        'Import into Lineage' — a single click already selects the row via
        the list widget's normal behaviour, so this just triggers the
        import for the row the double-click landed on."""
        row = self._list.row(item)
        if row < 0:
            return
        self._list.setCurrentRow(row)   # in case double-click landed on a non-current row
        self._import()

    def _import(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._filtered): return
        import plant_db
        # Emit the original DB node (original UUID, images populated).
        # Do NOT clone — cloning generates a new UUID which causes a duplicate
        # DB entry when _open_edit_dialog later calls plant_db.upsert() on the
        # lineage node.  The lineage stores its own copy via the .plf file;
        # the DB tracks the canonical metadata by the original UUID.
        original = self._filtered[row]
        plant_db.populate_images(original)   # ensure images are present
        self._last_imported_id = original.id
        self.import_requested.emit(original)
        self._filter(self._search.text())
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it and it.data(Qt.ItemDataRole.UserRole) == self._last_imported_id:
                self._list.scrollToItem(it, QAbstractItemView.ScrollHint.PositionAtCenter)
                break

    def _on_multi_select_changed(self) -> None:
        n = len(self._list.selectedItems())
        if n > 1:
            self._btn_delete.setText(f"🗑  Delete {n} Plants")
            self._btn_import.setEnabled(False)
        elif n == 1:
            self._btn_delete.setText("🗑  Delete Plant")
            self._on_select(self._list.currentRow())
        else:
            self._btn_delete.setText("🗑  Delete Plant")

    def _delete_plant(self) -> None:
        """Delete selected plant(s) from the global database."""
        import plant_db
        selected = self._list.selectedItems()
        if not selected:
            row = self._list.currentRow()
            if row < 0 or row >= len(self._filtered):
                return
            selected = [self._list.item(row)] if row >= 0 else []
        plants = []
        for itm in selected:
            pid = itm.data(Qt.ItemDataRole.UserRole)
            p = next((x for x in self._filtered if x.id == pid), None)
            if p: plants.append(p)
        if not plants: return
        if len(plants) == 1:
            msg = (f"Permanently delete '{plants[0].name}' from the plant database?\n"
                   "This will NOT remove it from any existing lineages.")
            title = "Delete Plant"
        else:
            names = ", ".join(p.name for p in plants[:3])
            if len(plants) > 3: names += f" and {len(plants)-3} more"
            msg = (f"Permanently delete {len(plants)} plants ({names})?\n"
                   "This will NOT remove them from any existing lineages.")
            title = "Delete Plants"
        reply = QMessageBox.question(self, title, msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Yes:
            for p in plants: plant_db.delete(p.id)
            self._refresh()
            self._filter(self._search.text())

    def _add_new_plant(self) -> None:
        """Add a new plant directly to the database without adding to any lineage."""
        from models import PlantNode
        import plant_db
        node = PlantNode()
        dlg  = PlantDialog(node, self, is_new=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            plant_db.upsert(node)
            self._last_imported_id = node.id
            self._refresh()
            self._filter(self._search.text())


# ── About dialog ──────────────────────────────────────────────────────────────

_GITHUB_REPO   = "PatrickJamesDev/BreedingCharts"
_RELEASES_URL  = f"https://github.com/{_GITHUB_REPO}/releases"
_API_LATEST    = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"

# Asset name fragments used to pick the right download per platform.
# We look for the first release asset whose name contains the fragment.
import platform as _platform
_OS = _platform.system()  # "Windows", "Darwin", "Linux"
_ASSET_HINTS = {
    "Windows": (".exe", "-windows", "_windows", "win"),
    "Darwin":  (".dmg", "-mac", "_mac", "-macos", "_macos"),
    "Linux":   (".AppImage", "-linux", "_linux"),
}


class AboutDialog(FadeDialog):
    # PyQt signal so the background thread can safely update UI labels
    _update_result = pyqtSignal(str, str, str)  # (status_text, tag, download_url)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QApplication as _QApp
        self._current_version = _QApp.instance().applicationVersion() if _QApp.instance() else "?"

        self.setWindowTitle("About BreedingCharts")
        self.setMinimumWidth(520)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(self); lay.setContentsMargins(28, 24, 28, 20); lay.setSpacing(12)

        # Title + version on the same row
        header_row = QHBoxLayout()
        title = QLabel("🌿  BreedingCharts")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#6bcc80;")
        ver_lbl = QLabel(f"v{self._current_version}")
        ver_lbl.setStyleSheet("font-size:12px; color:#888; padding-top:6px;")
        ver_lbl.setAlignment(Qt.AlignmentFlag.AlignBottom)
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(ver_lbl)
        lay.addLayout(header_row)

        body = QLabel(
            "A crossbreeding lineage visualiser for plant breeders.\n\n"
            "Features:\n"
            "  • Multi-image cards (lossless PNG, per-node colour edges)\n"
            "  • Mother/Father roles, autocomplete for name/species/cultivar\n"
            "  • Year-band canvas with always-visible year labels\n"
            "  • 4 themes · 8 font families · UI/image scale controls\n"
            "  • Global plant database (add once, reuse across lineages)\n"
            "  • Undo/Redo (action-log based, persistent)\n"
            "  • Copy/Paste node groups, right-click canvas menu\n"
            "  • Export matches on-screen view exactly (lossless PNG)\n"
            "  • Autosave, custom keybinds\n\n"
            "File format: .plf  (JSON — human-readable)"
        )
        body.setWordWrap(True)
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(body)

        community = QLabel("Pepper Lovers Community ❤️")
        community.setStyleSheet("color: #e87060; font-size: 13px; font-weight: bold;")
        community.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(community)
        lay.addStretch()

        # ── Update row ────────────────────────────────────────────────────────
        update_row = QHBoxLayout()
        self._upd_lbl = QLabel("Click to check for the latest release on GitHub.")
        self._upd_lbl.setStyleSheet("color: #90b898; font-size: 11px;")
        self._upd_lbl.setWordWrap(True)
        self._upd_btn = QPushButton("🔄  Check for Updates")
        self._upd_btn.clicked.connect(self._check_updates)
        update_row.addWidget(self._upd_lbl, 1)
        update_row.addWidget(self._upd_btn)
        lay.addLayout(update_row)

        # Download button — hidden until an update is found
        self._dl_btn = QPushButton("⬇️  Download Update")
        self._dl_btn.setVisible(False)
        self._dl_btn.clicked.connect(self._do_download)
        self._dl_url: str = ""
        dl_row = QHBoxLayout()
        dl_row.addStretch()
        dl_row.addWidget(self._dl_btn)
        lay.addLayout(dl_row)

        ok = QPushButton("Close"); ok.clicked.connect(self.accept)
        lay.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)

        # Wire the cross-thread signal
        self._update_result.connect(self._on_update_result)

    # ── background worker ────────────────────────────────────────────────────
    def _check_updates(self) -> None:
        import urllib.request, json as _j, threading
        self._upd_btn.setEnabled(False)
        self._dl_btn.setVisible(False)
        self._upd_lbl.setText("Checking GitHub releases…")

        def _work():
            try:
                req = urllib.request.Request(
                    _API_LATEST,
                    headers={"Accept": "application/vnd.github+json",
                             "User-Agent": f"BreedingCharts/{self._current_version}"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        data = _j.loads(r.read())
                except urllib.error.HTTPError as http_err:
                    if http_err.code == 404:
                        # No releases published yet on this repo
                        self._update_result.emit(
                            "No releases found yet on GitHub. Check back after the first release is published.",
                            "", _RELEASES_URL)
                    else:
                        self._update_result.emit(
                            f"GitHub returned HTTP {http_err.code}. Try again later.", "", "")
                    return

                tag      = data.get("tag_name", "").lstrip("v")
                assets   = data.get("assets", [])
                body_txt = data.get("body", "")

                # Try to find the right asset for this OS
                dl_url = ""
                hints  = _ASSET_HINTS.get(_OS, [])
                for asset in assets:
                    aname = asset.get("name", "").lower()
                    if any(h.lower() in aname for h in hints):
                        dl_url = asset.get("browser_download_url", "")
                        break
                # Fallback: releases page
                if not dl_url:
                    dl_url = _RELEASES_URL

                # Compare versions (simple string split comparison)
                def _ver_tuple(v):
                    try: return tuple(int(x) for x in v.strip().split("."))
                    except: return (0,)

                cur_t   = _ver_tuple(self._current_version)
                new_t   = _ver_tuple(tag)

                if new_t > cur_t:
                    status = f"Update available: v{tag}  (you have v{self._current_version})"
                elif new_t == cur_t:
                    status = f"You are up to date  (v{self._current_version})"
                    dl_url = ""   # no need to show download button
                else:
                    status = f"You are running a newer build than the latest release  (v{tag})"
                    dl_url = ""

                self._update_result.emit(status, tag, dl_url)

            except Exception as e:
                self._update_result.emit(f"Could not reach GitHub: {e}", "", "")

        threading.Thread(target=_work, daemon=True).start()

    def _on_update_result(self, status: str, tag: str, dl_url: str) -> None:
        self._upd_lbl.setText(status)
        self._upd_btn.setEnabled(True)
        self._dl_url = dl_url
        if dl_url:
            os_name = {"Windows": "Windows", "Darwin": "macOS"}.get(_OS, "Linux")
            self._dl_btn.setText(f"⬇️  Download for {os_name}")
            self._dl_btn.setVisible(True)

    def _do_download(self) -> None:
        """Open direct asset URL (or releases page) in the system browser."""
        import webbrowser
        if self._dl_url:
            webbrowser.open(self._dl_url)


# ═══════════════════════════════════════════════════════════════════════════════
# v1.2.0 — BATCH EDIT DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class BatchEditDialog(FadeDialog):
    """Apply tag / color / species changes to multiple selected plants at once.

    Usage:
        dlg = BatchEditDialog(nodes, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.apply_to(nodes)
            # then save the tree
    """

    def __init__(self, nodes: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Batch Edit — {len(nodes)} plants selected")
        self.setMinimumWidth(400)
        self._nodes = nodes
        self._color: Optional[str] = None
        self._build()

    def _build(self) -> None:
        from PyQt6.QtWidgets import (
            QVBoxLayout, QFormLayout, QCheckBox, QLineEdit, QPushButton,
            QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel,
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        info = QLabel(
            f"Changes will be applied to <b>{len(self._nodes)}</b> plant(s).<br>"
            "Only checked fields are modified."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        grp = QGroupBox("Fields to change")
        form = QFormLayout(grp)
        form.setSpacing(8)

        # Species
        self._chk_species = QCheckBox("Change species")
        self._inp_species  = QLineEdit()
        self._inp_species.setPlaceholderText("e.g. Capsicum chinense")
        self._inp_species.setEnabled(False)
        self._chk_species.toggled.connect(self._inp_species.setEnabled)
        form.addRow(self._chk_species, self._inp_species)

        # Add tags
        self._chk_add_tags = QCheckBox("Add tags (comma-separated)")
        self._inp_add_tags  = QLineEdit()
        self._inp_add_tags.setPlaceholderText("e.g. F3, stable, hot")
        self._inp_add_tags.setEnabled(False)
        self._chk_add_tags.toggled.connect(self._inp_add_tags.setEnabled)
        form.addRow(self._chk_add_tags, self._inp_add_tags)

        # Remove tags
        self._chk_rem_tags = QCheckBox("Remove tags (comma-separated)")
        self._inp_rem_tags  = QLineEdit()
        self._inp_rem_tags.setPlaceholderText("e.g. experimental")
        self._inp_rem_tags.setEnabled(False)
        self._chk_rem_tags.toggled.connect(self._inp_rem_tags.setEnabled)
        form.addRow(self._chk_rem_tags, self._inp_rem_tags)

        # Color
        self._chk_color = QCheckBox("Change card color")
        color_row = QHBoxLayout()
        self._color_preview = QPushButton("  ●  Pick color…")
        self._color_preview.setStyleSheet("background: #52a86a; color: #fff; border-radius:4px;")
        self._color_preview.setEnabled(False)
        self._color_preview.clicked.connect(self._pick_color)
        color_row.addWidget(self._color_preview)
        color_row.addStretch()
        self._chk_color.toggled.connect(self._color_preview.setEnabled)
        form.addRow(self._chk_color, color_row)

        # Year
        self._chk_year = QCheckBox("Set year")
        self._inp_year  = QLineEdit()
        self._inp_year.setPlaceholderText("e.g. 2024")
        self._inp_year.setEnabled(False)
        self._chk_year.toggled.connect(self._inp_year.setEnabled)
        form.addRow(self._chk_year, self._inp_year)

        lay.addWidget(grp)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _pick_color(self) -> None:
        from PyQt6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor(self._color or "#52a86a"), self, "Pick Card Color")
        if c.isValid():
            self._color = c.name()
            self._color_preview.setStyleSheet(
                f"background: {self._color}; color: #fff; border-radius:4px;")

    def apply_to(self, nodes: list) -> int:
        """Apply changes to nodes in-place. Returns number modified."""
        count = 0
        for node in nodes:
            changed = False
            if self._chk_species.isChecked() and self._inp_species.text().strip():
                node.species = self._inp_species.text().strip(); changed = True
            if self._chk_add_tags.isChecked():
                new_tags = [t.strip() for t in self._inp_add_tags.text().split(",") if t.strip()]
                for t in new_tags:
                    if t not in node.tags: node.tags.append(t)
                changed = True
            if self._chk_rem_tags.isChecked():
                rem = {t.strip() for t in self._inp_rem_tags.text().split(",") if t.strip()}
                node.tags = [t for t in node.tags if t not in rem]; changed = True
            if self._chk_color.isChecked() and self._color:
                node.color = self._color; changed = True
            if self._chk_year.isChecked():
                try:    node.year = int(self._inp_year.text().strip()); changed = True
                except: pass
            if changed: count += 1
        return count


# ═══════════════════════════════════════════════════════════════════════════════
# v1.2.0 — DUPLICATE PLANT DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class DuplicateDetectorDialog(FadeDialog):
    """Scan all .plf files in the app directory and report plants that share
    the same (name, species, year) tuple — likely duplicates."""

    def __init__(self, parent=None, app_dir: str = "."):
        super().__init__(parent)
        self.setWindowTitle("Duplicate Plant Detector")
        self.setMinimumSize(620, 480)
        self._app_dir = app_dir
        self._build()
        QTimer.singleShot(50, self._scan)

    def _build(self) -> None:
        from PyQt6.QtWidgets import (
            QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
            QPushButton, QProgressBar, QHeaderView,
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self._status = QLabel("Scanning lineages…")
        lay.addWidget(self._status)
        self._progress = QProgressBar(); self._progress.setRange(0, 0)
        lay.addWidget(self._progress)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Plant Name", "Species", "Year", "Found In"])
        self._table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        lay.addWidget(self._table)
        close_btn = QPushButton("Close"); close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn)

    def _scan(self) -> None:
        from PyQt6.QtWidgets import QTableWidgetItem
        from database import get_lineage_files, load_lineage
        import os, collections
        files = get_lineage_files(self._app_dir)
        self._progress.setRange(0, len(files))
        key_map = collections.defaultdict(list)  # (name, species, year) → [(filename, node)]
        for i, path in enumerate(files):
            self._progress.setValue(i)
            self._status.setText(f"Scanning {os.path.basename(path)}…")
            QApplication.processEvents()
            tree = load_lineage(path)
            if tree is None: continue
            fname = os.path.basename(path)
            for node in tree.nodes.values():
                k = (node.name.strip().lower(), node.species.strip().lower(),
                     str(node.year or ""))
                key_map[k].append((fname, node.name, node.species, node.year))

        dupes = {k: v for k, v in key_map.items() if len(v) > 1}
        self._progress.setRange(0, 1); self._progress.setValue(1)
        self._table.setRowCount(0)
        for key, entries in sorted(dupes.items()):
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(entries[0][1]))
            self._table.setItem(row, 1, QTableWidgetItem(entries[0][2] or "—"))
            self._table.setItem(row, 2, QTableWidgetItem(str(entries[0][3] or "—")))
            files_str = ", ".join(sorted({e[0] for e in entries}))
            self._table.setItem(row, 3, QTableWidgetItem(files_str))
        n = len(dupes)
        self._status.setText(
            f"Found <b>{n}</b> duplicate group{'s' if n != 1 else ''}."
            if n else "No duplicates found across all lineages ✓")
        self._status.setTextFormat(Qt.TextFormat.RichText)


# ═══════════════════════════════════════════════════════════════════════════════
# v1.2.0 — CROSS-LINEAGE PARENT LINK DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class CrossLineageConnectDialog(FadeDialog):
    """Browse plants across ALL lineages and pick one as a parent for a node
    in the current lineage.  Returns (source_file, plant_name, plant_id, role).
    """

    def __init__(self, parent=None, app_dir: str = "."):
        super().__init__(parent)
        self.setWindowTitle("Add Cross-Lineage Parent")
        self.setMinimumSize(560, 480)
        self._app_dir = app_dir
        self._selected: Optional[dict] = None
        self._build()
        QTimer.singleShot(50, self._load)

    @property
    def selected(self) -> Optional[dict]:
        """Returns dict with keys: filepath, plant_id, plant_name, role"""
        return self._selected

    def _build(self) -> None:
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
            QListWidget, QComboBox, QPushButton, QDialogButtonBox, QSplitter,
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Search for a plant across all your lineages:"))

        srch_row = QHBoxLayout()
        self._search = QLineEdit(); self._search.setPlaceholderText("🔍 Plant name or species…")
        self._search.textChanged.connect(self._filter)
        srch_row.addWidget(self._search)
        lay.addLayout(srch_row)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.currentItemChanged.connect(self._on_select)
        lay.addWidget(self._list)

        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("Assign as:"))
        self._role_combo = QComboBox()
        self._role_combo.addItems(["— unset —", "♀ Mother", "♂ Father"])
        role_row.addWidget(self._role_combo)
        role_row.addStretch()
        lay.addLayout(role_row)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._commit); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._all_entries: list = []   # (filepath, plant_id, display_str, name, species)

    def _load(self) -> None:
        from database import get_lineage_files, load_lineage
        import os
        for path in get_lineage_files(self._app_dir):
            tree = load_lineage(path)
            if tree is None: continue
            fname = os.path.basename(path).replace(".plf", "")
            for pid, node in tree.nodes.items():
                disp = f"{node.name}"
                if node.species: disp += f"  ({node.species})"
                disp += f"  — {fname}"
                self._all_entries.append(
                    (path, pid, disp, node.name.lower(), node.species.lower()))
        self._filter("")

    def _filter(self, q: str) -> None:
        from PyQt6.QtWidgets import QListWidgetItem
        self._list.clear()
        q = q.strip().lower()
        for path, pid, disp, name, species in self._all_entries:
            if q and q not in name and q not in species: continue
            item = QListWidgetItem(disp)
            item.setData(Qt.ItemDataRole.UserRole, (path, pid))
            self._list.addItem(item)

    def _on_select(self, item) -> None:
        pass   # preview could go here

    def _commit(self) -> None:
        item = self._list.currentItem()
        if item is None: return
        path, pid = item.data(Qt.ItemDataRole.UserRole)
        role_map = {"♀ Mother": "mother", "♂ Father": "father"}
        role = role_map.get(self._role_combo.currentText(), "")
        disp = item.text().split("—")[0].strip()
        self._selected = {"filepath": path, "plant_id": pid,
                          "plant_name": disp, "role": role}
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# v1.2.0 — PEDIGREE CERTIFICATE (PDF)
# ═══════════════════════════════════════════════════════════════════════════════

class PedigreeCertDialog(FadeDialog):
    """Generate and preview a single-plant pedigree certificate as a PDF.

    Shows: plant photo, full name/species/year, ancestor table (up to 4
    generations), trait tags, and a formatted footer.
    """

    def __init__(self, node, tree, parent=None):
        super().__init__(parent)
        from models import LineageTree, PlantNode
        self._node: PlantNode = node
        self._tree: LineageTree = tree
        self.setWindowTitle(f"Pedigree Certificate — {node.name}")
        self.setMinimumSize(520, 400)
        self._build()

    def _build(self) -> None:
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialogButtonBox,
            QTextEdit, QFileDialog,
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        # Preview label (rich text)
        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMinimumHeight(280)
        self._preview.setHtml(self._build_html())
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout()
        btn_pdf = QPushButton("📄 Export PDF…")
        btn_pdf.clicked.connect(self._export_pdf)
        btn_png = QPushButton("🖼️ Export PNG…")
        btn_png.clicked.connect(self._export_png)
        btn_row.addWidget(btn_pdf); btn_row.addWidget(btn_png); btn_row.addStretch()
        lay.addLayout(btn_row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _build_html(self) -> str:
        """Build rich HTML preview of the certificate."""
        node = self._node; tree = self._tree
        tags_html = " ".join(
            f"<span style='background:#2a4a2e;color:#8cdc8c;"
            f"padding:2px 8px;border-radius:8px;font-size:11px;'>{t}</span>"
            for t in node.tags) if node.tags else "<i>none</i>"

        # Build ancestor table
        def get_name(pid: str) -> str:
            n = tree.nodes.get(pid)
            return n.name if n else "—"

        parents   = node.parent_ids[:2]
        pnames    = [get_name(p) for p in parents]
        gp_names  = []
        for pid in parents:
            pnode = tree.nodes.get(pid)
            gp_names += [(get_name(gp) if pnode else "—") for gp in (pnode.parent_ids[:2] if pnode else [])]

        parent_html = "".join(
            f"<tr><td style='padding:3px 8px'>{i+1}.</td>"
            f"<td style='padding:3px 8px'><b>{pnames[i]}</b></td></tr>"
            for i in range(len(pnames))) or "<tr><td colspan='2'><i>No parents recorded</i></td></tr>"

        gp_html = "".join(
            f"<tr><td style='padding:2px 6px'>{i+1}.</td>"
            f"<td style='padding:2px 6px'>{gp_names[i]}</td></tr>"
            for i in range(len(gp_names))) or "<tr><td colspan='2'><i>—</i></td></tr>"

        return f"""
<div style='font-family:Georgia,serif;color:#d8f0d8;background:#121e14;padding:18px;'>
  <h2 style='color:#4ecb6a;margin:0 0 4px 0;'>{node.name}</h2>
  <div style='color:#90b898;font-style:italic;margin-bottom:12px;'>
    {node.species or '—'}
    {(' · Cultivar: <b>' + node.cultivar + '</b>') if getattr(node,'cultivar','') else ''}
    {(' · Year: <b>' + str(node.year) + '</b>') if node.year else ''}
  </div>
  <hr style='border-color:#2a4a2e;'>
  <h4 style='color:#8cdc8c;margin:10px 0 4px 0;'>🏷️ Trait Tags</h4>
  <div style='margin-bottom:10px;'>{tags_html}</div>
  <hr style='border-color:#2a4a2e;'>
  <h4 style='color:#8cdc8c;margin:10px 0 4px 0;'>👥 Parents</h4>
  <table style='border-collapse:collapse;width:100%;'>{parent_html}</table>
  <h4 style='color:#8cdc8c;margin:10px 0 4px 0;'>👴 Grandparents</h4>
  <table style='border-collapse:collapse;width:100%;'>{gp_html}</table>
  <hr style='border-color:#2a4a2e;'>
  <div style='color:#90b898;font-size:11px;margin-top:8px;'>
    Notes: {node.notes or '—'} <br>
    Generated by BreedingCharts v1.2.0
  </div>
</div>"""

    def _export_pdf(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Pedigree Certificate as PDF",
            f"{self._node.name}_pedigree.pdf", "PDF (*.pdf)")
        if not path: return
        try:
            from PyQt6.QtPrintSupport import QPrinter
            from PyQt6.QtGui import QTextDocument
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(path)
            doc = QTextDocument()
            doc.setHtml(self._build_html())
            doc.print(printer)
            flash_status(self, f"Pedigree certificate saved to {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "PDF Error", str(e))

    def _export_png(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Pedigree Certificate as PNG",
            f"{self._node.name}_pedigree.png", "PNG (*.png)")
        if not path: return
        try:
            img = self._preview.grab().toImage()
            img.save(path, "PNG")
            flash_status(self, f"Pedigree certificate saved to {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "PNG Error", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# v1.20 — EXPORT DATA BUNDLE
# ═══════════════════════════════════════════════════════════════════════════════

class ExportBundleDialog(FadeDialog):
    """Pick which lineages (and whether to include the whole plant
    database) go into a shareable .bcbundle file.

    Usage:
        dlg = ExportBundleDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            paths = dlg.selected_lineage_paths()
            include_db = dlg.include_plant_db()
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Data Bundle")
        self.setMinimumSize(440, 480)
        self._build()

    def _build(self) -> None:
        from database import get_lineage_files, get_lineage_meta, get_data_dir

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        info = QLabel(
            "Bundle lineages and/or your plant database into a single file "
            "to share with another BreedingCharts install. Importing it "
            "elsewhere never overwrites anything already there — matching "
            "lineages/plants are skipped and everything new is added "
            "alongside the existing data.")
        info.setWordWrap(True)
        lay.addWidget(info)

        lay.addWidget(QLabel("<b>Lineages to include:</b>"))

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._paths_by_row: List[str] = []
        for path in get_lineage_files(get_data_dir()):
            meta = get_lineage_meta(path)
            item = QListWidgetItem(
                f"{meta.get('name', os.path.basename(path))}   "
                f"({meta.get('node_count', 0)} plants)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._list.addItem(item)
            self._paths_by_row.append(path)
        lay.addWidget(self._list, 1)

        sel_row = QHBoxLayout()
        btn_all  = QPushButton("Select All")
        btn_none = QPushButton("Select None")
        btn_all.clicked.connect(lambda: self._set_all_checked(True))
        btn_none.clicked.connect(lambda: self._set_all_checked(False))
        sel_row.addWidget(btn_all); sel_row.addWidget(btn_none); sel_row.addStretch()
        lay.addLayout(sel_row)

        self._chk_plant_db = QCheckBox(
            "Include the full plant database (recommended — photos & "
            "metadata for every plant, including ones not in the "
            "lineages selected above)")
        self._chk_plant_db.setChecked(True)
        lay.addWidget(self._chk_plant_db)

        if self._list.count() == 0:
            lay.addWidget(QLabel("<i>No lineages found in the current data folder.</i>"))

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Export…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)

    def selected_lineage_paths(self) -> List[str]:
        return [self._paths_by_row[i] for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.CheckState.Checked]

    def include_plant_db(self) -> bool:
        return self._chk_plant_db.isChecked()


# ── import guard ──────────────────────────────────────────────────────────────
try:
    from PyQt6.QtWidgets import QApplication, QMessageBox, QTimer
except ImportError:
    pass
