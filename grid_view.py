"""
grid_view.py — Full-screen grid view of every plant in the database.

Performance fixes:
  • _card_cache: MiniPlantCard widgets are reused across sort/filter rebuilds.
    Only brand-new plants get a new card; existing ones are simply re-placed in
    the layout. Removes the O(n) card-construction cost on every sort change.
  • Role labels are pre-computed once per rebuild (O(n)) instead of each card
    calling plant_db.get_all() independently (was O(n²)).

New features:
  • Del key deletes the selected/focused card (same behaviour as lineage view).
  • Clicking a card selects it (visual highlight + tracks _selected_plant).
"""
from __future__ import annotations
import base64
import collections
import csv
import io
import logging
import os
from typing import TYPE_CHECKING, Optional, Dict

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea,
    QLabel, QPushButton, QLineEdit, QFrame, QSizePolicy,
    QMessageBox, QApplication, QComboBox, QMenu,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QStackedWidget, QTabWidget, QFileDialog,
)
from PyQt6.QtGui import QPixmap, QPixmapCache, QColor, QPainter, QFont, QPen, QBrush, QCursor
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, QPoint, QRectF

from ui_common import flash_status

if TYPE_CHECKING:
    from models import PlantNode

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-sheet spreadsheet components
# ─────────────────────────────────────────────────────────────────────────────

class SheetTable(QTableWidget):
    """
    A QTableWidget that supports Excel-style Ctrl+V paste (tab-separated rows)
    and auto-expands to fit the pasted data.
    """

    def __init__(self, rows: int = 50, cols: int = 20, parent=None):
        super().__init__(rows, cols, parent)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.SelectedClicked |
            QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(False)
        self.verticalHeader().setVisible(True)
        # Default blank headers (A, B, C …)
        self._reset_headers(cols)

    def _reset_headers(self, n: int) -> None:
        labels = []
        for i in range(n):
            # Generate Excel-style column letters: A, B … Z, AA, AB …
            label = ""
            v = i
            while True:
                label = chr(ord("A") + v % 26) + label
                v = v // 26 - 1
                if v < 0:
                    break
            labels.append(label)
        self.setHorizontalHeaderLabels(labels)

    def set_named_headers(self, headers: list) -> None:
        """Set explicit column header names."""
        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)

    def load_from_rows(self, rows: list[list[str]], has_header: bool = True) -> None:
        """
        Populate the table from a 2-D list of strings.
        If has_header, the first row is used as column headers.
        """
        if not rows:
            return
        self.blockSignals(True)
        self.setSortingEnabled(False)
        if has_header:
            headers = rows[0]
            data    = rows[1:]
        else:
            headers = []
            data    = rows

        n_cols = max((len(r) for r in data), default=0)
        if headers:
            n_cols = max(n_cols, len(headers))
        if n_cols == 0:
            self.blockSignals(False)
            return

        n_rows = len(data)
        self.setRowCount(max(n_rows, 50))
        self.setColumnCount(max(n_cols, 20))

        if headers:
            # Pad headers if needed
            padded = list(headers) + [""] * (n_cols - len(headers))
            self.setHorizontalHeaderLabels(padded)
        else:
            self._reset_headers(max(n_cols, 20))

        for r, row in enumerate(data):
            for c, val in enumerate(row):
                self.setItem(r, c, QTableWidgetItem(val))

        self.resizeColumnsToContents()
        self.setSortingEnabled(True)
        self.blockSignals(False)

    def keyPressEvent(self, event) -> None:
        if (event.key() == Qt.Key.Key_V and
                event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._paste_from_clipboard()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # Clear selected cells
            for item in self.selectedItems():
                item.setText("")
            return
        super().keyPressEvent(event)

    def _paste_from_clipboard(self) -> None:
        """
        Paste tab/newline separated text (Excel or LibreOffice Calc style)
        starting at the top-left of the current selection, auto-expanding the
        table if the data is larger than the current dimensions.
        """
        text = QApplication.clipboard().text()
        if not text:
            return

        # Parse: rows split by newline, cells split by tab
        paste_rows = []
        for line in text.splitlines():
            paste_rows.append(line.split("\t"))

        if not paste_rows:
            return

        # Determine top-left anchor (selected cell or 0,0)
        sel = self.selectedIndexes()
        if sel:
            top    = min(i.row()    for i in sel)
            left   = min(i.column() for i in sel)
        else:
            top, left = 0, 0

        n_rows = len(paste_rows)
        n_cols = max(len(r) for r in paste_rows)

        # Auto-expand table if needed
        need_rows = top + n_rows
        need_cols = left + n_cols
        if need_rows > self.rowCount():
            self.setRowCount(need_rows + 20)   # add a little headroom
        if need_cols > self.columnCount():
            new_total = need_cols + 5
            old_count = self.columnCount()
            self.setColumnCount(new_total)
            # Extend headers
            old_labels = [self.horizontalHeaderItem(i).text()
                          if self.horizontalHeaderItem(i) else ""
                          for i in range(old_count)]
            for i in range(old_count, new_total):
                v = i
                label = ""
                while True:
                    label = chr(ord("A") + v % 26) + label
                    v = v // 26 - 1
                    if v < 0:
                        break
                old_labels.append(label)
            self.setHorizontalHeaderLabels(old_labels)

        self.blockSignals(True)
        for r, row in enumerate(paste_rows):
            for c, val in enumerate(row):
                self.setItem(top + r, left + c, QTableWidgetItem(val))
        self.blockSignals(False)
        self.resizeColumnsToContents()


class SheetTabWidget(QWidget):
    """
    A tabbed container of SheetTable instances.

    Supports:
    - Multiple named tabs
    - "+" tab always appended at right → creates a new blank sheet
    - CSV import (adds a new tab named after the file)
    - Applying a dark style that matches the rest of BreedingCharts
    """

    def __init__(self, palette: dict = None, parent=None):
        super().__init__(parent)
        self._pal   = palette or {}
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        # Toolbar row inside the spreadsheet area
        tool_row = QHBoxLayout()
        tool_row.setSpacing(6)

        btn_import = QPushButton("📂  Import .csv…")
        btn_import.setToolTip("Import a CSV file as a new sheet tab")
        btn_import.clicked.connect(self._import_csv_dialog)
        tool_row.addWidget(btn_import)

        btn_new = QPushButton("➕  New Sheet")
        btn_new.setToolTip("Add a new blank sheet")
        btn_new.clicked.connect(self._add_blank_sheet)
        tool_row.addWidget(btn_new)

        btn_rename = QPushButton("✏️  Rename Sheet")
        btn_rename.clicked.connect(self._rename_current)
        tool_row.addWidget(btn_rename)

        btn_del = QPushButton("🗑️  Remove Sheet")
        btn_del.clicked.connect(self._remove_current)
        tool_row.addWidget(btn_del)

        tool_row.addStretch()

        paste_hint = QLabel("Tip: Copy from Excel → Ctrl+V here to auto-fill")
        paste_hint.setStyleSheet(
            f"color: {self._pal.get('text_muted', '#90b898')}; font-size: 10px;")
        tool_row.addWidget(paste_hint)

        lay.addLayout(tool_row)

        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(False)   # we use our own remove button
        self._tabs.setMovable(True)
        lay.addWidget(self._tabs, 1)

        # Start with one blank sheet
        self._add_blank_sheet("Sheet 1")
        self._apply_style()

    def _apply_style(self) -> None:
        bg      = self._pal.get("panel_bg",    "#141f18")
        bg2     = self._pal.get("panel_bg2",   "#1c2a1c")
        accent  = self._pal.get("accent",      "#4ecb6a")
        txt     = self._pal.get("text_header", "#d0e8d4")
        border  = self._pal.get("node_border", "#2e4a34")
        sel_bg  = QColor(accent).darker(280).name()

        table_css = f"""
            QTableWidget {{
                background-color: {bg};
                alternate-background-color: {bg2};
                color: {txt};
                gridline-color: {border};
                border: 1px solid {border};
                font-family: Consolas, monospace;
                font-size: 11px;
                selection-background-color: {sel_bg};
                selection-color: {accent};
            }}
            QTableWidget::item {{ padding: 3px 6px; border: none; }}
            QTableWidget::item:selected {{ background: {sel_bg}; color: {accent}; }}
            QHeaderView::section {{
                background-color: {bg2};
                color: {accent};
                border: 1px solid {border};
                padding: 4px 8px;
                font-weight: bold;
                font-size: 11px;
            }}
        """
        # Apply to all existing tables
        for i in range(self._tabs.count()):
            w = self._tabs.widget(i)
            if isinstance(w, SheetTable):
                w.setStyleSheet(table_css)

        tab_css = f"""
            QTabWidget::pane {{ border: 1px solid {border}; }}
            QTabBar::tab {{
                background: {bg2};
                color: {txt};
                border: 1px solid {border};
                padding: 4px 14px;
                margin-right: 2px;
                font-size: 11px;
            }}
            QTabBar::tab:selected {{ background: {bg}; color: {accent}; }}
            QTabBar::tab:hover    {{ background: {self._pal.get('btn_hover', '#284038')}; }}
            QPushButton {{
                background: {self._pal.get('btn_bg', '#1e3028')};
                color: {accent};
                border: 1px solid {self._pal.get('btn_border', '#3a5a40')};
                border-radius: 5px;
                padding: 3px 10px;
                font-size: 10px;
            }}
            QPushButton:hover {{ background: {self._pal.get('btn_hover', '#284038')}; }}
        """
        self.setStyleSheet(tab_css)

    def update_palette(self, palette: dict) -> None:
        self._pal = palette
        self._apply_style()

    def _make_table(self) -> SheetTable:
        t = SheetTable(parent=self)
        t.setStyleSheet(self._css_for_table())
        return t

    def _css_for_table(self) -> str:
        bg     = self._pal.get("panel_bg",    "#141f18")
        bg2    = self._pal.get("panel_bg2",   "#1c2a1c")
        accent = self._pal.get("accent",      "#4ecb6a")
        txt    = self._pal.get("text_header", "#d0e8d4")
        border = self._pal.get("node_border", "#2e4a34")
        sel_bg = QColor(accent).darker(280).name()
        return f"""
            QTableWidget {{
                background-color: {bg};
                alternate-background-color: {bg2};
                color: {txt};
                gridline-color: {border};
                border: 1px solid {border};
                font-family: Consolas, monospace;
                font-size: 11px;
                selection-background-color: {sel_bg};
                selection-color: {accent};
            }}
            QTableWidget::item {{ padding: 3px 6px; border: none; }}
            QTableWidget::item:selected {{ background: {sel_bg}; color: {accent}; }}
            QHeaderView::section {{
                background-color: {bg2};
                color: {accent};
                border: 1px solid {border};
                padding: 4px 8px;
                font-weight: bold;
                font-size: 11px;
            }}
        """

    def _add_blank_sheet(self, name: str = "") -> SheetTable:
        """Add a new blank sheet and return it."""
        if not name:
            idx  = self._tabs.count() + 1
            name = f"Sheet {idx}"
        table = self._make_table()
        self._tabs.addTab(table, name)
        self._tabs.setCurrentWidget(table)
        return table

    def import_csv_file(self, path: str) -> None:
        """Load a CSV file into a new tab named after the file."""
        import os
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            # Try UTF-8, fall back to cp1252 (common Excel encoding)
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    with open(path, "r", encoding=enc, newline="") as fh:
                        content = fh.read()
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise ValueError("Could not decode file with any common encoding.")

            # Detect delimiter (tab or comma)
            sample = content[:4096]
            tab_count   = sample.count("\t")
            comma_count = sample.count(",")
            delim = "\t" if tab_count > comma_count else ","

            reader = csv.reader(io.StringIO(content), delimiter=delim)
            rows   = [r for r in reader]
        except Exception as e:
            log.error("import_csv_file(%s): %s", path, e)
            QMessageBox.critical(None, "Import Error",
                                 f"Could not read CSV:\n{path}\n\n{e}")
            return

        table = self._add_blank_sheet(name)
        # Rename the tab if it was auto-named differently
        idx = self._tabs.indexOf(table)
        self._tabs.setTabText(idx, name)
        table.load_from_rows(rows, has_header=bool(rows))

    def _import_csv_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import CSV", "",
            "CSV / TSV Files (*.csv *.tsv *.txt);;All Files (*)")
        for p in paths:
            self.import_csv_file(p)

    def _rename_current(self) -> None:
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        from PyQt6.QtWidgets import QInputDialog
        old  = self._tabs.tabText(idx)
        name, ok = QInputDialog.getText(self, "Rename Sheet", "Sheet name:", text=old)
        if ok and name.strip():
            self._tabs.setTabText(idx, name.strip())

    def _remove_current(self) -> None:
        if self._tabs.count() <= 1:
            flash_status(self, "At least one sheet must remain.")
            return
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        reply = QMessageBox.question(
            self, "Remove Sheet",
            f"Remove sheet '{self._tabs.tabText(idx)}'? Data will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Yes:
            widget = self._tabs.widget(idx)
            self._tabs.removeTab(idx)
            if widget:
                widget.deleteLater()

    def current_table(self) -> Optional[SheetTable]:
        w = self._tabs.currentWidget()
        return w if isinstance(w, SheetTable) else None

    def grab_full_content(self) -> "QPixmap":
        """Capture the ENTIRE current sheet — all rows/columns, not just
        whatever fits in the tab's visible area — as a single QPixmap.

        A QTableWidget's own size doesn't grow to fit its content the way a
        QScrollArea's contained widget does; it stays whatever size its
        parent layout gives it and scrolls internally. And simply calling
        table.resize() while it's still an active QTabWidget page doesn't
        stick, because QTabWidget re-imposes its own page geometry on the
        very next layout pass (which a processEvents() call, needed for the
        resize to actually apply before grabbing, promptly triggers).
        So: briefly detach the table from the tab widget (removeTab doesn't
        delete it, just un-parents it from the managed page stack), resize
        it to its full content size with nothing overriding that, grab it,
        then put it right back in the same tab position.
        """
        from PyQt6.QtGui import QPixmap
        table = self.current_table()
        if table is None:
            return QPixmap()

        idx = self._tabs.indexOf(table)
        tab_text = self._tabs.tabText(idx)
        tab_icon = self._tabs.tabIcon(idx)
        orig_size = table.size()

        full_w = (table.verticalHeader().width()
                  + table.horizontalHeader().length()
                  + table.frameWidth() * 2)
        full_h = (table.horizontalHeader().height()
                  + table.verticalHeader().length()
                  + table.frameWidth() * 2)

        self._tabs.removeTab(idx)
        try:
            table.resize(max(full_w, 1), max(full_h, 1))
            QApplication.processEvents()
            pix = table.grab()
        finally:
            table.resize(orig_size)
            self._tabs.insertTab(idx, table, tab_icon, tab_text)
            self._tabs.setCurrentIndex(idx)
            QApplication.processEvents()
        return pix


# ─────────────────────────────────────────────────────────────────────────────

CARD_W = 180
CARD_H = 240   # baseline/minimum card height — see MiniPlantCard for why this
               # is a floor, not a ceiling, for the text area below the image
IMG_H  = 120
PAD    = 10
COL_W  = CARD_W + PAD * 2

# Any single field (name, species) beyond this length is almost certainly a
# mis-paste (a whole paragraph dropped into a name field) rather than a real
# plant name — cap display at this length so one malformed entry can't blow
# a card up to an absurd height, while every realistic name/species (always
# far shorter than this) is shown in full with no truncation.
_CARD_TEXT_SAFETY_CAP = 240

# Same idea for tag counts: normal plants have a handful of tags and all of
# them are always shown in full; a runaway list still degrades gracefully
# (a "+N more" chip) instead of being silently dropped like before.
_CARD_TAG_DISPLAY_CAP = 12


def _capped_card_text(text: str, cap: int = _CARD_TEXT_SAFETY_CAP) -> tuple[str, bool]:
    """Return (display_text, was_capped). Only clips absurdly long strings;
    normal plant names/species are always returned unmodified."""
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap].rstrip() + "…", True


