"""
ui_common.py — Shared, lightweight UI polish helpers used across the app.

Three small pieces of infrastructure live here so every dialog and view can
opt in without duplicating code:

  • FadeDialog        — QDialog base class that fades in on first show instead
                         of snapping into existence.
  • flash_status()     — non-blocking success/info notification. Prefers the
                         owning window's status bar (matches the existing
                         "Autosaved ✓" pattern); falls back to a small
                         self-dismissing overlay label when no status bar is
                         reachable (e.g. from a standalone modal dialog).
  • mark_field_invalid() / clear_field_invalid()
                        — inline validation styling for QLineEdit/QComboBox/etc,
                          replacing blocking QMessageBox popups for routine
                          "this field is required" feedback.

None of this changes behaviour for destructive confirmations or genuine
errors — QMessageBox.question / .critical are left exactly as they were.
"""
from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QWidget, QLabel, QMainWindow, QGraphicsOpacityEffect,
)
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor


# ── Fade-in dialogs ────────────────────────────────────────────────────────

class FadeDialog(QDialog):
    """Drop-in QDialog replacement that fades in (~140ms) the first time it's
    shown. Purely cosmetic — no behavioural change — so existing subclasses
    can switch their base class with no other edits."""

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_faded_in", False):
            return
        self._faded_in = True
        try:
            effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(140)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            # Qt drops the effect once opacity settles back to fully-opaque;
            # keep a reference so the animation itself isn't garbage collected
            # mid-flight, and clear the effect afterward (opacity effects on a
            # dialog that stay resident are a small but pointless paint cost).
            self._fade_anim = anim

            def _cleanup():
                self.setGraphicsEffect(None)
            anim.finished.connect(_cleanup)
            anim.start()
        except Exception:
            # Never let a cosmetic effect block the dialog from showing.
            pass


# ── Toast / status notifications ──────────────────────────────────────────

def flash_status(widget: QWidget, message: str, msec: int = 2500) -> None:
    """Show a transient, non-blocking notification near `widget`.

    Walks up the parent chain looking for a QMainWindow with a status bar
    (this is how the app already reports "Autosaved ✓" / "Saved ✓") so that
    routine confirmations feel consistent whether they come from the main
    window or a dialog launched from it. Falls back to a small floating,
    self-dismissing label anchored to `widget` when no status bar is
    reachable — e.g. a standalone modal dialog with its own success message.
    """
    w = widget
    while w is not None:
        if isinstance(w, QMainWindow):
            bar = w.statusBar()
            if bar is not None:
                bar.showMessage(message, msec)
                return
        w = w.parentWidget() if hasattr(w, "parentWidget") else None
    _show_inline_toast(widget, message, msec)


def _show_inline_toast(widget: QWidget, message: str, msec: int) -> None:
    top = widget.window() if widget is not None else None
    host = top or widget
    if host is None:
        return

    lbl = QLabel(message, host)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        "QLabel {"
        " background: rgba(28, 30, 28, 235);"
        " color: #eafaf0;"
        " padding: 9px 16px;"
        " border-radius: 9px;"
        " font-size: 12px;"
        " border: 1px solid rgba(255,255,255,30);"
        "}"
    )
    lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    max_w = max(120, host.width() - 48)
    lbl.setMaximumWidth(max_w)
    lbl.adjustSize()

    x = (host.width() - lbl.width()) // 2
    y = host.height() - lbl.height() - 28
    lbl.move(max(8, x), max(8, y))

    effect = QGraphicsOpacityEffect(lbl)
    lbl.setGraphicsEffect(effect)
    lbl.show()
    lbl.raise_()

    anim_in = QPropertyAnimation(effect, b"opacity", lbl)
    anim_in.setDuration(130)
    anim_in.setStartValue(0.0)
    anim_in.setEndValue(1.0)
    anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)
    lbl._toast_anim_in = anim_in  # keep alive
    anim_in.start()

    def _fade_out():
        if lbl is None:
            return
        try:
            anim_out = QPropertyAnimation(effect, b"opacity", lbl)
            anim_out.setDuration(220)
            anim_out.setStartValue(1.0)
            anim_out.setEndValue(0.0)
            anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
            anim_out.finished.connect(lbl.deleteLater)
            lbl._toast_anim_out = anim_out
            anim_out.start()
        except RuntimeError:
            pass  # widget already gone

    QTimer.singleShot(msec, _fade_out)


# ── Inline field validation ────────────────────────────────────────────────

_INVALID_STYLE_SUFFIX = "border: 1.5px solid #e0555a;"


def mark_field_invalid(field: QWidget, message: str,
                        caption: Optional[QLabel] = None) -> None:
    """Give a field a red outline and, if a caption label is supplied, show
    a short inline message under it — instead of interrupting the user with
    a QMessageBox for routine 'this field is required' validation."""
    base = field.property("_base_stylesheet")
    if base is None:
        base = field.styleSheet() or ""
        field.setProperty("_base_stylesheet", base)
    field.setStyleSheet(base + " " + _INVALID_STYLE_SUFFIX)
    if caption is not None:
        caption.setText(message)
        caption.setStyleSheet("color: #e0555a; font-size: 11px;")
        caption.setVisible(True)
    field.setFocus()


def clear_field_invalid(field: QWidget, caption: Optional[QLabel] = None) -> None:
    base = field.property("_base_stylesheet")
    field.setStyleSheet(base if base is not None else "")
    if caption is not None:
        caption.setVisible(False)
