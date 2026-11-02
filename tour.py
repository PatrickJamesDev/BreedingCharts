"""
tour.py — First-run guided tour dialog for BreedingCharts.

v1.2.0:
  • Tour now highlights the relevant UI widget(s) on the main window as each
    step is discussed, making the tour truly interactive.
  • Pass  widget_map={step_idx: QWidget}  to TourDialog to enable highlighting.
  • Highlighted widgets get a pulsing green-glow border that is cleanly
    removed when the step changes or the dialog closes.
"""
from __future__ import annotations
from typing import Dict, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QWidget,
)
from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush

from ui_common import FadeDialog

# ── Step data ─────────────────────────────────────────────────────────────────

TOUR_STEPS = [
    {
        "title":   "Welcome to BreedingCharts 🌿",
        "icon":    "🌿",
        "widget_keys": [],    # which widget_map keys to highlight
        "body": (
            "BreedingCharts lets you visualize and manage plant breeding lineages — "
            "tracking parent-child relationships, storing photos and notes, and exporting "
            "publication-quality charts.\n\n"
            "This short tour will walk you through the key parts of the interface. "
            "Press <b>Next</b> to continue, or <b>Skip</b> to jump straight in."
        ),
    },
    {
        "title":   "The Lineage Sidebar 📁",
        "icon":    "📁",
        "widget_keys": ["sidebar"],
        "body": (
            "The <b>sidebar on the left</b> lists all your saved lineages.<br><br>"
            "• <b>Double-click</b> an entry to open it on the canvas.<br>"
            "• <b>Right-click</b> for Rename, Delete, Set Category options.<br>"
            "• Use the <b>sort dropdown</b> to order by name, date, or plant count.<br>"
            "• <b>Drag rows</b> to set a custom order.<br>"
            "• The <b>🔍 filter bar</b> supports fuzzy search."
        ),
    },
    {
        "title":   "The Lineage Canvas 🗺️",
        "icon":    "🗺️",
        "widget_keys": ["canvas"],
        "body": (
            "The <b>canvas</b> is your main workspace. Plants appear as cards you can "
            "drag freely.<br><br>"
            "• <b>Hover</b> a card to reveal <b>ports</b> — top (parent side) and "
            "bottom (child side).<br>"
            "• <b>Drag from a port</b> to another card to draw a lineage connection.<br>"
            "• <b>Right-click</b> the canvas to add a plant or comment at that spot.<br>"
            "• <b>Scroll</b> to pan, <b>Ctrl/Cmd+scroll</b> to zoom, <b>middle-drag</b> also pans.<br>"
            "• Press <b>F</b> to fit everything into view."
        ),
    },
    {
        "title":   "Adding & Editing Plants ➕",
        "icon":    "➕",
        "widget_keys": ["btn_add_plant", "btn_connect"],
        "body": (
            "Use the <b>＋ New Plant</b> toolbar button to add a plant.<br><br>"
            "• Fill in name, species, cultivar, year, color, tags, and notes.<br>"
            "• Attach a <b>photo</b> from disk, drag-drop it, or press <b>Ctrl+V</b> "
            "to paste straight from your clipboard — even on the canvas.<br>"
            "• <b>Double-click</b> any card to edit it later.<br>"
            "• Use <b>⇄ Connect</b> to link two plants, then assign Mother / Father roles.<br>"
            "• Press <b>Delete</b> to remove a selected card."
        ),
    },
    {
        "title":   "Grid View & Spreadsheet 📊",
        "icon":    "📊",
        "widget_keys": ["btn_grid_view"],
        "body": (
            "Click <b>⊞ Grid View</b> in the toolbar to switch to a visual card gallery "
            "of all plants in the database.<br><br>"
            "• Inside Grid View, click <b>📊 Spreadsheet View</b> for an Excel-style table.<br>"
            "• <b>Double-click any cell</b> to edit in place.<br>"
            "• <b>Import .csv</b> (File menu) adds a new sheet tab.<br>"
            "• Copy cells from Excel and press <b>Ctrl+V</b> inside a sheet."
        ),
    },
    {
        "title":   "Export & Settings ⚙️",
        "icon":    "⚙️",
        "widget_keys": ["btn_export"],
        "body": (
            "When you're ready to share your work:<br><br>"
            "• <b>📤 Export PNG</b> captures the whole lineage chart at 2× resolution.<br>"
            "• You can add a <b>watermark</b> (text or custom) in Settings → Export.<br>"
            "• <b>File → Export Plant List to CSV</b> saves all plant data.<br><br>"
            "Open <b>Settings → ⚙️ All Settings…</b> to customise themes, fonts, "
            "card appearance, autosave interval, and keyboard shortcuts.<br><br>"
            "<i>Happy breeding! 🌱  You can restart this tour any time from Help → Restart Tour…</i>"
        ),
    },
]