class SectionHeaderWidget(QWidget):
    """Full-width divider row used when the grid is grouped by genus or year.

    Shows a horizontal line and a bold label for the group name so the user
    can immediately see which section they are looking at.
    """
    def __init__(self, label: str, palette: dict, parent=None):
        super().__init__(parent)
        self._pal = palette
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 4)
        lay.setSpacing(10)

        lbl = QLabel(label)
        lbl.setFont(QFont("Georgia", 11, QFont.Weight.Bold))
        lbl.setStyleSheet(
            f"color: {palette.get('text_header', '#6ee880')}; background: transparent;")
        lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet(
            f"color: {palette.get('edge', '#2d4a35')}; "
            f"background: {palette.get('edge', '#2d4a35')};")
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay.addWidget(lbl)
        lay.addWidget(line)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


_CARDS_FIRST_PAINT = 40
_CARDS_PER_TICK = 40


class _CardTextArea(QWidget):
    """Text panel with rounded bottom corners, painted directly."""
    def __init__(self):
        super().__init__()
        self._c = QColor("#1c2a1c")
    def set_color(self, c: QColor) -> None:
        if c != self._c:
            self._c = c; self.update()
    def paintEvent(self, event) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect(); p.setClipRect(r); p.setPen(Qt.PenStyle.NoPen); p.setBrush(self._c)
        p.drawRoundedRect(QRectF(r.left(), r.top() - 10, r.width(), r.height() + 10), 8, 8)
        p.end()


