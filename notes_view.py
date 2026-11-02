"""
notes_view.py — Notes feature: a responsive Google-Keep-style card grid of
short previews, each opening a rich-text editor.

Editor supports bold / italic / underline / one heading style / bullet &
numbered lists, a handful of pastel card-color tags, pin-to-top, and
pasting images directly from the clipboard (see NoteTextEdit below).

Persistence is notes_store.py. A note is fully self-contained — pasted
images are embedded as base64 data-URIs directly inside the note's HTML —
so nothing here needs a separate image sidecar the way plant photos do.
"""
from __future__ import annotations

import base64
import re
from typing import Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QFrame,
    QLabel, QLineEdit, QPushButton, QToolButton, QTextEdit, QDialog,
    QMessageBox, QGraphicsDropShadowEffect,
)
from PyQt6.QtGui import (
    QFont, QTextCharFormat, QTextListFormat, QTextCursor, QImage, QCursor,
    QPixmap, QColor, QPainter, QPainterPath, QPen,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QMimeData, QPropertyAnimation, QRectF

import image_utils
import notes_store
from models import Note
from ui_common import FadeDialog

CARD_W     = 220
CARD_MIN_H = 150
CARD_RADIUS = 12
PAD        = 16
IMG_PREVIEW_H = 112

# A wide, evenly-distributed, genuinely bright set of note-card colors —
# 14 hues stepped around the full color wheel at matched saturation/
# lightness, so every option reads as clearly, vividly different from its
# neighbors rather than the muddy, closely-clustered dark tones this had
# before. "" = no tint (falls back to the theme's normal card color).
NOTE_COLORS = [
    "",
    "#E85E6A",  # Red
    "#E89F5E",  # Orange
    "#E8C35E",  # Amber
    "#E8DA5E",  # Yellow
    "#B6E85E",  # Lime
    "#5EE87A",  # Green
    "#5EE8CD",  # Teal
    "#5ED1E8",  # Cyan
    "#5EAFE8",  # Sky
    "#5E88E8",  # Blue
    "#715EE8",  # Indigo
    "#A85EE8",  # Purple
    "#E85EE3",  # Magenta
    "#E85E9F",  # Pink
]


def _relative_luminance(hex_color: str) -> float:
    """Standard WCAG relative luminance (0=black, 1=white) — used to pick
    readable text/border/hover colors for an arbitrary card color instead
    of hardcoding a single light-text or dark-text assumption that would
    only work for half the palette."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except Exception:
        return 0.15

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _shade(hex_color: str, factor: float) -> str:
    """Darken (factor < 1) or lighten (factor > 1) a hex color, clamped."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return hex_color
    r, g, b = (max(0, min(255, round(c * factor))) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def note_text_colors(bg_hex: str, theme_pal: dict) -> dict:
    """Return colors tuned for readability against bg_hex, used both for
    grid cards and to tint the editor's whole surface to match. bg_hex ==
    "" means "no tint" — use the theme's own colors unchanged so untinted
    notes look exactly like the rest of the app."""
    if not bg_hex:
        return {
            "bg":            theme_pal.get("node_bg", "#1a2a1e"),
            "title":         theme_pal.get("text", "#e8f5e8"),
            "body":          theme_pal.get("text_muted", "#90b898"),
            "border":        theme_pal.get("node_border", "#2e4a34"),
            "border_hover":  theme_pal.get("accent", "#4ecb6a"),
            "hover_overlay": "rgba(255,255,255,35)",
            "btn_overlay":       theme_pal.get("btn_bg", "#1a2a1e"),
            "btn_overlay_hover": theme_pal.get("btn_hover", "#24382a"),
            "btn_overlay_checked": theme_pal.get("accent", "#4ecb6a"),
        }
    dark_text = _relative_luminance(bg_hex) > 0.45
    return {
        "bg":            bg_hex,
        "title":         "#1c1c1c" if dark_text else "#fbfbfb",
        "body":          "#3a3a3a" if dark_text else "#e6e6e6",
        "border":        _shade(bg_hex, 0.8),
        "border_hover":  _shade(bg_hex, 0.62),
        "hover_overlay": "rgba(0,0,0,40)" if dark_text else "rgba(255,255,255,55)",
        "btn_overlay":         "rgba(0,0,0,16)"  if dark_text else "rgba(255,255,255,22)",
        "btn_overlay_hover":   "rgba(0,0,0,30)"  if dark_text else "rgba(255,255,255,42)",
        "btn_overlay_checked": "rgba(0,0,0,55)"  if dark_text else "rgba(255,255,255,70)",
    }

_DATA_IMG_RE = re.compile(r'src="data:image/[a-zA-Z]+;base64,([^"]+)"')

# Cached preview pixmaps for note cards.  Rebuilding the grid recreates
# every card, which used to re-base64-decode, re-parse, re-scale and
# re-round the same embedded photo on every refresh and every search
# keystroke.  Keying on (note id, modified_at) means an unchanged note is
# decoded exactly once per session, while an edited note automatically
# gets a fresh entry (its timestamp changes).
_preview_cache: Dict[Tuple[str, str], QPixmap] = {}


def _clear_preview_cache() -> None:
    _preview_cache.clear()


def _rounded_top_pixmap(pixmap: QPixmap, radius: int) -> QPixmap:
    """Clip a pixmap's top-left/top-right corners to `radius`, bottom
    corners left square. A photo sitting flush at the top of a rounded
    card needs this — otherwise its hard rectangular corners visibly
    poke out past the card's own rounded frame, which is exactly the
    kind of thing that reads as "cheap" at a glance even if nothing else
    is wrong."""
    if pixmap.isNull():
        return pixmap
    w, h = pixmap.width(), pixmap.height()
    result = QPixmap(w, h)
    result.fill(Qt.GlobalColor.transparent)
    rounded = QPainterPath()
    rounded.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    square_bottom = QPainterPath()
    square_bottom.addRect(QRectF(0, radius, w, h - radius))
    path = rounded.united(square_bottom)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return result


# ── Rich-text editor with paste-image support ───────────────────────────────

class NoteTextEdit(QTextEdit):
    """QTextEdit that turns a pasted/dropped image into an inline,
    self-contained <img> (base64 data-URI) instead of silently dropping
    it — QTextEdit's default insertFromMimeData only handles text/html
    mime data, not image mime data, so without this a Ctrl+V of a
    screenshot or copied photo would just do nothing."""

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasImage():
            qimg = QImage(source.imageData())
            if not qimg.isNull():
                b64 = image_utils.make_thumb_from_qimage(qimg, max_dim=1000, quality=85)
                if b64:
                    self.textCursor().insertHtml(
                        f'<img src="data:image/jpeg;base64,{b64}" style="max-width:100%;" />')
                    return
        super().insertFromMimeData(source)


# ── Note editor dialog ───────────────────────────────────────────────────────

class NoteEditorDialog(FadeDialog):
    """Open/edit a single note.

    There is no destructive Cancel — closing the dialog any way (Done,
    Escape, the window's X) always saves, matching the low-friction,
    always-autosaving feel of Google Keep. The caller is responsible for
    discarding the result if is_blank() is True (a brand-new note the
    user opened and closed without typing anything) or deleted is True.
    """

    def __init__(self, note: Note, palette: dict, parent=None, is_new: bool = False):
        super().__init__(parent)
        self._note = note
        self._pal = palette
        self._is_new = is_new
        self._deleted = False
        self._colors = note_text_colors(note.color, palette)
        self.setWindowTitle("New Note" if is_new else "Edit Note")
        self.setMinimumSize(580, 520)
        self._build()

    # ── Build ────────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        # ── Color bar: its own distinct strip at the very top, separate
        # from the "paper" below — this is the first thing you see when
        # a note opens, and the only interaction it offers is picking a
        # color, so it reads clearly as a toolbar rather than clutter
        # mixed in with Pin/Delete/Done at the bottom.
        self._color_bar = QWidget()
        self._color_bar.setObjectName("note_color_bar")
        cb = QHBoxLayout(self._color_bar)
        cb.setContentsMargins(14, 10, 14, 10)
        cb.setSpacing(7)
        self._color_btns: List[QToolButton] = []
        for c in NOTE_COLORS:
            b = QToolButton(); b.setFixedSize(24, 24); b.setCheckable(True)
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.setAutoExclusive(True)
            b.setToolTip("No color" if not c else "")
            b.setChecked(c == self._note.color)
            b.clicked.connect(lambda _checked=False, col=c, btn=b: self._pick_color(col, btn))
            cb.addWidget(b)
            self._color_btns.append(b)
        cb.addStretch()
        outer.addWidget(self._color_bar)

        # ── Everything else sits on the "paper" with normal margins.
        lay = QVBoxLayout()
        lay.setSpacing(10)
        lay.setContentsMargins(18, 14, 18, 16)
        outer.addLayout(lay)

        self._title = QLineEdit(self._note.title)
        self._title.setPlaceholderText("Title")
        tf = QFont(); tf.setPointSize(15); tf.setBold(True)
        self._title.setFont(tf)
        lay.addWidget(self._title)

        self._toolbar_w = QWidget()
        self._toolbar_w.setObjectName("note_toolbar")
        tb = QHBoxLayout(self._toolbar_w)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(3)

        def tbtn(label: str, tip: str, slot, checkable: bool = False) -> QToolButton:
            b = QToolButton(); b.setText(label); b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setFixedSize(30, 28)
            if checkable:
                b.toggled.connect(slot)
            else:
                b.clicked.connect(slot)
            tb.addWidget(b)
            return b

        self._dividers: List[QFrame] = []

        def divider() -> None:
            line = QFrame(); line.setFrameShape(QFrame.Shape.VLine)
            line.setFixedHeight(20)
            tb.addSpacing(4); tb.addWidget(line); tb.addSpacing(4)
            self._dividers.append(line)

        bf = QFont(); bf.setBold(True)
        it = QFont(); it.setItalic(True)
        uf = QFont(); uf.setUnderline(True)
        self._btn_bold    = tbtn("B",  "Bold (Ctrl+B)",      self._toggle_bold,      True)
        self._btn_bold.setFont(bf)
        self._btn_italic  = tbtn("I",  "Italic (Ctrl+I)",    self._toggle_italic,    True)
        self._btn_italic.setFont(it)
        self._btn_under   = tbtn("U",  "Underline (Ctrl+U)", self._toggle_underline, True)
        self._btn_under.setFont(uf)
        divider()
        self._btn_heading = tbtn("H",  "Heading",            self._toggle_heading,   True)
        divider()
        tbtn("•",  "Bullet list",   self._toggle_bullets)
        tbtn("1.", "Numbered list", self._toggle_numbers)
        tb.addStretch()
        lay.addWidget(self._toolbar_w)

        self._editor = NoteTextEdit()
        self._editor.setFrameShape(QFrame.Shape.NoFrame)
        self._editor.setAcceptRichText(True)
        self._editor.setHtml(self._note.html)
        self._editor.setPlaceholderText(
            "Take a note…  (paste an image directly with Ctrl+V)")
        self._editor.cursorPositionChanged.connect(self._sync_toolbar_state)
        lay.addWidget(self._editor, 1)

        bottom = QHBoxLayout(); bottom.setSpacing(8)
        self._btn_pin = QPushButton("📌  Pinned" if self._note.pinned else "📌  Pin")
        self._btn_pin.setCheckable(True); self._btn_pin.setChecked(self._note.pinned)
        self._btn_pin.toggled.connect(self._toggle_pin)
        bottom.addWidget(self._btn_pin)
        bottom.addStretch()

        self._btn_del = None
        if not self._is_new:
            self._btn_del = QPushButton("🗑  Delete")
            self._btn_del.clicked.connect(self._delete)
            bottom.addWidget(self._btn_del)

        self._btn_done = QPushButton("Done")
        self._btn_done.setDefault(True)
        self._btn_done.clicked.connect(self._done)
        bottom.addWidget(self._btn_done)
        lay.addLayout(bottom)

        self._apply_note_color()
        self._editor.setFocus()
        self._sync_toolbar_state()

    def _apply_note_color(self) -> None:
        """(Re)paint the whole editing surface — color bar, dialog
        background, toolbar, editor, action buttons, swatch selection
        rings — to match the note's current color. Called once at build
        time and again every time the user picks a different color, so
        the note looks the same while you're editing it as it does on
        its card (matching Keep, where the whole note surface is tinted,
        not just a small preview strip)."""
        c = self._colors = note_text_colors(self._note.color, self._pal)

        self.setStyleSheet(f"QDialog {{ background: {c['bg']}; }}")
        self._color_bar.setStyleSheet(
            f"#note_color_bar {{ background: {c['btn_overlay']}; "
            f"border-bottom: 1px solid {c['border']}; }}")
        self._title.setStyleSheet(
            f"border: none; background: transparent; color: {c['title']};")

        self._toolbar_w.setStyleSheet(f"""
            #note_toolbar QToolButton {{
                background: {c['btn_overlay']};
                color: {c['title']};
                border: 1px solid {c['border']};
                border-radius: 6px;
            }}
            #note_toolbar QToolButton:hover {{
                background: {c['btn_overlay_hover']};
            }}
            #note_toolbar QToolButton:checked {{
                background: {c['btn_overlay_checked']};
                border-color: {c['border_hover']};
            }}
            #note_toolbar QToolButton:pressed {{
                background: {c['btn_overlay']};
            }}
        """)
        for line in self._dividers:
            line.setStyleSheet(f"color: {c['border']};")

        self._editor.setStyleSheet(
            f"background: transparent; border: none; color: {c['title']};")

        action_btn_css = f"""
            QPushButton {{
                background: {c['btn_overlay']};
                color: {c['title']};
                border: 1px solid {c['border']};
                border-radius: 6px;
                padding: 5px 14px;
            }}
            QPushButton:hover {{ background: {c['btn_overlay_hover']}; }}
            QPushButton:checked {{ background: {c['btn_overlay_checked']}; }}
            QPushButton:pressed {{ background: {c['btn_overlay']}; }}
        """
        self._btn_pin.setStyleSheet(action_btn_css)
        self._btn_done.setStyleSheet(action_btn_css)
        if self._btn_del is not None:
            self._btn_del.setStyleSheet(action_btn_css)

        for btn, color in zip(self._color_btns, NOTE_COLORS):
            swatch = color or self._pal.get("node_bg", "#1a2a1e")
            self._style_swatch(btn, swatch, color == self._note.color, is_default=not color)

    @staticmethod
    def _style_swatch(btn: QToolButton, color: str, selected: bool, is_default: bool = False) -> None:
        ring = "#ffffff" if selected else "rgba(255,255,255,70)"
        width = 3 if selected else 1
        check = "✓" if selected else ("Ø" if is_default else "")
        # A checkmark on the selected swatch (rather than just a ring) is
        # a much clearer "this one's active" signal at a glance; the
        # "no color" swatch gets a permanent Ø so it never looks like an
        # oddly plain dark color choice sitting among the bright ones.
        btn.setText(check)
        btn.setStyleSheet(
            f"QToolButton {{ background: {color}; border-radius: 12px; "
            f"border: {width}px solid {ring}; color: #ffffff; font-weight: bold; "
            f"font-size: 11px; }}"
            f"QToolButton:hover {{ border: 3px solid #ffffff; }}")

    # ── Formatting ───────────────────────────────────────────────────────
    def _merge_format(self, fmt: QTextCharFormat) -> None:
        cursor = self._editor.textCursor()
        cursor.mergeCharFormat(fmt)
        self._editor.mergeCurrentCharFormat(fmt)

    def _toggle_bold(self, checked: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if checked else QFont.Weight.Normal)
        self._merge_format(fmt)

    def _toggle_italic(self, checked: bool) -> None:
        fmt = QTextCharFormat(); fmt.setFontItalic(checked)
        self._merge_format(fmt)

    def _toggle_underline(self, checked: bool) -> None:
        fmt = QTextCharFormat(); fmt.setFontUnderline(checked)
        self._merge_format(fmt)

    def _toggle_heading(self, checked: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontPointSize(15 if checked else 10)
        fmt.setFontWeight(QFont.Weight.Bold if checked else QFont.Weight.Normal)
        self._merge_format(fmt)

    def _toggle_bullets(self) -> None:
        self._toggle_list(QTextListFormat.Style.ListDisc)

    def _toggle_numbers(self) -> None:
        self._toggle_list(QTextListFormat.Style.ListDecimal)

    def _toggle_list(self, style: QTextListFormat.Style) -> None:
        cursor = self._editor.textCursor()
        lst = cursor.currentList()
        if lst and lst.format().style() == style:
            lst.remove(cursor.block())
        else:
            fmt = QTextListFormat(); fmt.setStyle(style)
            cursor.createList(fmt)

    def _sync_toolbar_state(self) -> None:
        fmt = self._editor.currentCharFormat()
        self._btn_bold.blockSignals(True); self._btn_bold.setChecked(fmt.fontWeight() == QFont.Weight.Bold); self._btn_bold.blockSignals(False)
        self._btn_italic.blockSignals(True); self._btn_italic.setChecked(fmt.fontItalic()); self._btn_italic.blockSignals(False)
        self._btn_under.blockSignals(True); self._btn_under.setChecked(fmt.fontUnderline()); self._btn_under.blockSignals(False)
        self._btn_heading.blockSignals(True); self._btn_heading.setChecked(fmt.fontPointSize() >= 14); self._btn_heading.blockSignals(False)

    def _pick_color(self, color: str, btn: QToolButton) -> None:
        self._note.color = color
        for b in self._color_btns:
            if b is not btn:
                b.setChecked(False)
        self._apply_note_color()

    def _toggle_pin(self, checked: bool) -> None:
        self._note.pinned = checked
        self._btn_pin.setText("📌  Pinned" if checked else "📌  Pin")

    # ── Save / delete ────────────────────────────────────────────────────
    def _done(self) -> None:
        self._note.title = self._title.text().strip()
        self._note.html = self._editor.toHtml()
        self.accept()

    def _delete(self) -> None:
        reply = QMessageBox.question(
            self, "Delete Note", "Permanently delete this note?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Yes:
            self._deleted = True
            self.accept()

    def reject(self) -> None:
        # Escape / window-X: still save rather than discard — see class docstring.
        self._done()

    @property
    def deleted(self) -> bool:
        return self._deleted

    def result_note(self) -> Note:
        return self._note

    def is_blank(self) -> bool:
        """True if this note has no title and no real body text/images —
        used by NotesGridView to avoid littering the board with empty
        notes left over from opening ➕ New Note and immediately closing it."""
        has_image = bool(_DATA_IMG_RE.search(self._note.html or ""))
        return (not self._note.title.strip()
                and not notes_store.plain_text(self._note.html).strip()
                and not has_image)


# ── Note preview card ────────────────────────────────────────────────────────

class NoteCard(QFrame):
    clicked          = pyqtSignal(str)   # note id
    delete_requested = pyqtSignal(str)   # note id

    def __init__(self, note: Note, palette: dict, parent=None):
        super().__init__(parent)
        self._note = note
        self._pal  = palette
        self._colors = note_text_colors(note.color, palette)
        # Signature of everything that affects this card's appearance, and the
        # hover state currently painted.  Both let set_note()/_apply_style()
        # return immediately when nothing actually changed — the grid calls
        # set_note() for every visible card on every rebuild.
        self._sig = None
        self._style_hovered = None
        self._style_sig = None
        self._shadow_state = None
        self._img_lbl = None
        self.setObjectName("note_card")
        self.setFixedWidth(CARD_W)
        self.setMinimumHeight(CARD_MIN_H)   # floor, not a ceiling — see MiniPlantCard
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptDrops(False)
        self._build()
        self.set_note(note, palette, pal_gen=0)
        self._apply_style(False)
        self._del_btn.setVisible(False)

    def set_note(self, note: Note, palette: dict, pal_gen: int = 0) -> None:
        """Point this card at `note`, re-rendering only when something that
        changes its appearance actually changed.

        Cards are cached and reused across rebuilds (see
        NotesGridView._rebuild_grid), so this is the normal path — a refresh
        that doesn't alter a given note costs one tuple comparison.
        """
        sig = (note.id, note.modified_at, note.color, note.pinned, pal_gen)
        if sig == self._sig:
            return
        self._sig    = sig
        self._note   = note
        self._pal    = palette
        self._colors = note_text_colors(note.color, palette)
        self._populate()
        # The frame's colours live in the stylesheet written by _apply_style(),
        # so a reused card whose note changed colour needs that rewritten.
        # Re-applying at the *current* hover state refreshes the stylesheet
        # (the signature changed) while leaving the shadow animation alone.
        self._apply_style(bool(self._style_hovered))

    def showEvent(self, event) -> None:
        # Qt's layout activation on first show can override a
        # constructor-time setVisible(False) on a freshly-added layout
        # item, so the hidden-until-hover state is re-asserted here too —
        # belt and braces against that timing quirk.
        super().showEvent(event)
        if not self.underMouse():
            self._del_btn.setVisible(False)

    def _build(self) -> None:
        """Create the card's widget skeleton — once, for the card's lifetime.

        Every part of a note card is created here and then shown, hidden or
        re-texted by _populate().  The grid used to destroy and rebuild every
        card on each refresh, search keystroke and resize, which meant a fresh
        QGraphicsDropShadowEffect, QPropertyAnimation and ~15 child widgets
        per note, per rebuild.
        """
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)

        self._text_w = QWidget()
        self._text_w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        tl = QVBoxLayout(self._text_w)
        tl.setContentsMargins(14, 12, 14, 6)
        tl.setSpacing(5)

        self._pin_row = QWidget()
        self._pin_row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        pin_row = QHBoxLayout(self._pin_row)
        pin_row.setContentsMargins(0, 0, 0, 0)
        pin_lbl = QLabel("📌")
        pin_lbl.setStyleSheet("background: transparent; font-size: 12px;")
        pin_row.addStretch()
        pin_row.addWidget(pin_lbl)
        tl.addWidget(self._pin_row)

        self._title_lbl = QLabel()
        self._title_lbl.setWordWrap(True)
        f = QFont(); f.setBold(True); f.setPointSize(11)
        self._title_lbl.setFont(f)
        tl.addWidget(self._title_lbl)

        self._body_lbl = QLabel()
        self._body_lbl.setWordWrap(True)
        body_f = QFont(); body_f.setPointSize(9)
        self._body_lbl.setFont(body_f)
        tl.addWidget(self._body_lbl)

        self._empty_lbl = QLabel("Empty note")
        self._empty_lbl.setStyleSheet("font-style: italic; background: transparent;")
        tl.addWidget(self._empty_lbl)

        tl.addStretch()
        self._lay.addWidget(self._text_w)

        # Delete button lives outside the click-through text area so it
        # still receives its own mouse events. Hidden until the card is
        # hovered (see enterEvent/leaveEvent) so the grid's default look
        # isn't cluttered with a trash icon on every single card —
        # RetainSizeWhenHidden keeps its layout space reserved so nothing
        # shifts by a pixel when it fades in/out.
        del_row = QHBoxLayout()
        del_row.setContentsMargins(8, 0, 8, 8)
        del_row.addStretch()
        del_btn = QPushButton("🗑")
        del_btn.setFixedSize(24, 24)
        del_btn.setToolTip("Delete note")
        del_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        sp = del_btn.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        del_btn.setSizePolicy(sp)
        self._del_btn = del_btn
        del_row.addWidget(del_btn)
        self._lay.addLayout(del_row)

        # Soft elevation shadow, lifting a little further on hover — the
        # same "hovered card feels physically closer" language the plant
        # cards use, just via a real QGraphicsDropShadowEffect since this
        # grid is a modest, static scroll view rather than a zoomable
        # canvas with hundreds of items repainting every frame.
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setColor(QColor(0, 0, 0, 110))
        self._shadow.setBlurRadius(14)
        self._shadow.setOffset(0, 3)
        self.setGraphicsEffect(self._shadow)
        self._shadow_anim = QPropertyAnimation(self._shadow, b"blurRadius", self)
        self._shadow_anim.setDuration(140)
        self._del_btn.clicked.connect(
            lambda: self.delete_requested.emit(self._note.id))

    def _populate(self) -> None:
        """Fill the skeleton from self._note.  Called only when the card's
        signature actually changes — see set_note()."""
        note = self._note
        c = self._colors

        # ── Optional preview image (decoded once per note revision) ────────
        pix = None
        m = _DATA_IMG_RE.search(note.html or "")
        if m:
            key = (note.id, note.modified_at)
            pix = _preview_cache.get(key)
            if pix is None:
                try:
                    raw = base64.b64decode(m.group(1))
                    img = QImage.fromData(raw)
                    if not img.isNull():
                        pix = QPixmap.fromImage(img).scaled(
                            CARD_W, IMG_PREVIEW_H,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
                        if pix.width() > CARD_W:
                            x = (pix.width() - CARD_W) // 2
                            pix = pix.copy(x, 0, CARD_W, pix.height())
                        pix = _rounded_top_pixmap(pix, CARD_RADIUS)
                        _preview_cache[key] = pix
                    else:
                        pix = None
                except Exception:
                    pix = None
        if pix is not None:
            if self._img_lbl is None:
                self._img_lbl = QLabel()
                self._img_lbl.setStyleSheet("background: transparent;")
                self._img_lbl.setFixedSize(CARD_W, IMG_PREVIEW_H)
                self._lay.insertWidget(0, self._img_lbl)
            self._img_lbl.setPixmap(pix)
            self._img_lbl.setVisible(True)
        elif self._img_lbl is not None:
            self._img_lbl.setVisible(False)

        # ── Pin badge ─────────────────────────────────────────────────────
        self._pin_row.setVisible(bool(note.pinned))

        # ── Title / body / empty placeholder ──────────────────────────────
        # Text is only assigned when it differs: setText() on a word-wrapped
        # QLabel invalidates its layout, which is the expensive part.
        title = note.title.strip()
        if title:
            text = self._cap(note.title, 120)
            if self._title_lbl.text() != text:
                self._title_lbl.setText(text)
            self._title_lbl.setStyleSheet(
                f"color: {c['title']}; background: transparent;")
            self._title_lbl.setVisible(True)
        else:
            self._title_lbl.setVisible(False)

        preview = notes_store.plain_text_for(note)
        if preview:
            text = self._cap(preview, 220)
            if self._body_lbl.text() != text:
                self._body_lbl.setText(text)
            self._body_lbl.setStyleSheet(
                f"color: {c['body']}; background: transparent;")
            self._body_lbl.setVisible(True)
            self._empty_lbl.setVisible(False)
        else:
            self._body_lbl.setVisible(False)
            self._empty_lbl.setStyleSheet(
                f"color: {c['body']}; "
                f"font-style: italic; background: transparent;")
            self._empty_lbl.setVisible(not title)

        # The delete button's hover tint depends on the note color, so it
        # follows the same signature.
        self._del_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 5px; }"
            f"QPushButton:hover {{ background: {c['hover_overlay']}; }}")

    @staticmethod
    def _cap(text: str, n: int) -> str:
        text = text.strip()
        return text if len(text) <= n else text[:n].rstrip() + "…"

    def _apply_style(self, hovered: bool) -> None:
        hovered = bool(hovered)
        # The stylesheet and the shadow animation are tracked separately: a
        # colour-only change (set_note resets _style_hovered) must restyle the
        # card without restarting a 140 ms shadow animation that would repaint
        # it every frame to no visible effect.
        if hovered != self._style_hovered or self._sig != self._style_sig:
            self._style_hovered = hovered
            self._style_sig = self._sig
            border = self._colors["border_hover"] if hovered else self._colors["border"]
            self.setStyleSheet(
                f"#note_card {{ background: {self._colors['bg']}; border: 1px solid {border}; "
                f"border-radius: {CARD_RADIUS}px; }}")
        if hovered != self._shadow_state:
            self._shadow_state = hovered
            self._shadow_anim.stop()
            self._shadow_anim.setStartValue(self._shadow.blurRadius())
            self._shadow_anim.setEndValue(26 if hovered else 14)
            self._shadow_anim.start()
            self._shadow.setOffset(0, 6 if hovered else 3)

    def enterEvent(self, event) -> None:
        self._apply_style(True)
        self._del_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._apply_style(False)
        self._del_btn.setVisible(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._note.id)
        super().mousePressEvent(event)


class _NotesCanvas(QWidget):
    """Plain container that holds the note-card grid. Its only
    non-standard behavior is emitting a signal on double-click, so
    double-clicking empty space (anywhere that isn't already a card)
    starts a new note — mirrors Google Keep, where clicking empty board
    space opens the composer directly rather than requiring the ＋
    button."""
    double_clicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class _EmptyStateLabel(QLabel):
    """The "No notes yet" placeholder — double-clickable too, same as
    the canvas behind it, so the very first note doesn't require hunting
    for a button."""
    double_clicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


# ── Notes grid (main view) ───────────────────────────────────────────────────

class NotesGridView(QWidget):
    """Top-level Notes view — a peer of the lineage canvas and the plant
    grid/spreadsheet, shown in MainWindow's central QStackedWidget."""

    switch_to_lineage_view = pyqtSignal()

    def __init__(self, palette: dict, parent=None):
        super().__init__(parent)
        self._pal = palette
        self._all_notes: List[Note] = []
        self._dirty = True
        # Cache: note_id -> NoteCard.  Rebuilding used to destroy and recreate
        # every card (each with its own drop-shadow effect, shadow animation
        # and ~15 child widgets) on every refresh, search keystroke and resize.
        self._card_cache: Dict[str, NoteCard] = {}
        # Signature of the last rendered grid — see _rebuild_grid().
        self._last_sig = None
        # Bumped whenever the palette changes, so cached cards know to restyle.
        self._pal_gen = 0
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._rebuild_grid)
        self._build()

    def set_palette(self, palette: dict) -> None:
        self._pal = palette
        self._pal_gen += 1
        self._apply_palette_styles()
        if self.isVisible():
            self._rebuild_grid()
        else:
            self.mark_dirty()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._back_btn = QPushButton("↩  Back to Lineage View")
        self._back_btn.clicked.connect(self.switch_to_lineage_view.emit)
        header.addWidget(self._back_btn)

        header.addSpacing(8)
        self._title_lbl = QLabel("📝 Notes")
        tf = QFont(); tf.setPointSize(14); tf.setBold(True)
        self._title_lbl.setFont(tf)
        header.addWidget(self._title_lbl)
        self._count_lbl = QLabel("")
        header.addWidget(self._count_lbl)

        header.addStretch()
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Search notes…")
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(lambda: self._filter_timer.start(150))
        header.addWidget(self._search)
        btn_new = QPushButton("➕  New Note")
        btn_new.setDefault(True)
        btn_new.clicked.connect(self._new_note)
        header.addWidget(btn_new)
        lay.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._inner = _NotesCanvas()
        self._inner.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._inner.double_clicked.connect(self._new_note)
        self._grid_layout = QGridLayout(self._inner)
        self._grid_layout.setContentsMargins(2, 2, 2, 20)
        self._grid_layout.setSpacing(PAD)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._inner)
        lay.addWidget(self._scroll, 1)

        self._empty_lbl = _EmptyStateLabel(
            "🗒️\n\nNo notes yet\nDouble-click anywhere, or click ＋ New Note, "
            "to start your breeder's log.")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._empty_lbl.double_clicked.connect(self._new_note)
        self._apply_palette_styles()
        self._empty_lbl.setVisible(False)
        lay.addWidget(self._empty_lbl)

        # No eager refresh() here: this view is constructed during MainWindow
        # startup but starts hidden, and building every note card up front was
        # costing ~0.3 s of startup for a board the user may never open.
        # _dirty is already True, and showEvent() refreshes on first show.
        self.mark_dirty()

    def _apply_palette_styles(self) -> None:
        muted = self._pal.get('text_muted', '#90b898')
        self._title_lbl.setStyleSheet(
            f"color: {self._pal.get('text', '#e8f5e8')}; background: transparent;")
        self._count_lbl.setStyleSheet(f"color: {muted}; background: transparent;")
        self._empty_lbl.setStyleSheet(f"""
            color: {muted};
            padding: 48px;
            font-size: 13px;
            line-height: 1.6;
        """)

    # ── Refresh ──────────────────────────────────────────────────────────
    def mark_dirty(self) -> None:
        self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()

    def refresh(self) -> None:
        self._all_notes = notes_store.get_all()
        self._dirty = False
        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        query = self._search.text().strip()
        notes = notes_store.search(query) if query else self._all_notes

        total = len(self._all_notes)
        if query:
            self._count_lbl.setText(f"({len(notes)} of {total} found)")
        else:
            self._count_lbl.setText(f"({total})" if total else "")

        show_empty = (len(notes) == 0 and not query)
        self._empty_lbl.setVisible(show_empty)
        self._scroll.setVisible(not show_empty)

        if not notes:
            # No results: unparent the cards from the layout but keep them
            # cached (hidden), so clearing the search box doesn't have to
            # rebuild the whole board.
            if self._last_sig is not None:
                self._hide_all_cards()
                self._last_sig = None
            return

        avail_w = self._scroll.viewport().width() or self.width()
        n_cols = max(1, avail_w // (CARD_W + PAD))

        # Nothing to do if the same notes are being shown the same way — this
        # is what makes a resize that doesn't change the column count (and a
        # search keystroke that doesn't change the result set) free, instead
        # of a full teardown and rebuild of the board.
        sig = (query, n_cols, self._pal_gen,
               tuple((n.id, n.modified_at) for n in notes))
        if sig == self._last_sig:
            return
        self._last_sig = sig

        # Evict only cards whose note no longer exists at all.  A card for a
        # note that is merely filtered out is kept (hidden) instead: search is
        # applied on every keystroke, and destroying + rebuilding the board on
        # each one is exactly the churn this cache exists to avoid.  The cache
        # stays bounded by the total note count.
        live_ids = {n.id for n in self._all_notes}
        for nid in [nid for nid in self._card_cache if nid not in live_ids]:
            self._card_cache.pop(nid).deleteLater()

        # Cards not being shown must be hidden explicitly: taking them out of
        # the layout leaves them parented to the canvas at their old geometry,
        # so without this a filtered-out card would still be drawn.
        shown_ids = {n.id for n in notes}
        for nid, card in self._card_cache.items():
            if nid not in shown_ids:
                card.setVisible(False)

        self._inner.setUpdatesEnabled(False)
        try:
            # Clear layout items only — the widgets live on in the cache.
            while self._grid_layout.count():
                self._grid_layout.takeAt(0)

            row = col = 0
            for note in notes:
                card = self._card_cache.get(note.id)
                if card is None:
                    card = NoteCard(note, self._pal, parent=self._inner)
                    card.clicked.connect(self._open_note)
                    card.delete_requested.connect(self._delete_note)
                    self._card_cache[note.id] = card
                else:
                    card.set_note(note, self._pal, self._pal_gen)
                card.setVisible(True)
                self._grid_layout.addWidget(card, row, col)
                col += 1
                if col >= n_cols:
                    col = 0; row += 1
        finally:
            self._inner.setUpdatesEnabled(True)

    def _hide_all_cards(self) -> None:
        """Unparent every card from the layout and hide it, keeping the
        widgets cached for the next rebuild."""
        while self._grid_layout.count():
            self._grid_layout.takeAt(0)
        for card in self._card_cache.values():
            card.setVisible(False)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._filter_timer.start(80)

    # ── Note CRUD ────────────────────────────────────────────────────────
    def _new_note(self) -> None:
        note = Note()
        dlg = NoteEditorDialog(note, self._pal, parent=self.window(), is_new=True)
        if dlg.exec() == QDialog.DialogCode.Accepted and not dlg.is_blank():
            notes_store.upsert(dlg.result_note())
            self.refresh()

    def _open_note(self, note_id: str) -> None:
        note = notes_store.get(note_id)
        if note is None:
            return
        dlg = NoteEditorDialog(note, self._pal, parent=self.window())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.deleted or dlg.is_blank():
                notes_store.delete(note_id)
            else:
                notes_store.upsert(dlg.result_note())
            self.refresh()

    def _delete_note(self, note_id: str) -> None:
        note = notes_store.get(note_id)
        title = note.title.strip() if note else ""
        prompt = f'Permanently delete "{title}"?' if title else "Permanently delete this note?"
        reply = QMessageBox.question(
            self, "Delete Note", prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Yes:
            notes_store.delete(note_id)
            self.refresh()