ACCENT       = "#4ecb6a"
ACCENT_DIM   = "#2a6040"
BG           = "#141f18"
TEXT         = "#d0e8d4"
TEXT_MUTED   = "#90b898"
PANEL        = "#1c2a1c"
BTN_BG       = "#1e3028"
BTN_HOVER    = "#284038"
BTN_BORDER   = "#3a5a40"

# Style applied to highlighted widgets
_HIGHLIGHT_STYLE_TMPL = """
    background: {orig_bg};
    border: 2px solid #4ecb6a;
    border-radius: 6px;
"""

# Pulsing border animation using setStyleSheet alternation
_PULSE_ON  = "border: 2px solid #4ecb6a; border-radius:5px; padding:1px;"
_PULSE_OFF = "border: 2px solid #2a6040; border-radius:5px; padding:1px;"


class _DotBar(QWidget):
    """A row of step-indicator dots."""

    def __init__(self, total: int, current: int = 0, parent=None):
        super().__init__(parent)
        self._total   = total
        self._current = current
        self.setFixedHeight(16)

    def set_current(self, idx: int) -> None:
        self._current = idx
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r       = 6
        gap     = 14
        total_w = self._total * (r * 2) + (self._total - 1) * gap
        x0      = (self.width() - total_w) // 2
        y       = self.height() // 2
        for i in range(self._total):
            cx = x0 + i * (r * 2 + gap) + r
            if i == self._current:
                p.setBrush(QBrush(QColor(ACCENT)))
            else:
                p.setBrush(QBrush(QColor(ACCENT_DIM)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(cx - r, y - r, r * 2, r * 2)
        p.end()


class TourDialog(FadeDialog):
    """Step-through guided tour with live widget highlighting."""

    def __init__(self, parent=None,
                 widget_map: Dict[str, "QWidget"] = None):
        """
        widget_map maps string keys (referenced in TOUR_STEPS widget_keys)
        to actual QWidget instances in the main window.
        Example:
            widget_map={
                "sidebar": self._sidebar,
                "btn_add_plant": toolbar_add_btn,
                ...
            }
        """
        super().__init__(parent)
        self._widget_map: Dict[str, QWidget] = widget_map or {}
        self._saved_styles: Dict[str, str] = {}   # key → original stylesheet
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._do_pulse)
        self._pulse_phase = False
        self._current_highlighted: list = []

        self.setWindowTitle("BreedingCharts — Quick Tour")
        self.setMinimumSize(540, 440)
        self.setMaximumSize(620, 520)
        self._step  = 0
        self._total = len(TOUR_STEPS)
        self._build()
        self._refresh()
        self._apply_style()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        # Icon + title row
        top_row = QHBoxLayout()
        self._icon_lbl = QLabel()
        self._icon_lbl.setFont(QFont("Segoe UI Emoji", 28))
        self._icon_lbl.setFixedWidth(52)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        top_row.addWidget(self._icon_lbl)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._step_lbl = QLabel()
        self._step_lbl.setFont(QFont("Segoe UI", 8))
        self._step_lbl.setStyleSheet(f"color: {TEXT_MUTED};")
        title_col.addWidget(self._step_lbl)
        self._title_lbl = QLabel()
        self._title_lbl.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {TEXT};")
        self._title_lbl.setWordWrap(True)
        title_col.addWidget(self._title_lbl)
        top_row.addLayout(title_col, 1)
        lay.addLayout(top_row)

        # Highlight hint
        self._hint_lbl = QLabel()
        self._hint_lbl.setFont(QFont("Segoe UI", 9))
        self._hint_lbl.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        self._hint_lbl.setWordWrap(True)
        lay.addWidget(self._hint_lbl)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {ACCENT_DIM};")
        lay.addWidget(sep)

        # Body text
        self._body_lbl = QLabel()
        self._body_lbl.setFont(QFont("Segoe UI", 10))
        self._body_lbl.setStyleSheet(f"color: {TEXT}; background: transparent;")
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._body_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._body_lbl.setMinimumHeight(200)
        lay.addWidget(self._body_lbl, 1)

        # Dot bar
        self._dots = _DotBar(self._total)
        lay.addWidget(self._dots, alignment=Qt.AlignmentFlag.AlignCenter)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_skip = QPushButton("Skip Tour")
        self._btn_skip.setFixedHeight(32)
        self._btn_skip.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_skip)
        btn_row.addStretch()
        self._btn_prev = QPushButton("◀  Previous")
        self._btn_prev.setFixedHeight(32)
        self._btn_prev.setEnabled(False)
        self._btn_prev.clicked.connect(self._prev)
        btn_row.addWidget(self._btn_prev)
        self._btn_next = QPushButton("Next  ▶")
        self._btn_next.setFixedHeight(32)
        self._btn_next.clicked.connect(self._next)
        btn_row.addWidget(self._btn_next)
        lay.addLayout(btn_row)

    # ── Widget highlighting ─────────────────────────────────────────────────────

    def _clear_highlights(self) -> None:
        """Remove pulsing highlight from all currently highlighted widgets."""
        self._pulse_timer.stop()
        for key in self._current_highlighted:
            w = self._widget_map.get(key)
            if w is None: continue
            orig = self._saved_styles.get(key, "")
            try:
                w.setStyleSheet(orig)
            except Exception:
                pass
        self._current_highlighted = []

    def _apply_highlights(self, keys: list) -> None:
        """Start pulsing highlight on the given widget keys."""
        self._clear_highlights()
        valid = []
        for key in keys:
            w = self._widget_map.get(key)
            if w is None: continue
            if key not in self._saved_styles:
                self._saved_styles[key] = w.styleSheet()
            valid.append(key)
        self._current_highlighted = valid
        if valid:
            self._pulse_phase = True
            self._do_pulse()
            self._pulse_timer.start(600)   # toggle every 600ms

    def _do_pulse(self) -> None:
        self._pulse_phase = not self._pulse_phase
        style = _PULSE_ON if self._pulse_phase else _PULSE_OFF
        for key in self._current_highlighted:
            w = self._widget_map.get(key)
            if w:
                try:
                    w.setStyleSheet(style)
                    w.update()
                except Exception:
                    pass

    # ── Navigation ─────────────────────────────────────────────────────────────

    def _prev(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._refresh()

    def _next(self) -> None:
        if self._step < self._total - 1:
            self._step += 1
            self._refresh()
        else:
            self.accept()

    def _refresh(self) -> None:
        s = TOUR_STEPS[self._step]
        self._icon_lbl.setText(s["icon"])
        self._step_lbl.setText(f"Step {self._step + 1} of {self._total}")
        self._title_lbl.setText(s["title"])
        self._body_lbl.setText(s["body"])
        self._dots.set_current(self._step)
        self._btn_prev.setEnabled(self._step > 0)
        is_last = self._step == self._total - 1
        self._btn_next.setText("Finish  ✓" if is_last else "Next  ▶")
        self._btn_skip.setVisible(not is_last)

        # Widget highlighting
        keys = s.get("widget_keys", [])
        if keys and self._widget_map:
            self._apply_highlights(keys)
            # Build hint label
            names = []
            key_labels = {
                "sidebar":       "Lineage Sidebar (left panel)",
                "canvas":        "Lineage Canvas (centre)",
                "btn_add_plant": "＋ New Plant button",
                "btn_connect":   "⇄ Connect button",
                "btn_grid_view": "⊞ Grid View button",
                "btn_export":    "📤 Export PNG button",
            }
            for k in keys:
                if k in key_labels and k in self._widget_map:
                    names.append(key_labels[k])
            if names:
                self._hint_lbl.setText("👆 Highlighted:  " + "  •  ".join(names))
            else:
                self._hint_lbl.setText("")
        else:
            self._clear_highlights()
            self._hint_lbl.setText("")

    # ── Style ──────────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QDialog {{
                background: {BG};
                color: {TEXT};
            }}
            QLabel {{
                background: transparent;
                color: {TEXT};
            }}
            QPushButton {{
                background: {BTN_BG};
                color: {ACCENT};
                border: 1px solid {BTN_BORDER};
                border-radius: 6px;
                padding: 4px 16px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {BTN_HOVER};
            }}
            QPushButton:disabled {{
                color: {ACCENT_DIM};
                border-color: {ACCENT_DIM};
            }}
        """)

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._clear_highlights()
        super().closeEvent(event)

    def reject(self) -> None:
        self._clear_highlights()
        super().reject()

    def accept(self) -> None:
        self._clear_highlights()
        super().accept()