class MiniPlantCard(QFrame):
    """A compact visual card representing one plant."""
    edit_requested           = pyqtSignal(object)   # PlantNode
    delete_requested         = pyqtSignal(object)   # PlantNode
    add_to_lineage_requested = pyqtSignal(object)   # PlantNode
    selected_signal          = pyqtSignal(object, bool)  # (PlantNode, ctrl_held)

    def __init__(self, plant, palette: dict, role: str = "", card_font: str = "Georgia",
                 card_font_size: int = 9, parent=None):
        super().__init__(parent)
        self.plant   = plant
        self._pal    = palette
        self._role   = role          # pre-computed "mother"/"father"/""
        self._card_font = card_font
        self._card_font_size = card_font_size
        self._selected = False
        self._image_loaded = False    # set True once _load_image() completes
        # Last (hovered, selected) pair passed to _apply_style(), plus the
        # stylesheet text generated for each state.  A card's chrome only has
        # four possible looks, and _apply_style() is now called for every
        # visible card on every rebuild (via set_selected) — re-running
        # setStyleSheet + unpolish/polish on an unchanged state was the single
        # most expensive thing the grid did, at ~0.5 ms per card per rebuild.
        self._style_state: Optional[tuple] = None
        self._chrome = (QColor('#1a2a1e'), QColor('#2e4a34'), 2)
        self._style_cache: Dict[tuple, str] = {}
        # Fixed width keeps grid columns razor-aligned; height is a *minimum*
        # only, so a short name still gets the familiar, consistent card
        # silhouette (Qt fills the slack via the addStretch() in _build()),
        # while a longer one grows the card instead of clipping the text.
        self.setFixedWidth(CARD_W)
        self.setMinimumHeight(CARD_H)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip(f"{plant.name}\n{plant.species or ''}\n{plant.year or ''}")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._build()
        self._apply_style(False, False)

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)     # F65: was the stylesheet border inset
        lay.setSpacing(0)

        self._img_lbl = QLabel()
        self._img_lbl.setFixedSize(CARD_W, IMG_H)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setScaledContents(False)
        # Show placeholder immediately; the grid view's decode queue will call
        # _load_image() asynchronously to avoid a synchronous burst on load.
        self._img_lbl.setText("🌿")
        self._img_lbl.setFont(QFont("Segoe UI", 28))
        # Purely decorative — without this, a click anywhere on the photo
        # (i.e. almost the whole card) is delivered to this QLabel instead
        # of bubbling to MiniPlantCard, so mousePressEvent/set_selected()
        # below never fires and the card silently never highlights.
        self._img_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(self._img_lbl)

        text_w = _CardTextArea()
        text_w.setObjectName("card_text_area")
        self._text_area = text_w   # kept so _apply_style can force-repolish it
        text_w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        tl = QVBoxLayout(text_w)
        tl.setContentsMargins(8, 6, 8, 6)
        tl.setSpacing(2)

        name_text, name_capped = _capped_card_text(self.plant.name or "Unnamed")
        name_lbl = QLabel(name_text)
        name_lbl.setWordWrap(True)
        name_lbl.setFont(QFont(self._card_font, self._card_font_size, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {self._pal.get('node_name','#e8f5e8')}; background: transparent;")
        # Max-width (not max-height) is what keeps this label inside the
        # card: it forces wrapping at the card's width instead of the
        # label demanding a single wide line. Height is intentionally left
        # unconstrained so wrapped names are never clipped — a short name
        # still yields the same card height as before via the addStretch()
        # below; a long one simply makes its own card a bit taller.
        name_lbl.setMaximumWidth(CARD_W - 16)    # 16 = left+right margin (8+8)
        name_lbl.setToolTip((self.plant.name or "") if name_capped else "")
        name_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        tl.addWidget(name_lbl)

        if self.plant.species:
            sp_text, sp_capped = _capped_card_text(self.plant.species)
            sp_f = QFont(self._card_font, max(6, self._card_font_size - 1)); sp_f.setItalic(True)
            # Same treatment as the name: wrap in full rather than elide to
            # one line, so nothing is hidden behind a "…" the user has to
            # hover to reveal.
            sp_lbl = QLabel(sp_text)
            sp_lbl.setWordWrap(True)
            sp_lbl.setFont(sp_f)
            sp_lbl.setMaximumWidth(CARD_W - 16)
            sp_lbl.setStyleSheet(f"color: {self._pal.get('text_muted','#90b898')}; background: transparent;")
            if sp_capped:
                sp_lbl.setToolTip(self.plant.species)
            sp_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            tl.addWidget(sp_lbl)

        role_label = self._get_role_label()
        if role_label:
            role_col = "#e87060" if "Mother" in role_label else "#60a0e8"
            role_lbl = QLabel(role_label)
            role_lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
            role_lbl.setStyleSheet(f"color: {role_col}; background: transparent;")
            role_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            tl.addWidget(role_lbl)

        if self.plant.year:
            yr_lbl = QLabel(str(self.plant.year))
            yr_lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            yr_lbl.setStyleSheet(
                f"color: {self._pal.get('year_fg','#f0b84a')}; "
                f"background: {self._pal.get('year_bg','#3d2e00')}; "
                f"border-radius: 6px; padding: 1px 6px;")
            yr_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            tl.addWidget(yr_lbl)

        if self.plant.tags:
            tags = self.plant.tags
            shown = tags[:_CARD_TAG_DISPLAY_CAP]
            tag_str = "  ".join(f"[{t}]" for t in shown)
            overflow = len(tags) - len(shown)
            if overflow > 0:
                tag_str += f"  +{overflow} more"
            tag_lbl = QLabel(tag_str)
            tag_lbl.setFont(QFont("Segoe UI", 7))
            tag_lbl.setStyleSheet(f"color: {self._pal.get('tag_fg','#70d888')}; background: transparent;")
            tag_lbl.setWordWrap(True)
            tag_lbl.setMaximumWidth(CARD_W - 16)
            if overflow > 0:
                tag_lbl.setToolTip("  ".join(f"[{t}]" for t in tags))
            tag_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            tl.addWidget(tag_lbl)

        tl.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        edit_btn = QPushButton("✏️")
        edit_btn.setFixedSize(28, 22)
        edit_btn.setToolTip("Edit plant")
        edit_btn.setStyleSheet(
            f"QPushButton {{ background: {self._pal.get('btn_bg','#1e3028')}; "
            f"color: {self._pal.get('accent','#4ecb6a')}; border: none; border-radius: 4px; font-size: 10px; }}"
            f"QPushButton:hover {{ background: {self._pal.get('btn_hover','#284038')}; }}"
        )
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.plant))

        del_btn = QPushButton("🗑️")
        del_btn.setFixedSize(28, 22)
        del_btn.setToolTip("Delete plant from database")
        del_btn.setStyleSheet(
            f"QPushButton {{ background: {self._pal.get('btn_bg','#1e3028')}; "
            f"color: #e87060; border: none; border-radius: 4px; font-size: 10px; }}"
            f"QPushButton:hover {{ background: #3a1010; }}"
        )
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self.plant))

        # Buttons stay fully interactive — only the labels/containers around
        # them are made click-transparent above, so pointer events on the
        # buttons themselves are unaffected.
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        tl.addLayout(btn_row)

        lay.addWidget(text_w)

    def _get_role_label(self) -> str:
        if self._role == "mother": return "♀ Mother"
        if self._role == "father": return "♂ Father"
        return ""

    def _load_image(self) -> None:
        import plant_db as _pdb
        if not self.plant.has_photo:
            self._show_placeholder()
            return

        cache_key = f"thumb:{self.plant.id}:{CARD_W}x{IMG_H}"
        cached = QPixmapCache.find(cache_key)
        if cached is not None and not cached.isNull():
            self._img_lbl.setPixmap(cached)
            self._image_loaded = True
            return

        b64 = self.plant.thumb
        using_full_res = False
        if not b64:
            # Legacy plant saved before thumbnails existed — fall back to
            # the full-resolution original ONCE, and generate/persist a
            # thumbnail so this branch never runs again for this plant.
            imgs = _pdb.get_images(self.plant.id)
            if imgs:
                b64 = imgs[0]
                using_full_res = True

        try:
            if b64:
                try:
                    pix = QPixmap()
                    pix.loadFromData(base64.b64decode(b64))
                    if not pix.isNull():
                        scaled = pix.scaled(
                            QSize(CARD_W, IMG_H),
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
                        x_off = max(0, (scaled.width()  - CARD_W) // 2)
                        y_off = max(0, (scaled.height() - IMG_H)  // 2)
                        cropped = scaled.copy(x_off, y_off, CARD_W, IMG_H)
                        QPixmapCache.insert(cache_key, cropped)
                        if using_full_res:
                            import image_utils
                            thumb = image_utils.make_thumb_from_b64(b64)
                            if thumb:
                                self.plant.thumb = thumb
                                _pdb.set_thumb(self.plant.id, thumb)
                        self._img_lbl.setPixmap(cropped)
                        self._image_loaded = True
                        return
                    else:
                        log.warning(
                            "MiniCard: image data for %s did not decode as a valid "
                            "image (corrupted file or unsupported format) — showing "
                            "placeholder instead.", self.plant.id)
                except Exception as e:
                    log.warning("MiniCard image decode failed for %s: %s", self.plant.id, e)
            self._show_placeholder()
        finally:
            # Whether decode succeeded or failed, the full-resolution bytes
            # pulled into plant_db._img_store above are no longer needed here:
            # on success the result now lives in QPixmapCache instead, and on
            # failure _show_placeholder() has already latched _image_loaded so
            # this method won't run again for this card — meaning without this,
            # an undecodable legacy image's bytes would otherwise sit resident
            # in RAM for the rest of the session with no way to reclaim them.
            if using_full_res:
                _pdb.evict_images(self.plant.id)

    def _show_placeholder(self) -> None:
        self._img_lbl.setText("🌿")
        self._img_lbl.setFont(QFont("Segoe UI", 28))
        self._img_lbl.setStyleSheet(
            f"background: {self._pal.get('placeholder','#1e2e22')}; "
            f"color: {self._pal.get('placeholder_icon','#3a6040')}; border-radius: 0px;")
        self._image_loaded = True

    def _apply_style(self, hovered: bool, selected: bool) -> None:
        """F65: card chrome is painted (paintEvent) instead of restyled with
        setStyleSheet + unpolish/polish (≈0.3-1.1 ms per card per state change)."""
        state = (bool(hovered), bool(selected))
        if state == self._style_state:
            return
        self._style_state = state
        hovered, selected = state
        accent   = self._pal.get("accent",     "#4ecb6a")
        bg       = self._pal.get("node_bg",    "#1a2a1e")
        border   = self._pal.get("node_border","#2e4a34")
        panel_bg = self._pal.get("panel_bg2",  "#1c2a1c")
        bw = 2
        if selected:
            border, bw = accent, 3
            bg, panel_bg = QColor(accent).darker(300), QColor(accent).darker(350)
        elif hovered:
            border = accent
        self._chrome = (QColor(bg), QColor(border), bw)
        if hasattr(self, "_text_area"):
            self._text_area.set_color(QColor(panel_bg))
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bgc, bc, bw = self._chrome
        p.setBrush(bgc); p.setPen(QPen(bc, bw))
        h = bw / 2.0
        p.drawRoundedRect(QRectF(self.rect()).adjusted(h, h, -h, -h), 10, 10)
        p.end()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_style(False, selected)

    def enterEvent(self, event):
        self._apply_style(True, self._selected); super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_style(False, self._selected); super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self.selected_signal.emit(self.plant, ctrl)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.edit_requested.emit(self.plant)
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        # Do NOT handle Delete here — let PlantGridView's eventFilter handle it
        # so multi-selected plants are all deleted together.
        super().keyPressEvent(event)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        a_edit    = menu.addAction("✏️  Edit Plant")
        a_lineage = menu.addAction("🌿  Add to Lineage")
        menu.addSeparator()
        a_delete  = menu.addAction("🗑️  Delete Plant")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == a_edit:
            self.edit_requested.emit(self.plant)
        elif chosen == a_lineage:
            self.add_to_lineage_requested.emit(self.plant)
        elif chosen == a_delete:
            self.delete_requested.emit(self.plant)


class PlantGridView(QWidget):
    """
    Full-screen embedded widget showing all plants from the database as a visual grid.
    """
    add_to_lineage         = pyqtSignal(object)   # PlantNode
    switch_to_lineage_view = pyqtSignal()          # emitted by "Back to Lineage" button
    sort_changed           = pyqtSignal(str)       # emitted whenever the user changes the sort combo

    def __init__(self, palette: dict = None, parent=None, sort_mode: str = "name"):
        super().__init__(parent)
        self._pal          = palette or {}
        self._card_font      = "Georgia"
        self._card_font_size = 9
        self._all_plants: list = []
        # id → PlantNode index.  Several hot paths (spreadsheet cell edits,
        # context menus, parent-name resolution) used to scan _all_plants
        # linearly; with a large database each lookup was O(n) and repeated
        # lookups inside row population made spreadsheet refresh O(n²).
        self._plants_by_id: Dict[str, "PlantNode"] = {}
        self._built_rev: int = -1
        self._role_map_cache: Optional[Dict[str, str]] = None
        self._sort_by      = sort_mode if sort_mode in ("name", "species", "year", "newest") else "name"
        self._dirty        = True
        self._selected_plants: list = []  # multi-select list
        # Cache: plant_id -> MiniPlantCard — avoids recreating cards on sort/filter
        self._card_cache: Dict[str, MiniPlantCard] = {}
        # Incremental layout build — see _rebuild_grid / _flush_plan.
        self._plan: list = []
        self._plan_index: int = 0
        self._plan_roles: Dict[str, str] = {}
        self._plan_selected: set = set()
        self._card_build_timer = QTimer(self)
        self._card_build_timer.setInterval(0)     # fire between event-loop iterations
        self._card_build_timer.timeout.connect(self._build_next_card_batch)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._rebuild_current_view)

        # Staggered image decode queue — processes ONE card per event-loop tick
        # so card grid construction is instant and images appear progressively.
        self._img_decode_queue: collections.deque = collections.deque()
        self._img_decode_timer = QTimer(self)
        self._img_decode_timer.setInterval(0)   # fire between event-loop iterations
        self._img_decode_timer.timeout.connect(self._decode_next_card)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build_ui()

    def apply_card_settings(self, palette: dict, font: str, font_size: int) -> None:
        """Apply palette + font + font-size in one shot to avoid triple rebuild.

        Called from MainWindow._apply_settings instead of three separate calls
        (update_palette / set_card_font / set_card_font_size).  Only triggers
        _refresh() once at the end instead of up to three times.
        """
        changed = False
        if palette != self._pal:
            self._pal = palette
            # F37: thumbnails don't depend on theme colours — keep them cached
            changed = True
        if font != self._card_font:
            self._card_font = font
            changed = True
        if font_size != self._card_font_size:
            self._card_font_size = font_size
            changed = True
        if changed:
            self._card_cache.clear()   # stale widgets (wrong colours / font)
            self._selected_plants = []
            self._dirty = True
            if self.isVisible():
                if self._stack.currentIndex() == 1:
                    self._sheet_tabs.update_palette(palette)
                self._refresh()

    def set_card_font(self, font_family: str) -> None:
        if self._card_font == font_family:
            return
        self._card_font = font_family
        self._card_cache.clear()
        self._selected_plants = []
        self._dirty = True
        if self.isVisible():
            self._refresh()

    def set_card_font_size(self, size: int) -> None:
        if self._card_font_size == size:
            return
        self._card_font_size = size
        self._card_cache.clear()
        self._selected_plants = []
        self._dirty = True
        if self.isVisible():
            self._refresh()

    def update_palette(self, palette: dict) -> None:
        self._pal = palette
        # Invalidate entire cache since colours changed
        self._card_cache.clear()
        self._selected_plants = []
        self._dirty = True
        if self.isVisible():
            self._sheet_tabs.update_palette(palette)
            self._refresh()

    def mark_dirty(self) -> None:
        self._dirty = True

    def debug_ram_stats(self) -> dict:
        """Snapshot of this view's own in-RAM footprint (cached card widgets
        + the current unfiltered plant list), for the Debug → Show RAM
        Usage dialog."""
        return {
            "cached_card_widgets": len(self._card_cache),
            "all_plants_loaded":   len(self._all_plants),
        }

    def set_lineage_open(self, name: Optional[str]) -> None:
        """Show or hide the 'Back to Lineage' button.

        Call with the lineage name whenever a lineage is loaded, or None
        when no lineage is open (startup / after all lineages are closed).
        """
        if name:
            short = name if len(name) <= 28 else name[:25] + "…"
            self._back_btn.setText(f"↩  {short}")
            self._back_btn.setVisible(True)
        else:
            self._back_btn.setVisible(False)

    # ── Column definitions — must match CSV export order exactly ─────────────
    _SPREADSHEET_COLS = [
        ("Name",        "name"),
        ("Species",     "species"),
        ("Cultivar",    "cultivar"),
        ("Year",        "year"),
        ("Parent 1",    "_parent1"),
        ("Parent 2",    "_parent2"),
        ("Color",       "color"),
        ("Tags",        "_tags"),
        ("Description", "description"),
    ]

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        hdr_row = QHBoxLayout()
        hdr = QLabel("🌿  Plant Database — Grid View")
        hdr.setFont(QFont("Georgia", 14, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {self._pal.get('text_header','#6ee880')}; background: transparent;")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color: {self._pal.get('text_muted','#90b898')}; background: transparent;")
        hdr_row.addWidget(self._count_lbl)

        # ── Back to Lineage button ────────────────────────────────────────────
        # Hidden until a lineage is loaded.  Clicking it switches back to the
        # canvas view without the user having to find the toolbar button.
        self._back_btn = QPushButton("↩  Back to Lineage View")
        self._back_btn.setToolTip("Switch back to the lineage canvas  (Ctrl+Tab)")
        self._back_btn.setVisible(False)
        self._back_btn.clicked.connect(self.switch_to_lineage_view.emit)
        hdr_row.addWidget(self._back_btn)
        root.addLayout(hdr_row)

        ctrl_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Search by name, species, cultivar, tag…")
        self._search.textChanged.connect(lambda: self._filter_timer.start(200))
        ctrl_row.addWidget(self._search, 1)

        self._sort_combo = QComboBox()
        self._sort_combo.addItems(
            ["Sort: Name A–Z", "Sort: Species", "Sort: Year", "Sort: Newest Added"])
        self._sort_combo.setToolTip(
            "Applies to both Card Grid and Spreadsheet views")
        # Reflect the persisted sort mode BEFORE connecting the signal so
        # restoring it here doesn't immediately re-fire a save/rebuild.
        self._sort_combo.setCurrentIndex(
            {"name": 0, "species": 1, "year": 2, "newest": 3}.get(self._sort_by, 0))
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        ctrl_row.addWidget(self._sort_combo)

        btn_add = QPushButton("➕  Add New Plant")
        btn_add.clicked.connect(self._add_plant)
        ctrl_row.addWidget(btn_add)

        self._btn_del_sel = QPushButton("🗑️  Delete Selected")
        self._btn_del_sel.setEnabled(False)
        self._btn_del_sel.setToolTip("Delete all selected plants (Ctrl+click to select multiple)")
        self._btn_del_sel.clicked.connect(self._delete_selected_batch)
        ctrl_row.addWidget(self._btn_del_sel)

        # ── Export buttons ──────────────────────────────────────────────────
        ctrl_row.addStretch()
        btn_export_png = QPushButton("📷 Export PNG")
        btn_export_png.setToolTip("Export the grid view to a PNG image")
        btn_export_png.clicked.connect(self._export_grid_png)
        ctrl_row.addWidget(btn_export_png)

        btn_export_pdf = QPushButton("📄 Export PDF")
        btn_export_pdf.setToolTip("Export the grid view to a PDF document")
        btn_export_pdf.clicked.connect(self._export_grid_pdf)
        ctrl_row.addWidget(btn_export_pdf)

        root.addLayout(ctrl_row)

        # ── Stacked widget: page 0 = card grid, page 1 = spreadsheet ───────
        self._stack = QStackedWidget()

        # Page 0 — card grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.installEventFilter(self)
        self._scroll.viewport().installEventFilter(self)
        self._grid_widget = QWidget()
        self._grid_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._grid_widget.customContextMenuRequested.connect(self._canvas_context_menu)
        # Lets eventFilter() see left-clicks that land on empty grid space
        # (i.e. not absorbed by a card) so they can deselect the current
        # selection — see eventFilter().
        self._grid_widget.installEventFilter(self)
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(PAD, PAD, PAD, PAD)
        self._grid_layout.setSpacing(PAD)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._grid_widget)
        self._stack.addWidget(self._scroll)           # index 0

        # Page 1 — multi-sheet spreadsheet
        self._sheet_tabs = SheetTabWidget(palette=self._pal, parent=self)
        # Also keep the legacy plant-DB table on the first built-in sheet
        self._rebuild_plant_sheet_on_next_show = True
        self._stack.addWidget(self._sheet_tabs)   # index 1

        root.addWidget(self._stack, 1)

    # ── Plant database sheet (tab 0 in SheetTabWidget) ──────────────────────

    _PLANT_SHEET_NAME = "🌿 Plant Database"

    def _get_or_create_plant_sheet(self) -> "SheetTable":
        tabs = self._sheet_tabs._tabs
        for i in range(tabs.count()):
            if tabs.tabText(i) == self._PLANT_SHEET_NAME:
                return tabs.widget(i)
        table = SheetTable(parent=self._sheet_tabs)
        table.setStyleSheet(self._sheet_tabs._css_for_table())
        table.itemChanged.connect(self._on_table_cell_changed)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._table_context_menu)
        table.installEventFilter(self)
        tabs.insertTab(0, table, self._PLANT_SHEET_NAME)
        tabs.setCurrentIndex(0)
        return table

    def _rebuild_plant_sheet(self) -> None:
        import plant_db as _pdb
        if self._dirty:
            self._all_plants = _pdb.get_all()
            self._plants_by_id = {p.id: p for p in self._all_plants}
            self._dirty = False
        q = self._search.text().strip()
        plants = _pdb.search(q) if q else list(self._all_plants)
        plants = self._sorted_plants(plants)
        table = self._get_or_create_plant_sheet()
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(plants))
        table.setColumnCount(len(self._SPREADSHEET_COLS))
        table.setHorizontalHeaderLabels([c[0] for c in self._SPREADSHEET_COLS])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Resolve parent display names ONCE for the whole sheet.  Previously
        # _plant_to_row_values() rebuilt this id→name dict for every plant,
        # making a spreadsheet refresh O(n²) — the dominant cost on a large
        # database.
        id_to_name = {pid: (p.name or "") for pid, p in self._plants_by_id.items()}
        table.setUpdatesEnabled(False)
        try:
            for row, plant in enumerate(plants):
                values = self._plant_to_row_values(plant, id_to_name)
                for col, val in enumerate(values):
                    item = QTableWidgetItem(val)
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, plant.id)
                    if col == 6 and val and val.startswith("#"):
                        try:
                            sw = QColor(val)
                            item.setBackground(QBrush(QColor(sw.red(), sw.green(), sw.blue(), 60)))
                            item.setForeground(QBrush(QColor(val)))
                        except Exception:
                            pass
                    table.setItem(row, col, item)
        finally:
            table.setUpdatesEnabled(True)
        col_widths = [160, 140, 120, 55, 130, 130, 70, 120, 220]
        for i, w in enumerate(col_widths):
            table.setColumnWidth(i, w)
        # Re-enabling sorting makes Qt immediately re-sort by whatever
        # column sort-indicator is still active from a previous header
        # click (or its own default) — silently overriding the row order
        # we just built from the sort combo. Column -1 tells Qt "no
        # indicator, leave rows exactly as inserted", so the combo's
        # choice actually sticks; clicking a header afterwards still
        # works normally and takes over from there.
        table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        table.setSortingEnabled(True)
        table.blockSignals(False)
        n = len(plants)
        self._count_lbl.setText(f"{n} plant{'s' if n != 1 else ''}")

    def _on_table_cell_changed(self, item: "QTableWidgetItem") -> None:
        import plant_db as _pdb
        row = item.row()
        col = item.column()
        plant_sheet = self._get_or_create_plant_sheet()
        id_item = plant_sheet.item(row, 0)
        if id_item is None:
            return
        plant_id = id_item.data(Qt.ItemDataRole.UserRole)
        if not plant_id:
            return
        plant = self._plants_by_id.get(plant_id)
        if plant is None:
            return
        new_val = item.text().strip()
        col_key = self._SPREADSHEET_COLS[col][1]
        if col_key in ("_parent1", "_parent2"):
            return
        if col_key == "year":
            try:
                plant.year = int(new_val) if new_val else None
            except ValueError:
                plant_sheet.blockSignals(True)
                item.setText(str(plant.year) if plant.year else "")
                plant_sheet.blockSignals(False)
                return
        elif col_key == "_tags":
            plant.tags = [t.strip() for t in new_val.split(",") if t.strip()]
        elif col_key == "description":
            plant.description = new_val
        else:
            setattr(plant, col_key, new_val)
        _pdb.upsert(plant)
        if plant_id in self._card_cache:
            self._card_cache[plant_id].setParent(None)
            del self._card_cache[plant_id]

    def _table_context_menu(self, pos: QPoint) -> None:
        plant_sheet = self._get_or_create_plant_sheet()
        index = plant_sheet.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        id_item = plant_sheet.item(row, 0)
        plant_id = id_item.data(Qt.ItemDataRole.UserRole) if id_item else None
        plant = self._plants_by_id.get(plant_id)
        menu   = QMenu(self)
        a_edit = menu.addAction("✏️  Edit Plant…")
        a_lin  = menu.addAction("🌿  Add to Lineage")
        menu.addSeparator()
        a_del  = menu.addAction("🗑️  Delete Plant")
        chosen = menu.exec(plant_sheet.mapToGlobal(pos))
        if plant is None:
            return
        if chosen == a_edit:
            self._edit_plant(plant)
        elif chosen == a_lin:
            self.add_to_lineage.emit(plant)
        elif chosen == a_del:
            self._delete_plant(plant)

    # ── Public API ────────────────────────────────────────────────────────────

    def _toggle_view_mode(self, show_sheet: bool) -> None:
        """Switch between card grid (False) and spreadsheet (True). The
        sort combo applies to both — it used to be disabled (and the
        spreadsheet's row order hardcoded to name-only) whenever the
        spreadsheet was active."""
        if show_sheet:
            self._stack.setCurrentIndex(1)
            self._rebuild_plant_sheet()
        else:
            self._stack.setCurrentIndex(0)
            self._rebuild_grid()

    # ── Grid-view export (PNG / PDF) ─────────────────────────────────────────

    def _grab_grid(self) -> "QPixmap":
        """Capture the ENTIRE grid or spreadsheet — every row/column, not just
        whatever is currently scrolled into view.

        Card grid: self._grid_widget (the QScrollArea's contained widget)
        already has its true full-content size — setWidgetResizable(True)
        only means it *shrinks* to fit the viewport when there's room, it
        still grows past the viewport for everything else. self._scroll (or
        self._stack.currentWidget(), which is the same thing) is just the
        viewport-sized frame around it, so grabbing that instead of
        self._grid_widget is what was clipping the export to whatever was
        currently visible on screen.

        Spreadsheet: see SheetTabWidget.grab_full_content() — a plain
        resize-then-grab doesn't work here because the table is an active
        QTabWidget page, so it needs a brief detach first.
        """
        if self._stack.currentIndex() == 1:
            return self._sheet_tabs.grab_full_content()
        else:
            return self._grid_widget.grab()

    def _export_grid_png(self) -> None:
        """Export the ENTIRE plant grid / spreadsheet (all rows, not just
        what's currently visible on screen) as a PNG image."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Grid as PNG", "plant_grid.png", "PNG Image (*.png)")
        if not path:
            return
        pix = self._grab_grid()
        if pix.isNull():
            QMessageBox.critical(self, "Export Error", "Nothing to capture."); return
        img = pix.toImage()
        img.save(path, "PNG")
        flash_status(self, f"Grid exported to {os.path.basename(path)}")

    def _export_grid_pdf(self) -> None:
        """Export the ENTIRE plant grid / spreadsheet as a single-page PDF
        sized to fit the content at full resolution.

        Uses QPdfWriter (built into PyQt6.QtGui, same as the lineage canvas
        export) rather than QPrinter from PyQt6.QtPrintSupport — that's an
        optional package this project's own requirements.txt says isn't
        needed, so depending on it here was a latent trap for anyone on a
        minimal PyQt6 install. The page is sized to match the captured
        content's aspect ratio instead of forcing everything onto one fixed
        A3 sheet, so a large grid doesn't get shrunk down to illegible size
        to make it fit.
        """
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Grid as PDF", "plant_grid.pdf", "PDF Document (*.pdf)")
        if not path:
            return

        pix = self._grab_grid()
        if pix.isNull():
            QMessageBox.critical(self, "Export Error", "Nothing to capture."); return

        try:
            from PyQt6.QtGui import QPdfWriter, QPageSize, QPageLayout, QPainter
            from PyQt6.QtCore import QMarginsF, QRectF, QSizeF

            writer = QPdfWriter(path)
            writer.setTitle("BreedingCharts Plant Grid")
            writer.setCreator("BreedingCharts")

            # Render at ~150 DPI so text on cards stays crisp, but cap the
            # physical page size to something PDF viewers/printers handle
            # comfortably (same 200-inch-per-side ceiling used for the
            # lineage canvas PDF export, for consistency).
            dpi = 150.0
            max_inches = 200.0
            page_w_in = min(max_inches, pix.width()  / dpi)
            page_h_in = min(max_inches, pix.height() / dpi)

            writer.setResolution(int(dpi))
            page_size = QPageSize(QSizeF(page_w_in, page_h_in), QPageSize.Unit.Inch)
            layout = QPageLayout(page_size, QPageLayout.Orientation.Portrait,
                                  QMarginsF(0, 0, 0, 0))
            writer.setPageLayout(layout)

            painter = QPainter(writer)
            if not painter.isActive():
                QMessageBox.critical(self, "PDF Error",
                    "Could not start writing the PDF file."); return
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            target = QRectF(painter.viewport())
            painter.drawPixmap(target, pix, QRectF(pix.rect()))
            painter.end()
        except Exception as e:
            log.error("_export_grid_pdf: %s", e)
            QMessageBox.critical(self, "PDF Error", f"Could not export PDF:\n{e}")
            return

        flash_status(self, f"Grid exported to {os.path.basename(path)}")

    def import_csv(self, path: str) -> None:
        """Import a CSV file into a new sheet tab (called from MainWindow)."""
        self._sheet_tabs.import_csv_file(path)

    def show_spreadsheet(self) -> None:
        """Ensure the Spreadsheet View is active."""
        if self._stack.currentIndex() != 1:
            self._toggle_view_mode(True)
        else:
            self._rebuild_plant_sheet()

    def show_card_grid(self) -> None:
        """Ensure the Card Grid View is active."""
        if self._stack.currentIndex() != 0:
            self._toggle_view_mode(False)

    def current_mode(self) -> str:
        """Return 'cards' or 'sheet' depending on which sub-view is active."""
        return "sheet" if self._stack.currentIndex() == 1 else "cards"

    # ── Spreadsheet data helpers ──────────────────────────────────────────────

    def _plant_to_row_values(self, plant, id_to_name=None) -> list:
        """Return a list of string values in CSV-column order for one plant.

        Callers rebuilding the whole sheet should pass the pre-built id→name
        map; rebuilding it inside this function for every plant made a
        spreadsheet refresh O(n²) and was the dominant cost on large data.
        """
        if id_to_name is None:
            id_to_name = {pid: (p.name or "") for pid, p in self._plants_by_id.items()}
        parents = [id_to_name.get(pid, "") for pid in (plant.parent_ids or [])]
        p1 = parents[0] if len(parents) > 0 else ""
        p2 = parents[1] if len(parents) > 1 else ""
        tags_str = ", ".join(plant.tags) if plant.tags else ""
        desc = (plant.description or "").replace("\n", " ")
        return [
            plant.name or "",
            plant.species or "",
            plant.cultivar or "",
            str(plant.year) if plant.year else "",
            p1,
            p2,
            plant.color or "",
            tags_str,
            desc,
        ]

    def _on_sort_changed(self, index: int) -> None:
        self._sort_by = ["name", "species", "year", "newest"][index]
        if self._stack.currentIndex() == 1:
            self._rebuild_plant_sheet()
        else:
            self._rebuild_grid()
        self.sort_changed.emit(self._sort_by)

    def _canvas_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        a_add  = menu.addAction("➕  Add New Plant")
        menu.addSeparator()
        a_sel  = menu.addAction("☑️  Select All  (Ctrl+A)")
        a_dsel = menu.addAction("☐  Deselect All  (Esc)")
        if self._selected_plants:
            menu.addSeparator()
            a_del = menu.addAction(f"🗑️  Delete {len(self._selected_plants)} Selected")
        else:
            a_del = None
        chosen = menu.exec(self._grid_widget.mapToGlobal(pos))
        if chosen == a_add:
            self._add_plant()
        elif chosen == a_sel:
            self._select_all()
        elif chosen == a_dsel:
            self._deselect_all()
        elif a_del and chosen == a_del:
            self._delete_selected_batch()

    def _decode_next_card(self) -> None:
        """Timer callback — loads one queued card image per event-loop tick.
        Processes live cards only; skips any that were removed since enqueuing."""
        while self._img_decode_queue:
            card = self._img_decode_queue.popleft()
            if card.parent() is not None:   # still attached to the widget tree
                try:
                    card._load_image()
                except Exception as e:
                    log.error("_decode_next_card: unhandled error for %s: %s",
                              getattr(card.plant, 'id', '?'), e)
                    card._image_loaded = True   # prevent infinite retry
                return                           # one per tick — keep UI responsive
        self._img_decode_timer.stop()            # queue drained

    def _rebuild_current_view(self) -> None:
        """Dispatch to the active view's rebuild method."""
        # Flush any stale queued decodes from the previous layout before rebuilding.
        self._img_decode_queue.clear()
        self._img_decode_timer.stop()
        if self._stack.currentIndex() == 1:
            self._rebuild_plant_sheet()
        else:
            self._rebuild_grid()

    def _refresh(self) -> None:
        import plant_db
        self._all_plants = plant_db.get_all()
        self._built_rev = plant_db.get_revision()
        self._plants_by_id = {p.id: p for p in self._all_plants}
        self._role_map_cache = None
        self._dirty = False
        if self._stack.currentIndex() == 1:
            self._rebuild_plant_sheet()
        else:
            self._rebuild_grid()

    def _sorted_plants(self, plants: list) -> list:
        if self._sort_by == "name":
            return sorted(plants, key=lambda p: (p.name or "").lower())
        elif self._sort_by == "species":
            return sorted(plants, key=lambda p: (p.species or "").lower())
        elif self._sort_by == "year":
            return sorted(plants, key=lambda p: p.year or 9999)
        elif self._sort_by == "newest":
            # Newest first; "" (legacy plants saved before created_at
            # existed — see models.PlantNode) sorts last rather than being
            # scattered in with real dates.
            return sorted(plants, key=lambda p: p.created_at or "", reverse=True)
        return plants

    def _dedup_plants(self, plants: list) -> list:
        """
        Remove duplicate entries: keep only the first plant with each
        unique (name, species, cultivar) combination so the grid view
        never shows the same variety twice even if it appears in multiple
        lineages under different IDs.
        """
        seen: set = set()
        out: list = []
        for p in plants:
            key = (
                (p.name    or "").strip().lower(),
                (p.species  or "").strip().lower(),
                ((p.cultivar if hasattr(p, "cultivar") else "") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _build_role_map(self) -> Dict[str, str]:
        """Pre-compute {parent_id: role} from all plants — O(n) not O(n²).

        Cached until _refresh()/a data edit invalidates it; the grid is rebuilt
        on every sort change and search keystroke, and recomputing this scan
        each time is pure waste when the parent roles haven't changed.
        """
        if self._role_map_cache is not None:
            return self._role_map_cache
        role_map: Dict[str, str] = {}
        for p in self._all_plants:
            for pid, role in p.parent_roles.items():
                if role:
                    role_map[pid] = role
        self._role_map_cache = role_map
        return role_map

    @staticmethod
    def _genus_key(plant) -> str:
        """Return the genus (first word of species) for grouping, lowercased."""
        sp = (plant.species or "").strip()
        if not sp:
            return "Unknown"
        return sp.split()[0].capitalize()

    @staticmethod
    def _year_key(plant) -> str:
        y = plant.year
        return str(y) if y else "Unknown"

    def _rebuild_grid(self) -> None:
        import plant_db
        q = self._search.text().strip()
        plants = plant_db.search(q) if q else list(self._all_plants)
        # plant_db.search() already returns name-sorted results, so the
        # common "search while sorting by name" path skipped a redundant
        # full sort here (it previously ran on every keystroke).
        if not (q and self._sort_by == "name"):
            plants = self._sorted_plants(plants)
        self._visible_plants = plants            # F5: what is actually shown

        # ── Update live plant count in header ─────────────────────────────────
        total = len(self._all_plants)
        shown = len(plants)
        if q and shown != total:
            self._count_lbl.setText(f"{shown} of {total} plants")
        else:
            self._count_lbl.setText(f"{total} plant{'s' if total != 1 else ''}")

        # Pre-compute roles so each card doesn't need its own DB call
        role_map = self._build_role_map()
        # Pre-compute selection as a set — the old per-card any() scan was
        # O(plants × selected) on every rebuild.
        selected_ids = {sp.id for sp in self._selected_plants}

        # Evict cached cards for plants no longer shown.  deleteLater() frees
        # the widget (and its child labels/buttons/fonts) instead of merely
        # unparenting it, which otherwise left a hidden top-level widget
        # alive for every filtered-out plant.
        current_ids = {p.id for p in plants}
        stale = [pid for pid in list(self._card_cache) if pid not in current_ids]
        for pid in stale:
            card = self._card_cache.pop(pid)
            card.deleteLater()

        # Evict old section header widgets
        for hw in getattr(self, "_section_headers", []):
            hw.deleteLater()
        self._section_headers: list = []

        # Clear layout items WITHOUT destroying widgets (they stay in cache)
        while self._grid_layout.count():
            self._grid_layout.takeAt(0)

        if not plants:
            empty = QLabel("No plants found." if q else
                          "No plants in database yet.\nClick ➕ Add New Plant to get started.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {self._pal.get('text_muted','#90b898')}; font-size: 14px;")
            self._grid_layout.addWidget(empty, 0, 0)
            self._count_lbl.setText("")
            return

        avail_w = max(self._scroll.viewport().width() - PAD, COL_W)
        n_cols  = max(1, avail_w // (CARD_W + PAD))
        self._last_cols = n_cols                  # F38

        # ── Determine whether to group into sections ───────────────────────────
        # Group by genus (first word of species) when sorted by species,
        # or by year when sorted by year.  Other sort modes show a flat grid.
        do_group = self._sort_by in ("species", "year")
        key_fn   = self._genus_key if self._sort_by == "species" else self._year_key

        # ── Build a placement plan, then apply it incrementally ──────────────
        # Constructing a card is ~1 ms of real work (a dozen widgets, several
        # stylesheets, a layout).  Doing all of them in one go meant a
        # multi-second freeze when opening the grid on a large database, so
        # the plan below is applied in batches: enough for the first screen
        # synchronously (the view is never blank), the rest across subsequent
        # event-loop ticks, leaving the UI responsive throughout.
        plan: list = []
        grid_row  = 0    # current row in QGridLayout (sections consume whole rows)
        col       = 0    # current column within the current card row
        prev_key  = None
        for plant in plants:
            if do_group:
                gk = key_fn(plant)
                if gk != prev_key:
                    if col > 0:          # finish the current card row first
                        grid_row += 1
                        col = 0
                    plan.append(("header", gk, grid_row, n_cols))
                    grid_row += 1
                    prev_key = gk
            plan.append(("card", plant, grid_row, col))
            col += 1
            if col >= n_cols:
                col = 0
                grid_row += 1

        self._plan          = plan
        self._plan_index    = 0
        self._plan_roles    = role_map
        self._plan_selected = selected_ids
        self._card_build_timer.stop()
        self._flush_plan(_CARDS_FIRST_PAINT)
        if self._plan_index < len(self._plan):
            self._card_build_timer.start()
        else:
            self._update_selection_badge()

    def _flush_plan(self, budget: int) -> None:
        """Apply queued layout ops, stopping after `budget` newly built cards.

        Cached cards are re-attached freely — only construction is budgeted,
        since that is the expensive part.
        """
        # Selection can change while the remaining cards are still being
        # built (clicking a card, Ctrl+A), so re-snapshot it each slice
        # rather than relying on the value captured when the plan was made.
        self._plan_selected = {sp.id for sp in self._selected_plants}

        built = 0
        self._grid_widget.setUpdatesEnabled(False)
        try:
            while self._plan_index < len(self._plan):
                op = self._plan[self._plan_index]
                self._plan_index += 1
                if self._apply_plan_op(op):
                    built += 1
                    if built >= budget:
                        break
        finally:
            self._grid_widget.setUpdatesEnabled(True)

    def _apply_plan_op(self, op) -> bool:
        """Place one queued op.  Returns True if it constructed a new card."""
        made_card = False
        if op[0] == "header":
            _, gk, r, span = op
            hw = SectionHeaderWidget(gk, self._pal, parent=self._grid_widget)
            self._grid_layout.addWidget(hw, r, 0, 1, span)
            self._section_headers.append(hw)
            return False

        _, plant, r, c = op
        pid = plant.id
        card = self._card_cache.get(pid)
        if card is None:
            card = MiniPlantCard(plant, self._pal,
                                 role=self._plan_roles.get(pid, ""),
                                 card_font=self._card_font,
                                 card_font_size=max(6, self._card_font_size - 1),
                                 parent=self._grid_widget)
            card.edit_requested.connect(self._edit_plant)
            card.delete_requested.connect(self._delete_plant)
            card.add_to_lineage_requested.connect(self.add_to_lineage.emit)
            card.selected_signal.connect(self._on_card_selected)
            card.installEventFilter(self)
            self._card_cache[pid] = card
            made_card = True
        elif card.parent() is not self._grid_widget:
            card.setParent(self._grid_widget)

        # plant.has_photo comes from the cached metadata, so this
        # avoids a plant_db lock + set lookup for every card.
        if plant.has_photo and not card._image_loaded:
            self._img_decode_queue.append(card)
            if not self._img_decode_timer.isActive():
                self._img_decode_timer.start()

        card.set_selected(pid in self._plan_selected)
        self._grid_layout.addWidget(card, r, c)
        return made_card

    def _build_next_card_batch(self) -> None:
        """Timer callback — finish the pending layout plan a slice at a time."""
        self._flush_plan(_CARDS_PER_TICK)
        if self._plan_index >= len(self._plan):
            self._card_build_timer.stop()
            self._update_selection_badge()

    def _finish_pending_build(self) -> None:
        """Apply every remaining queued op now.

        Used by the PNG/PDF exporters, which must capture a complete grid
        rather than whatever the incremental builder has reached so far.
        """
        if self._plan_index < len(self._plan):
            self._card_build_timer.stop()
            self._flush_plan(len(self._plan))
            self._update_selection_badge()

        self._update_selection_badge()

    def _on_card_selected(self, plant, ctrl_held: bool = False) -> None:
        """Track which cards are selected; Ctrl+click adds/removes from selection."""
        if ctrl_held:
            # Toggle this plant in the multi-select list
            ids = [sp.id for sp in self._selected_plants]
            if plant.id in ids:
                self._selected_plants = [sp for sp in self._selected_plants if sp.id != plant.id]
                if plant.id in self._card_cache:
                    self._card_cache[plant.id].set_selected(False)
            else:
                self._selected_plants.append(plant)
                if plant.id in self._card_cache:
                    self._card_cache[plant.id].set_selected(True)
        else:
            # Single-click: deselect all, select only this one
            old_ids = [sp.id for sp in self._selected_plants]
            self._selected_plants = [plant]
            for oid in old_ids:
                if oid != plant.id and oid in self._card_cache:
                    self._card_cache[oid].set_selected(False)
            if plant.id in self._card_cache:
                self._card_cache[plant.id].set_selected(True)
        # Give keyboard focus to the grid so Del works
        self.setFocus()
        self._update_selection_badge()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # F38: only re-flow when the column count actually changes
        avail_w = max(self._scroll.viewport().width() - PAD, COL_W)
        if max(1, avail_w // (CARD_W + PAD)) != getattr(self, "_last_cols", None):
            self._filter_timer.start(50)

    def eventFilter(self, obj, event) -> bool:
        """Intercept Delete/Backspace from child cards so we can batch-delete,
        and clicks on empty grid space so they clear the current selection."""
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.MouseButtonPress and obj is self._grid_widget:
            # A card sits above the grid widget and absorbs its own clicks,
            # so any press that reaches the grid widget itself normally
            # landed on empty space between/below the cards. Some of a
            # card's own child widgets are click-transparent (so hover/
            # selection styling on the card works correctly — see
            # MiniPlantCard._build), and in some cases Qt can still deliver
            # a duplicate press to the grid widget at a position that's
            # actually over a card; childAt() guards against treating that
            # as an empty-space click and wiping out the selection that was
            # just made.
            if event.button() == Qt.MouseButton.LeftButton and self._selected_plants:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                if self._grid_widget.childAt(pos) is None:
                    self._deselect_all()
            return False
        if event.type() == QEvent.Type.MouseButtonDblClick and obj is self._grid_widget:
            # Double-clicking empty canvas space (not on a card) is a quick
            # way to add a new plant — same shortcut as clicking the "+ Add
            # Plant" button, just without having to reach for it.
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                if self._grid_widget.childAt(pos) is None:
                    self._add_plant()
            return False
        if event.type() == QEvent.Type.KeyPress:
            # In spreadsheet mode: Del on selected rows deletes those plants
            plant_sheet = self._get_or_create_plant_sheet()
            if self._stack.currentIndex() == 1 and obj is plant_sheet:
                if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                    self._delete_selected_table_rows()
                    return True
                return False   # let table handle all other keys (editing, nav)
            # Card grid mode
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                if self._selected_plants:
                    self._delete_selected_batch()
                    return True
            if (event.key() == Qt.Key.Key_A and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._select_all()
                return True
            # Ctrl+F — focus search from anywhere (even when a child card has focus)
            if (event.key() == Qt.Key.Key_F and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._search.setFocus()
                self._search.selectAll()
                return True
            # Enter / Return on a child card — open edit dialog
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._selected_plants:
                    self._edit_plant(self._selected_plants[0])
                    return True
            if event.key() == Qt.Key.Key_Escape:
                if self._search.text():
                    self._search.clear()
                    return True
                self._deselect_all()
                return True
        return False

    def _delete_selected_table_rows(self) -> None:
        """Delete the plants currently selected in the plant-database sheet."""
        # Delegated to the new implementation above — call it directly.
        # (Kept for compatibility with the eventFilter reference above.)
        plant_sheet = self._get_or_create_plant_sheet()
        import plant_db as _pdb
        rows = sorted(set(idx.row() for idx in plant_sheet.selectedIndexes()), reverse=True)
        if not rows:
            return
        plants = []
        for row in rows:
            id_item = plant_sheet.item(row, 0)
            if id_item:
                pid   = id_item.data(Qt.ItemDataRole.UserRole)
                plant = self._plants_by_id.get(pid)
                if plant:
                    plants.append(plant)
        if not plants:
            return
        if len(plants) == 1:
            msg = (f"Permanently delete '{plants[0].name}' from the plant database?\n"
                   "This will NOT remove it from any existing lineages.")
        else:
            preview = ", ".join(p.name for p in plants[:4])
            if len(plants) > 4:
                preview += f" … and {len(plants)-4} more"
            msg = (f"Permanently delete {len(plants)} plants ({preview}) "
                   f"from the plant database?\n"
                   "This will NOT remove them from any existing lineages.")
        reply = QMessageBox.question(
            self, "Delete Plants", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Yes:
            return
        _pdb.remove_many([p.id for p in plants])         # F27: one transaction
        for plant in plants:
            if plant.id in self._card_cache:
                self._card_cache[plant.id].setParent(None)
                del self._card_cache[plant.id]
        self._dirty = True
        self._refresh()

    def _select_all(self) -> None:
        """Select every visible plant card."""
        self._selected_plants = list(getattr(self, "_visible_plants", self._all_plants))   # F5
        sel_ids = {sp.id for sp in self._selected_plants}                                   # F15': O(N)
        for pid, card in self._card_cache.items():
            card.set_selected(pid in sel_ids)
        self.setFocus()
        self._update_selection_badge()

    def _deselect_all(self) -> None:
        """Clear all selections."""
        for card in self._card_cache.values():
            card.set_selected(False)
        self._selected_plants = []
        self._update_selection_badge()

    def _update_selection_badge(self) -> None:
        n = len(self._selected_plants)
        has_sel = n > 0
        if hasattr(self, '_btn_del_sel'):
            self._btn_del_sel.setEnabled(has_sel)
            if n > 1:
                self._btn_del_sel.setText(f"🗑️  Delete {n} Selected")
            else:
                self._btn_del_sel.setText("🗑️  Delete Selected")
        if n > 0:
            self._count_lbl.setText(f"{len(self._all_plants)} plants  •  {n} selected")
        else:
            total = len(self._all_plants)
            self._count_lbl.setText(f"{total} plant{'s' if total != 1 else ''}")

    def keyPressEvent(self, event) -> None:
        key  = event.key()
        mods = event.modifiers()

        # Delete / Backspace — batch delete selected
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_plants:
                self._delete_selected_batch()
                event.accept()
                return

        # Enter / Return — open edit dialog for the first selected plant
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._selected_plants:
                self._edit_plant(self._selected_plants[0])
                event.accept()
                return

        # Ctrl+F — focus the search field
        if key == Qt.Key.Key_F and mods & Qt.KeyboardModifier.ControlModifier:
            self._search.setFocus()
            self._search.selectAll()
            event.accept()
            return

        # Escape — clear search if active, else deselect all cards
        if key == Qt.Key.Key_Escape:
            if self._search.text():
                self._search.clear()
            else:
                self._deselect_all()
            event.accept()
            return

        super().keyPressEvent(event)

    def _delete_selected_batch(self) -> None:
        """Delete all currently selected plants with a single confirmation dialog."""
        import plant_db
        plants = list(self._selected_plants)
        if not plants:
            return
        if len(plants) == 1:
            msg = (f"Permanently delete '{plants[0].name}' from the plant database?\n"
                   "This will NOT remove it from any existing lineages.")
        else:
            names_preview = ", ".join(p.name for p in plants[:4])
            if len(plants) > 4:
                names_preview += f" … and {len(plants)-4} more"
            msg = (f"Permanently delete {len(plants)} plants ({names_preview}) "
                   f"from the plant database?\n"
                   "This will NOT remove them from any existing lineages.")
        reply = QMessageBox.question(
            self, "Delete Plants", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Yes:
            return
        plant_db.remove_many([p.id for p in plants])      # F27: one transaction
        for plant in plants:
            if plant.id in self._card_cache:
                self._card_cache[plant.id].setParent(None)
                del self._card_cache[plant.id]
        self._selected_plants = []
        self._dirty = True
        self._refresh()
        self._update_selection_badge()
        self.setFocus()

    def _edit_plant(self, plant) -> None:
        from dialogs import PlantDialog
        import plant_db
        # Nodes in _all_plants have images=[] for RAM efficiency.
        # Populate them from the image store before opening the dialog so the
        # Images tab and Info tab photo preview work correctly.
        plant_db.populate_images(plant)
        try:
            dlg = PlantDialog(plant, self)
            if dlg.exec():
                plant_db.upsert(plant)
                # Invalidate just this card so it rebuilds with fresh data
                if plant.id in self._card_cache:
                    self._card_cache[plant.id].setParent(None)
                    del self._card_cache[plant.id]
                # Clear cached thumbnail so the new image is decoded on next show
                QPixmapCache.remove(f"thumb:{plant.id}:{CARD_W}x{IMG_H}")
                self._dirty = True
                self._refresh()
        finally:
            # populate_images() above loaded this plant's full-resolution
            # bytes into plant_db._img_store for the dialog to display. Free
            # them again now that the dialog is closed (saved or cancelled) —
            # otherwise every plant ever opened for editing stays resident in
            # RAM for the rest of the session.
            plant.images = []
            plant_db.evict_images(plant.id)

    def _delete_plant(self, plant, confirm_once: bool = False) -> None:
        import plant_db
        if not confirm_once:
            reply = QMessageBox.question(
                self, "Delete Plant",
                f"Permanently delete '{plant.name}' from the plant database?\n"
                "This will NOT remove it from any existing lineages.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return
        plant_db.delete(plant.id)
        if plant.id in self._card_cache:
            self._card_cache[plant.id].setParent(None)
            del self._card_cache[plant.id]
        self._selected_plants = [sp for sp in self._selected_plants if sp.id != plant.id]
        self._dirty = True
        self._refresh()

    def _add_plant(self) -> None:
        from dialogs import PlantDialog
        from models import PlantNode
        import plant_db
        node = PlantNode()
        dlg = PlantDialog(node, self, is_new=True)
        if dlg.exec():
            plant_db.upsert(node)
            self._dirty = True
            self._refresh()

    def showEvent(self, event) -> None:
        """Refresh grid when the view becomes visible — only if data changed."""
        super().showEvent(event)
        import plant_db as _pdbr
        if self._dirty and (self._built_rev != _pdbr.get_revision() or not self._card_cache):
            self._refresh()
        self._dirty = False
        # Focus the search field so the user can type immediately
        QTimer.singleShot(0, self._search.setFocus)
