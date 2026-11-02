"""
canvas_view.py — Scene and view.

Changes:
  • Redo removed
  • export_to_image(): no longer calls auto_layout or fit_view — exports the
    full scene bounds without touching the live view state at all.
    Rendering math is straightforward: image pixels = scene units × scale,
    no setDevicePixelRatio confusion.
  • Top port now in shape() so it correctly receives mouse events.
  • connect direction: "top" = drag from child looking for parent,
    "bottom" = drag from parent looking for child — no confirmation popup.
"""
from __future__ import annotations
import copy
import logging
import math
from typing import Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsItem, QGraphicsPathItem,
    QRubberBand, QMenu, QWidget, QHBoxLayout, QPushButton, QLabel, QLineEdit,
)
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPainterPath, QPainterPathStroker,
    QTransform, QFont, QImage, QPixmap, QTextOption, QPolygonF,
)
from PyQt6.QtCore import (QTimer,
    Qt, QRectF, QPointF, QRect, QSize, QSizeF,
    pyqtSignal, pyqtSlot, QRunnable, QThreadPool, QObject,
)
try:
    from PyQt6.QtSvg import QSvgGenerator
    _SVG_AVAILABLE = True
except ImportError:
    _SVG_AVAILABLE = False

# QPdfWriter lives in PyQt6.QtGui (always available) — no QtPrintSupport needed.

from models import LineageTree, PlantNode, CommentNote
from node_item import PlantNodeItem, NodeSignals, CARD_W, TEXT_PAD, IMG_SCALE, IMG_H
from settings import THEMES, AppSettings
import perf

log = logging.getLogger(__name__)
_DARK = THEMES["dark"]

HANDLE_SIZE = 14


# ── Async image decoder ───────────────────────────────────────────────────────
# QImage is thread-safe (reentrant); only QPixmap must be on the main thread.
# We decode base64 → QImage → scale entirely in a worker thread, then emit the
# pre-scaled QImage back to the main thread where QPixmap.fromImage() is called.
# This keeps the UI fully responsive while loading large lineages.

class _DecodeSignals(QObject):
    """Carrier for the cross-thread done signal (QRunnable cannot have signals)."""
    done = pyqtSignal(str, object, object)   # (node_id, QImage | None, token)


class _ImgDecodeTask(QRunnable):
    """Decodes one plant-card image entirely off the main thread."""

    def __init__(self, node_id: str, b64: str, target_w: int, target_h: int, token=None):
        super().__init__()
        self._token     = token
        self.signals    = _DecodeSignals()
        self._node_id   = node_id
        self._b64       = b64
        self._target_w  = target_w
        self._target_h  = target_h
        self.setAutoDelete(True)

    def run(self) -> None:
        import base64 as _b64mod
        result = None
        try:
            raw = _b64mod.b64decode(self._b64)
            img = QImage()
            img.loadFromData(raw)
            if not img.isNull():
                result = img.scaled(
                    self._target_w, self._target_h,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
        except Exception as exc:
            log.warning("_ImgDecodeTask %s: %s", self._node_id, exc)
        self.signals.done.emit(self._node_id, result, self._token)


class _NoOpWarmupTask(QRunnable):
    """Does nothing — exists purely to force a worker OS thread to spin up."""
    def run(self) -> None:
        pass


def warmup_decode_threadpool() -> None:
    """Pre-spin the global QThreadPool's worker threads at app startup.

    QThreadPool lazily creates each worker OS thread the first time it's
    actually needed, and thread creation has measurable overhead (a few ms
    each — more on Windows). Left alone, that cost lands entirely on
    whichever _enqueue_visible_decodes() call happens to be first — i.e.
    the very first lineage the user opens — which is exactly the timing
    pattern seen in profiling (early scene.enqueue_decodes calls taking
    ~20ms while later ones on an already-warm pool take a fraction of a
    ms). Call this once at app startup, before any lineage is loaded, so
    that cost is paid during the invisible startup window instead.
    """
    pool = QThreadPool.globalInstance()
    for _ in range(max(1, pool.maxThreadCount())):
        pool.start(_NoOpWarmupTask())


# ── Edge ─────────────────────────────────────────────────────────────────────

class EdgeItem(QGraphicsPathItem):
    Z_EDGE = -2

    def __init__(self, parent_item: PlantNodeItem, child_item: PlantNodeItem,
                 color: str = "#4a7a58"):
        super().__init__()
        self.parent_item = parent_item
        self.child_item  = child_item
        self._color = color
        self._update_pen()
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setZValue(self.Z_EDGE)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        # Wider invisible hit area so edge is easy to right-click
        self.setAcceptHoverEvents(True)
        self._hovered = False
        self.update_path()

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True; self._update_pen(); self.update()

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False; self._update_pen(); self.update()

    def _update_pen(self) -> None:
        hov = getattr(self, "_hovered", False)
        col = QColor(self._color)
        if hov:
            col = col.lighter(150)
        pen = QPen(col, 3.5 if hov else 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(pen)

    def update_path(self) -> None:
        p1 = self.parent_item.bottom_port_scene()
        p2 = self.child_item.top_port_scene()
        # Cache endpoint positions so paint() can reuse them without calling
        # mapToScene() again — mapToScene() traverses the full item parent chain.
        self._cached_p1 = p1
        self._cached_p2 = p2
        dy = max(40.0, abs(p2.y() - p1.y()) * 0.5)
        path = QPainterPath(p1)
        path.cubicTo(QPointF(p1.x(), p1.y()+dy),
                     QPointF(p2.x(), p2.y()-dy), p2)
        self.setPath(path)

    def set_color(self, color: str) -> None:
        self._color = color; self._update_pen(); self.update()

    def shape(self) -> "QPainterPath":
        """Widen the hit area to 12px so edges are easy to right-click."""
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        return stroker.createStroke(self.path())

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        super().paint(painter, option, widget)
        # Reuse cached endpoints — avoids two mapToScene() calls per edge per frame.
        p2  = getattr(self, "_cached_p2", None) or self.child_item.top_port_scene()
        p1b = getattr(self, "_cached_p1", None) or self.parent_item.bottom_port_scene()
        dy    = max(40.0, abs(p2.y() - p1b.y()) * 0.5)
        ctrl  = QPointF(p2.x(), p2.y() - dy)
        angle = math.atan2(p2.y()-ctrl.y(), p2.x()-ctrl.x())
        aw, ah = 8, 5
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(QColor(self._color)))
        arr = QPainterPath()
        arr.moveTo(p2)
        arr.lineTo(p2.x()-aw*math.cos(angle)+ah*math.sin(angle),
                   p2.y()-aw*math.sin(angle)-ah*math.cos(angle))
        arr.lineTo(p2.x()-aw*math.cos(angle)-ah*math.sin(angle),
                   p2.y()-aw*math.sin(angle)+ah*math.cos(angle))
        arr.closeSubpath()
        painter.drawPath(arr)


# ── Comment (resizable sticky note) ──────────────────────────────────────────

class CommentItem(QGraphicsItem):
    Z_COMMENT = 5
    MIN_W, MIN_H = 100, 60

    def __init__(self, note: CommentNote, palette: dict, font_family: str = "Segoe UI"):
        super().__init__()
        self._note      = note
        self._pal       = palette
        self._ff        = font_family
        self._resizing  = False
        self._resize_start_pos = QPointF()
        self._resize_start_wh  = (note.w, note.h)

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,            True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable,         True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(self.Z_COMMENT)
        self.setPos(note.x, note.y)

    @property
    def note(self) -> CommentNote: return self._note

    def _handle_rect(self) -> QRectF:
        return QRectF(self._note.w - HANDLE_SIZE, self._note.h - HANDLE_SIZE,
                      HANDLE_SIZE, HANDLE_SIZE)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._note.w + 2, self._note.h + 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        try:
            p    = self._pal
            rect = QRectF(0, 0, self._note.w, self._note.h)
            col  = self._note.color or p.get("year_fg", "#c8963c")
            painter.setBrush(QBrush(QColor(p.get("comment_bg", "#2a2000"))))
            painter.setPen(QPen(QColor(p.get("comment_border", col)), 1.5))
            painter.drawRoundedRect(rect, 6, 6)
            if self.isSelected():
                painter.setPen(QPen(QColor(col), 2))
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawRoundedRect(rect.adjusted(-1,-1,1,1), 7, 7)
            painter.setPen(QPen(QColor(p.get("comment_text", col))))
            painter.setFont(QFont(self._ff, 9))
            text_opt = QTextOption()
            text_opt.setWrapMode(QTextOption.WrapMode.WordWrap)
            text_opt.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            painter.drawText(rect.adjusted(8, 6, -8, -6),
                             self._note.text or "Double-click to edit…",
                             text_opt)
            h   = self._handle_rect()
            pts = [QPointF(h.right(), h.top()+4),
                   QPointF(h.right(), h.bottom()),
                   QPointF(h.left()+4, h.bottom())]
            hp = QPainterPath()
            hp.moveTo(pts[0]); hp.lineTo(pts[1]); hp.lineTo(pts[2])
            hp.closeSubpath()
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.setBrush(QBrush(QColor(col).lighter(130)))
            painter.drawPath(hp)
        except Exception as e: log.error("CommentItem.paint: %s", e)

    def hoverMoveEvent(self, event):
        if self._handle_rect().contains(event.pos()):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._handle_rect().contains(event.pos()):
                self._resizing = True
                self._resize_start_pos = event.scenePos()
                self._resize_start_wh  = (self._note.w, self._note.h)
                event.accept(); return
        self._resizing = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.scenePos() - self._resize_start_pos
            self.prepareGeometryChange()
            self._note.w = max(self.MIN_W, self._resize_start_wh[0] + delta.x())
            self._note.h = max(self.MIN_H, self._resize_start_wh[1] + delta.y())
            self.update()
            sc = self.scene()
            if sc: sc.tree_changed.emit()
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._resizing = False; super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._open_editor(); super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu()
        a_edit   = menu.addAction("✏️  Edit Text")
        a_delete = menu.addAction("🗑️  Delete")
        chosen = menu.exec(event.screenPos())
        if chosen == a_edit:    self._open_editor()
        elif chosen == a_delete:
            sc = self.scene()
            if sc: sc.remove_comment(self._note.id)

    def _open_editor(self) -> None:
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getMultiLineText(
            None, "Edit Comment", "Note:", self._note.text)
        if ok:
            self._note.text = text; self.update()
            sc = self.scene()
            if sc: sc.tree_changed.emit()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._note.x = self.pos().x(); self._note.y = self.pos().y()
            sc = self.scene()
            if sc: sc.tree_changed.emit()
        return super().itemChange(change, value)

    def update_palette(self, p: dict, ff: str = "Segoe UI") -> None:
        self._pal = p; self._ff = ff; self.update()


# ── Temp edge ─────────────────────────────────────────────────────────────────

class TempEdgeItem(QGraphicsPathItem):
    def __init__(self, color: str = "#c8963c"):
        super().__init__()
        self.setPen(QPen(QColor(color), 2, Qt.PenStyle.DashLine))
        self.setZValue(100)

    def set_endpoints(self, start: QPointF, end: QPointF) -> None:
        dy = max(40.0, abs(end.y()-start.y())*0.5)
        path = QPainterPath(start)
        path.cubicTo(QPointF(start.x(), start.y()+dy),
                     QPointF(end.x(), end.y()-dy), end)
        self.setPath(path)


# ── Scene ─────────────────────────────────────────────────────────────────────

class LineageScene(QGraphicsScene):

    node_selected           = pyqtSignal(str)
    tree_changed            = pyqtSignal()
    edit_node_requested     = pyqtSignal(str)
    add_parent_requested    = pyqtSignal(str)
    add_offspring_requested = pyqtSignal(str)
    connect_requested       = pyqtSignal(str, str, str)
    connect_mode_changed    = pyqtSignal(str)   # direction: "top"/"bottom"/"" (cancelled)
    search_results_changed  = pyqtSignal(list)  # list of node_ids matching current query
    add_plant_at            = pyqtSignal(float, float)
    add_comment_at          = pyqtSignal(float, float)
    open_plant_db_requested = pyqtSignal()      # open Plant DB dialog (double-click / right-click)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tree: Optional[LineageTree]    = None
        self.filepath: Optional[str] = None   # current .plf path — needed for lineage_images lazy loads
        self._node_items:    Dict[str, PlantNodeItem]  = {}
        self._comment_items: Dict[str, CommentItem]    = {}
        self._edges:         List[EdgeItem]            = []
        self._temp_edge:     Optional[TempEdgeItem]    = None
        self.connect_mode        = False
        self._connect_from_id:  Optional[str] = None
        self._connect_direction: str = "bottom"
        self._connect_dragged:   bool = False
        self.show_images: bool   = True
        self.theme_palette: dict = dict(_DARK)
        self.font_family:   str  = "Segoe UI"
        self.card_font_family: str = "Georgia"
        self.card_font_size:   int  = 10
        self.font_size:     int  = 22
        self.img_scale_factor: float = 0.9
        self.card_style             = None
        self.color_code_lines: bool  = False
        self.mother_line_color: str  = "#e87060"
        self.father_line_color: str  = "#60a0e8"
        self.year_rows: Dict[int, float] = {}
        self.selectionChanged.connect(self._on_selection_changed)
        self._undo_stack: List[dict] = []
        self._clipboard_nodes: List[dict] = []
        self._undoing: bool = False
        # Role map: node_id → "mother"|"father"|"" — rebuilt in _rebuild_edges().
        # Allows O(1) lookup per paint instead of O(n) tree scan.
        self._role_map: Dict[str, str] = {}
        # Deferred tree_changed: set True during node drag, emitted on release
        # so the window title / status bar update fires once per drag, not 60fps.
        self._drag_dirty: bool = False
        # ── Per-node edge index ──────────────────────────────────────────────
        # Maps node_id → list of EdgeItems that touch that node (as parent OR child).
        # Populated by _rebuild_edges().  Lets _on_node_moved() update only the
        # 2-5 edges touching the dragged node instead of scanning all O(n) edges.
        self._node_edge_index: dict[str, list] = {}

    # ── Settings propagation ──────────────────────────────────────────────────

    def apply_settings(self, s: AppSettings, palette: Optional[dict] = None) -> None:
        # Track what changed so we only do expensive work when necessary
        _prev_img_scale   = self.img_scale_factor
        _prev_show_images = self.show_images

        self.font_family      = s.font_family
        self.card_font_family = getattr(s, "card_font_family", "Georgia")
        self.card_font_size   = getattr(s, "card_font_size",   10)
        self.font_size        = s.font_size
        self.img_scale_factor = s.img_scale_factor
        self.card_style       = s.card_style
        self.show_images        = s.show_images
        self.color_code_lines   = getattr(s, "color_code_lines",   False)
        self.mother_line_color  = getattr(s, "mother_line_color",  "#e87060")
        self.father_line_color  = getattr(s, "father_line_color",  "#60a0e8")

        # If a palette was passed in, update it now so the subsequent node
        # cache invalidation and _rebuild_edges() pick up new colours — this
        # means set_theme_palette() is NOT needed as a separate call and the
        # double _rebuild_edges() that caused lag is eliminated.
        if palette is not None:
            self.theme_palette = palette
            for ci in self._comment_items.values():
                ci.update_palette(palette, self.font_family)

        # Only decode and scale pixmaps when the image display settings changed.
        # All other setting changes (font, theme, color-coding) only need a repaint.
        reload_pixmaps = (
            s.img_scale_factor != _prev_img_scale
            or s.show_images != _prev_show_images
        )

        for item in self._node_items.values():
            item.invalidate_caches()   # font, height, palette caches
            if reload_pixmaps:
                # Cancel any in-flight async decode for this item — we're about
                # to do a synchronous reload with the new scale factor.
                item._decode_pending = False
                item.prepareGeometryChange()
                if self.show_images:
                    item._reload_pixmap()
                else:
                    # Free the decoded pixmap outright rather than just
                    # skipping the paint — no reason to keep it resident
                    # while images are hidden. It'll decode again (cheaply,
                    # from .thumb) if re-enabled.
                    item._pixmap = None
                    item._pixmap_dirty = True
            item.update()
        for ci in self._comment_items.values():
            ci.update_palette(self.theme_palette, self.font_family)
        self._rebuild_edges()
        self.update()

    # ── Theme ─────────────────────────────────────────────────────────────────

    def set_theme_palette(self, p: dict) -> None:
        self.theme_palette = p
        # Invalidate per-node palette caches so new colours are picked up on next paint.
        for item in self._node_items.values():
            item._palette_cache = None
        self._rebuild_edges()
        for ci in self._comment_items.values():
            ci.update_palette(p, self.font_family)
        self.update()

    # ── Load ──────────────────────────────────────────────────────────────────

    @perf.timeit("scene.load_tree")
    def load_tree(self, tree: LineageTree) -> None:
        self.connect_mode     = False
        self._connect_from_id = None
        self._node_items.clear()
        self._comment_items.clear()
        self._edges.clear()
        self._node_edge_index.clear()
        self._temp_edge = None
        self.year_rows.clear()

        # ── Freeze the view during bulk item creation ────────────────────────
        # Without this, Qt repaints the scene after EVERY addItem() call,
        # turning an O(n) load into an O(n²) painting operation.
        views = self.views()
        view  = views[0] if views else None
        if view:
            view.setUpdatesEnabled(False)

        # Block our own signals so the main window doesn't receive tree_changed
        # for each of the hundreds of intermediate item additions.
        self.blockSignals(True)
        with perf.timed("scene.clear"):
            self.clear()
        self.blockSignals(False)

        self.tree = tree
        if not self._undoing:
            self._undo_stack.clear()

        with perf.timed("scene.add_nodes"):
            for node in tree.nodes.values():
                self._add_node_item(node)

        with perf.timed("scene.add_comments"):
            for note in tree.comments.values():
                self._add_comment_item(note)

        with perf.timed("scene._rebuild_edges"):
            self._rebuild_edges()

        self._update_year_rows()

        with perf.timed("scene._recalc_scene_rect"):
            self._recalc_scene_rect()

        # Re-enable painting — one single repaint covers everything
        if view:
            view.setUpdatesEnabled(True)
            view.viewport().update()

        # Kick off image decodes, prioritising items inside the viewport
        with perf.timed("scene.enqueue_decodes"):
            self._enqueue_visible_decodes()

    def _recalc_scene_rect(self) -> None:
        # itemsBoundingRect() is a single O(n) call in native Qt C++ code —
        # far faster than Python's [item.mapToScene(item.boundingRect())...] loop.
        r = self.itemsBoundingRect()
        if not r.isEmpty():
            self.setSceneRect(r.adjusted(-200, -160, 200, 160))
        else:
            self.setSceneRect(QRectF(-400, -300, 800, 600))

    def _update_year_rows(self) -> None:
        self._year_rev = getattr(self, "_year_rev", 0) + 1
        self.year_rows.clear()
        if not self.tree: return
        for n in self.tree.nodes.values():
            if n.year and n.year not in self.year_rows:
                self.year_rows[n.year] = n.y

    def _signals(self) -> NodeSignals:
        if not hasattr(self, "_shared_signals"):
            s = NodeSignals()
            s.move_start.connect(self._on_node_move_start)
            s.moved.connect(self._on_node_moved)
            s.edit_requested.connect(self._request_edit)
            s.delete_requested.connect(self._delete_node)
            s.add_parent.connect(self._request_add_parent)
            s.add_offspring.connect(self._request_add_offspring)
            s.image_changed.connect(lambda _: self.tree_changed.emit())
            s.connect_start.connect(self._start_connect)
            self._shared_signals = s
        return self._shared_signals

    # ── Async image decode ────────────────────────────────────────────────────

    def _enqueue_visible_decodes(self) -> None:
        """Submit image decode tasks, giving visible items a higher thread-pool
        priority so the user sees cards rendered before off-screen ones."""
        if not self.show_images:
            return
        views = self.views()
        view  = views[0] if views else None

        if view:
            vr = view.mapToScene(view.viewport().rect()).boundingRect()
            # Generous margin — preload 1 viewport ahead in each direction
            margin_x = vr.width()  * 0.6
            margin_y = vr.height() * 0.6
            near_rect = vr.adjusted(-margin_x, -margin_y, margin_x, margin_y)
        else:
            near_rect = None

        for item in self._node_items.values():
            if not item.node.has_photo:
                continue
            if not item._pixmap_dirty or item._decode_pending:
                continue
            priority = 10 if (near_rect is None or
                              item.sceneBoundingRect().intersects(near_rect)) else 0
            self._enqueue_decode(item, priority=priority)

    def _enqueue_decode(self, item: "PlantNodeItem", priority: int = 0) -> None:
        """Submit a QRunnable to decode item's image off the main thread.
        Higher priority values run sooner in QThreadPool's queue.

        Decodes from node.thumb — a small pre-scaled JPEG — never the
        full-resolution original, which normal card rendering never touches."""
        b64 = item.node.thumb or (item.node.images[0] if item.node.images else None)
        if not b64:
            return
        item._decode_pending = True
        item._decode_gen = getattr(item, "_decode_gen", 0) + 1       # F15: ignore stale results
        token = (id(item), item._decode_gen)
        tw = (CARD_W - TEXT_PAD * 2) * IMG_SCALE
        th = max(40, int(IMG_H * self.img_scale_factor)) * IMG_SCALE
        task = _ImgDecodeTask(item.node.id, b64, tw, th, token)
        task.signals.done.connect(self._on_decode_done)
        QThreadPool.globalInstance().start(task, priority)

    @pyqtSlot(str, object, object)
    def _on_decode_done(self, node_id: str, qimage, token=None) -> None:
        """Receives a pre-scaled QImage from the worker thread.
        QPixmap.fromImage() must run here on the main thread."""
        item = self._node_items.get(node_id)
        if (item is None or not item._decode_pending
                or (token is not None and token != (id(item), getattr(item, "_decode_gen", 0)))):
            # Item was removed, or refresh() already superseded this result.
            return
        item._decode_pending = False
        item._pixmap_dirty   = False
        item._pixmap = QPixmap.fromImage(qimage) if qimage is not None else None
        item.update()

    # ── Items ─────────────────────────────────────────────────────────────────

    def _add_node_item(self, node: PlantNode) -> PlantNodeItem:
        item = PlantNodeItem(node, self._signals())
        self.addItem(item)
        self._node_items[node.id] = item
        # Image decode is NOT submitted here — load_tree() calls
        # _enqueue_visible_decodes() after all items are added so it can
        # prioritise visible cards.  For single-node additions (new plant)
        # we do enqueue immediately.
        if not self._undoing:
            # addItem called from load_tree — batched; defer decode
            # addItem called for a new plant — enqueue now
            pass
        return item

    def _add_node_item_and_decode(self, node: PlantNode) -> PlantNodeItem:
        """Use this path when adding a single new plant (not during full load)."""
        item = self._add_node_item(node)
        if self.show_images:
            self._enqueue_decode(item, priority=10)
        return item

    def _add_comment_item(self, note: CommentNote) -> CommentItem:
        item = CommentItem(note, self.theme_palette, self.font_family)
        self.addItem(item); self._comment_items[note.id] = item
        return item

    # ── Edges ─────────────────────────────────────────────────────────────────

    @perf.timeit("scene._rebuild_edges")
    def _rebuild_edges(self) -> None:
        for e in self._edges:
            self.removeItem(e)
        self._edges.clear()
        self._role_map.clear()
        self._node_edge_index.clear()    # ← reset per-node index
        if not self.tree:
            return
        for cn in self.tree.nodes.values():
            ci = self._node_items.get(cn.id)
            if not ci:
                continue
            for pid in cn.parent_ids:
                pi = self._node_items.get(pid)
                if pi:
                    role = cn.parent_roles.get(pid, "")
                    if role:
                        self._role_map[pid] = role
                    if self.color_code_lines:
                        if role == "mother":
                            edge_col = self.mother_line_color
                        elif role == "father":
                            edge_col = self.father_line_color
                        else:
                            edge_col = pi.node_color()
                    else:
                        edge_col = pi.node_color()
                    e = EdgeItem(pi, ci, edge_col)
                    self.addItem(e)
                    self._edges.append(e)
                    # ── Index this edge under both endpoint node IDs ─────────
                    # Enables O(degree) drag updates instead of O(all_edges).
                    self._node_edge_index.setdefault(pid,   []).append(e)
                    self._node_edge_index.setdefault(cn.id, []).append(e)

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

    def _on_node_move_start(self, _: str) -> None:
        self._push_undo()

    def _on_node_moved(self, node_id: str) -> None:
        if getattr(self, "_bulk_move", False):
            self._drag_dirty = True
            return
        # Only update edges that actually connect to the moved node.
        # Before: iterated ALL edges every mouse-move (60 fps × n_edges work).
        # After:  iterates only the 1–5 edges per node — typically a 50-200× speedup.
        for e in self._node_edge_index.get(node_id, []):
            e.update_path()
        self._drag_dirty = True

    # ── Connect mode ──────────────────────────────────────────────────────────

    def _start_connect(self, node_id: str, direction: str) -> None:
        self.connect_mode        = True
        self._connect_from_id    = node_id
        self._connect_direction  = direction
        if self._temp_edge is None:
            self._temp_edge = TempEdgeItem(
                self.theme_palette.get("year_fg", "#c8963c"))
            self.addItem(self._temp_edge)
        self.connect_mode_changed.emit(direction)
        self._connect_dragged = False   # will be set True on first mouseMoveEvent drag
        self.update()

    def finish_connect(self, to_id: str) -> None:
        from_id    = self._connect_from_id
        direction  = self._connect_direction
        self.connect_mode      = False
        self._connect_from_id  = None
        self._connect_dragged  = False
        if self._temp_edge:
            self.removeItem(self._temp_edge); self._temp_edge = None
        self.connect_mode_changed.emit("")
        self.update()
        if self.tree and from_id and from_id != to_id:
            self.connect_requested.emit(from_id, to_id, direction)

    def cancel_connect(self) -> None:
        self.connect_mode      = False
        self._connect_from_id  = None
        self._connect_dragged  = False
        if self._temp_edge:
            self.removeItem(self._temp_edge); self._temp_edge = None
        self.connect_mode_changed.emit("")
        self.update()

    # ── Undo ──────────────────────────────────────────────────────────────────

    def _push_undo(self) -> None:
        if self._undoing or not self.tree: return
        state = self.tree.to_dict()
        # NOTE: deliberately no deep equality check against the previous
        # snapshot here.  That comparison walked the entire tree dict (which
        # includes per-node image/thumbnail strings) on *every* mutation and
        # on every drag start, and was a measurable stall on large lineages.
        # Pushing a duplicate snapshot is harmless; undo simply restores the
        # same state, and the stack is capped at 50 entries below.
        self._undo_stack.append({
            "tree": state,
            "touched_images": set(self.tree._image_loaded_ids),
        })
        if len(self._undo_stack) > 50: self._undo_stack.pop(0)

    def undo(self) -> None:
        if not self._undo_stack or not self.tree: return
        from models import LineageTree as LT
        entry = self._undo_stack.pop()
        old_tree = LT.from_dict(entry["tree"])
        old_tree._image_loaded_ids = set(entry.get("touched_images", ()))
        self._undoing = True
        try:
            self.load_tree(old_tree)
        finally:
            self._undoing = False
        self.tree_changed.emit()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_node(self, node: PlantNode) -> None:
        if not self.tree: return
        self._push_undo()
        self.tree.add_node(node)
        if node.images or node.thumb:
            import lineage_images
            lineage_images.mark_images_authoritative(self.tree, node.id)
        self._add_node_item(node)
        if node.parent_ids:
            self._add_edges_for_child(node)       # F42: incremental, O(parents)
        self._update_year_rows(); self._recalc_scene_rect()
        self.tree_changed.emit()

    def update_node(self, node: PlantNode) -> None:
        self._push_undo()
        item = self._node_items.get(node.id)
        if item:
            item.refresh()
            # Topology (parent/child links) did not change, so rebuilding
            # every EdgeItem in the scene here was pure waste.  Only edges
            # touching this node can be affected: their path may change if
            # the card height changed, and their colour if this node is the
            # parent and its colour changed.
            for e in self._node_edge_index.get(node.id, []):
                if e.parent_item is item:
                    role = self._role_map.get(node.id, "")
                    if self.color_code_lines and role == "mother":
                        e.set_color(self.mother_line_color)
                    elif self.color_code_lines and role == "father":
                        e.set_color(self.father_line_color)
                    else:
                        e.set_color(item.node_color())
                e.update_path()
        self._update_year_rows()
        self.tree_changed.emit()

    def remove_node(self, node_id: str) -> None:
        if not self.tree: return
        self._push_undo()
        item = self._node_items.pop(node_id, None)
        if item: self.removeItem(item)
        self.tree.remove_node(node_id)
        self._rebuild_edges(); self._update_year_rows(); self._recalc_scene_rect()
        self.tree_changed.emit()

    def add_comment(self, note: CommentNote) -> None:
        if not self.tree: return
        self.tree.add_comment(note); self._add_comment_item(note)
        self.tree_changed.emit()

    def remove_comment(self, cid: str) -> None:
        if not self.tree: return
        item = self._comment_items.pop(cid, None)
        if item: self.removeItem(item)
        self.tree.remove_comment(cid); self.tree_changed.emit()

    def apply_connection(self, parent_id: str, child_id: str) -> bool:
        if not self.tree: return False
        self._push_undo()
        ok = self.tree.connect_parent(parent_id, child_id)
        if ok: self._rebuild_edges(); self.tree_changed.emit()
        return ok

    def remove_connection(self, parent_id: str, child_id: str) -> None:
        if not self.tree: return
        self._push_undo()
        self.tree.disconnect_parent(parent_id, child_id)
        self._rebuild_edges(); self.tree_changed.emit()

    def delete_selected(self) -> None:
        if not self.tree:
            return
        ids = [i.node.id for i in self.selectedItems() if isinstance(i, PlantNodeItem)]
        if not ids:
            return
        # One undo snapshot and one edge/year/rect rebuild for the whole
        # batch.  The previous per-node loop did all of that K times, making
        # a large multi-selection delete O(K × (N + E + snapshot)).
        self._push_undo()
        for nid in ids:
            item = self._node_items.pop(nid, None)
            if item:
                self.removeItem(item)
            self.tree.remove_node(nid)
        self._rebuild_edges()
        self._update_year_rows()
        self._recalc_scene_rect()
        self.tree_changed.emit()

    # ── Copy/paste ────────────────────────────────────────────────────────────

    def copy_selected_nodes(self) -> int:
        sel = [i for i in self.selectedItems() if isinstance(i, PlantNodeItem)]
        if not sel: return 0
        import lineage_images
        for i in sel:
            # Make sure we're copying the node's real photos, not an empty
            # placeholder left over from lazy loading.
            lineage_images.populate_node_images(self.tree, self.filepath, i.node.id)
        self._clipboard_nodes = [i.node.to_dict() for i in sel]
        sel_ids = {i.node.id for i in sel}
        for d in self._clipboard_nodes:
            d["parent_ids"]   = [p for p in d["parent_ids"]   if p in sel_ids]
            d["parent_roles"] = {k: v for k, v in d.get("parent_roles",{}).items() if k in sel_ids}
        return len(self._clipboard_nodes)

    def paste_nodes(self, offset: QPointF = QPointF(60, 60)) -> int:
        if not self._clipboard_nodes or not self.tree: return 0
        import uuid
        import lineage_images
        self._push_undo()
        id_map: Dict[str, str] = {}
        new_nodes = []
        for d in self._clipboard_nodes:
            nd = dict(d); old_id = nd["id"]
            nd["id"] = str(uuid.uuid4())
            nd["x"]  = float(nd.get("x", 0)) + offset.x()
            nd["y"]  = float(nd.get("y", 0)) + offset.y()
            id_map[old_id] = nd["id"]; new_nodes.append(nd)
        for nd in new_nodes:
            nd["parent_ids"]   = [id_map[p] for p in nd["parent_ids"]   if p in id_map]
            nd["parent_roles"] = {id_map[k]: v for k, v in nd.get("parent_roles",{}).items() if k in id_map}
        self.clearSelection()
        for nd in new_nodes:
            node = PlantNode.from_dict(nd)
            self.tree.add_node(node)
            # This node's .images came straight from the clipboard (real data,
            # not a lazy placeholder) — it must be persisted on next save.
            lineage_images.mark_images_authoritative(self.tree, node.id)
            item = self._add_node_item(node)
            item.setSelected(True)
        self._rebuild_edges(); self._update_year_rows(); self._recalc_scene_rect()
        self.tree_changed.emit()
        return len(new_nodes)

    # ── Signal dispatchers ────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        items = self.selectedItems()
        if items and isinstance(items[0], PlantNodeItem):
            self.node_selected.emit(items[0].node.id)
        else:
            self.node_selected.emit("")

    def _request_edit(self, nid: str) -> None:
        item = self._node_items.get(nid)
        if item: self.clearSelection(); item.setSelected(True)
        self.edit_node_requested.emit(nid)

    def _delete_node(self, nid: str) -> None: self.remove_node(nid)
    def _request_add_parent(self, nid: str) -> None: self.add_parent_requested.emit(nid)
    def _request_add_offspring(self, nid: str) -> None: self.add_offspring_requested.emit(nid)

    # ── Auto-layout ───────────────────────────────────────────────────────────

    # ── Node search ───────────────────────────────────────────────────────────

    def search_nodes(self, query: str) -> list:
        """Highlight nodes whose name/species/tags contain query. Returns matching ids."""
        q = query.strip().lower()
        matches = []
        for nid, item in self._node_items.items():
            node = item.node
            haystack = " ".join([
                node.name or "",
                node.species or "",
                node.cultivar or "",
                " ".join(node.tags),
                str(node.year or ""),
            ]).lower()
            hit = bool(q) and q in haystack
            # Only dirty the item when the highlight actually flipped: this is
            # wired to the search box's textChanged, so an unconditional
            # update() marked the whole canvas for repaint on every keystroke,
            # including the keystrokes that changed nothing.
            if hit != item._search_highlight:
                item._search_highlight = hit
                item.update()
            if hit:
                matches.append(nid)
        self.search_results_changed.emit(matches)
        return matches

    def clear_search(self) -> None:
        for item in self._node_items.values():
            item._search_highlight = False
            item.update()
        self.search_results_changed.emit([])

    def auto_layout(self, record_undo: bool = True) -> None:
        if not self.tree or not self.tree.nodes: return
        try:   self._do_auto_layout(record_undo)
        except Exception as e: log.error("auto_layout: %s", e)

    def auto_organize(self) -> None:
        """
        Minimize edge crossings using the Sugiyama barycenter heuristic.
        Works on the existing positions — it only reorders nodes within
        their row (same Y band) to reduce the number of crossing lines,
        then respread X positions evenly.  Does NOT change the Y layout
        or add new rows.  Call after auto_layout or on a manually arranged
        canvas.
        """
        if not self.tree or not self.tree.nodes: return
        try:   self._do_auto_organize()
        except Exception as e: log.error("auto_organize: %s", e)

    def _do_auto_organize(self) -> None:
        self._push_undo()
        nodes   = list(self.tree.nodes.values())
        id_to   = self.tree.nodes
        H_GAP   = CARD_W + 90

        # ── 1. Cluster nodes into rows by Y position (±80px tolerance) ──
        rows: List[List[PlantNode]] = []
        for n in sorted(nodes, key=lambda x: x.y):
            placed = False
            for row in rows:
                if abs(row[0].y - n.y) < 120:
                    row.append(n); placed = True; break
            if not placed:
                rows.append([n])
        rows.sort(key=lambda r: r[0].y)

        # ── 2. Barycenter sweep (top-down then bottom-up, 6 passes) ──
        # Precompute parent → children once instead of rescanning every node
        # for every node on every pass (was O(n² × 6)).
        children_of: Dict[str, List[PlantNode]] = {}
        for c in nodes:
            for pid in c.parent_ids:
                if pid in id_to:
                    children_of.setdefault(pid, []).append(c)

        def barycenter_up(row: List[PlantNode]) -> List[PlantNode]:
            """Sort row nodes by average X of their parents (row above)."""
            def bc(n: PlantNode) -> float:
                pxs = [id_to[pid].x for pid in n.parent_ids if pid in id_to]
                return sum(pxs)/len(pxs) if pxs else n.x
            return sorted(row, key=bc)

        def barycenter_down(row: List[PlantNode]) -> List[PlantNode]:
            """Sort row nodes by average X of their children (row below)."""
            def bc(n: PlantNode) -> float:
                children = children_of.get(n.id)
                return sum(c.x for c in children)/len(children) if children else n.x
            return sorted(row, key=bc)

        for _pass in range(6):
            # Top-down: fix upper rows, pull lower by parent barycenters
            for i in range(1, len(rows)):
                rows[i] = barycenter_up(rows[i])
            # Bottom-up: fix lower rows, pull upper by child barycenters
            for i in range(len(rows) - 2, -1, -1):
                rows[i] = barycenter_down(rows[i])

        # ── 3. Re-spread each row evenly around its current centre ──
        for row in rows:
            if not row: continue
            row_y = sum(n.y for n in row) / len(row)
            total_w = len(row) * H_GAP
            start_x = -total_w / 2 + H_GAP / 2
            for i, n in enumerate(row):
                n.x = start_x + i * H_GAP
                n.y = row_y
                item = self._node_items.get(n.id)
                if item: item.setPos(n.x, n.y)

        # ── 4. Mother-left / father-right refinement ──
        for child in nodes:
            if len(child.parent_ids) == 2:
                p0_id, p1_id = child.parent_ids[0], child.parent_ids[1]
                p0 = id_to.get(p0_id); p1 = id_to.get(p1_id)
                if not p0 or not p1: continue
                r0 = child.parent_roles.get(p0_id, "")
                r1 = child.parent_roles.get(p1_id, "")
                swap = False
                if r0 == "father" and r1 != "father" and p0.x < p1.x: swap = True
                elif r0 == "mother" and r1 != "mother" and p0.x > p1.x: swap = True
                if swap:
                    p0.x, p1.x = p1.x, p0.x
                    for pid in (p0_id, p1_id):
                        it = self._node_items.get(pid)
                        if it: it.setPos(id_to[pid].x, id_to[pid].y)

        for e in self._edges: e.update_path()
        self._update_year_rows(); self._recalc_scene_rect()
        self.tree_changed.emit()

    def _do_auto_layout(self, record_undo: bool = True) -> None:
        if record_undo: self._push_undo()
        nodes  = list(self.tree.nodes.values())
        id_to  = self.tree.nodes
        gen_cache: Dict[str, int] = {}
        max_gen = max((self.tree.get_generation(n.id, gen_cache) for n in nodes), default=0)
        real_years = sorted(set(n.year for n in nodes if n.year))

        def row_key(n: PlantNode) -> int:
            if n.year: return n.year
            gen = self.tree.get_generation(n.id, gen_cache)
            return (real_years[0] if real_years else 2000) - (max_gen - gen) - 1

        by_row: Dict[int, List[PlantNode]] = {}
        for n in nodes: by_row.setdefault(row_key(n), []).append(n)
        sorted_rows = sorted(by_row.keys())
        H_GAP, V_GAP = CARD_W + 90, 340
        row_y = {rk: i * V_GAP for i, rk in enumerate(sorted_rows)}

        for rk in sorted_rows:
            row_nodes = by_row[rk]
            def sort_key(n: PlantNode) -> float:
                pxs = [id_to[pid].x for pid in n.parent_ids if pid in id_to]
                return sum(pxs)/len(pxs) if pxs else 0.0
            row_nodes.sort(key=sort_key)
            total_w = len(row_nodes) * H_GAP
            start_x = -total_w / 2 + H_GAP / 2
            for i, n in enumerate(row_nodes):
                n.x = start_x + i * H_GAP; n.y = row_y[rk]
                item = self._node_items.get(n.id)
                if item: item.setPos(n.x, n.y)

        for child in nodes:
            if len(child.parent_ids) == 2:
                p0_id, p1_id = child.parent_ids[0], child.parent_ids[1]
                p0 = id_to.get(p0_id); p1 = id_to.get(p1_id)
                if not p0 or not p1: continue
                r0, r1 = child.parent_roles.get(p0_id,""), child.parent_roles.get(p1_id,"")
                swap = False
                if r0 == "father" and r1 != "father" and p0.x < p1.x: swap = True
                elif r0 == "mother" and r1 != "mother" and p0.x > p1.x: swap = True
                if swap:
                    p0.x, p1.x = p1.x, p0.x
                    for pid in (p0_id, p1_id):
                        it = self._node_items.get(pid)
                        if it: it.setPos(id_to[pid].x, id_to[pid].y)

        for e in self._edges: e.update_path()
        self._update_year_rows(); self._recalc_scene_rect()
        self.tree_changed.emit()

    # ── Clipboard image paste ─────────────────────────────────────────────────

    def paste_image_to_selected(self) -> bool:
        from PyQt6.QtWidgets import QApplication
        sel = self.selectedItems()
        if not sel or not isinstance(sel[0], PlantNodeItem): return False
        item: PlantNodeItem = sel[0]
        cb = QApplication.clipboard()
        img = cb.image()
        if not img.isNull():
            item._set_image_from_qimage(img); return True
        text = cb.text().strip()
        if text and text.lower().endswith((".png",".jpg",".jpeg",".webp",".bmp")):
            item._set_image_from_path(text); return True
        return False


# ── View ──────────────────────────────────────────────────────────────────────

YEAR_LABEL_W = 100

class LineageView(QGraphicsView):

    def __init__(self, scene: LineageScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setViewportMargins(YEAR_LABEL_W, 0, 0, 0)

        self._pan_active    = False
        self._pan_start     = None
        self._space_held    = False
        self._zoom_factor   = 1.0
        self._min_zoom      = 0.05
        self._max_zoom      = 5.0
        self._show_grid     = True
        self._dot_color     = "#1e2e24"
        self._bg_color      = "#111816"
        self._year_text_col = "#6ee880"
        self._year_line_col = "#2a4030"
        self._year_band_bg  = "#0e1610"
        self._font_size     = 22
        self._font_family   = "Segoe UI"
        self._rubber_band: Optional[QRubberBand] = None
        # Memoised (year_rows, sorted_items) — see _sorted_year_rows().
        self._year_rows_sorted = None
        self._rb_origin     = None
        self._scene: LineageScene = scene
        # Search bar (created lazily on first Ctrl+F)
        self._search_bar: Optional[QWidget]     = None
        self._search_edit                        = None
        self._search_results: list               = []
        self._search_idx: int                    = 0
        self._search_result_lbl                  = None
        self._rebuild_paint_resources()
        self._apply_bg()

    def _apply_bg(self) -> None:
        self.setBackgroundBrush(QBrush(QColor(self._bg_color)))

    def _rebuild_paint_resources(self) -> None:
        """Re-derive everything drawBackground()/drawForeground() need.

        Those two run on every viewport repaint — which, while panning or
        zooming, means every frame — and they used to parse hex colours, build
        QFonts, QPens and QColors from scratch each time.  All of it depends
        only on the active palette and font settings, so it is built here and
        rebuilt only when one of those actually changes.
        """
        self._bg_brush        = QBrush(QColor(self._bg_color))
        self._dot_pen         = QPen(QColor(self._dot_color), 1.5)
        self._year_text_pen   = QPen(QColor(self._year_text_col))
        self._year_line_pen   = QPen(QColor(self._year_line_col), 1)
        self._year_dash_pen   = QPen(QColor(self._year_line_col), 1, Qt.PenStyle.DashLine)
        self._year_band_brush = QBrush(QColor(self._year_band_bg))
        self._year_font       = QFont(self._font_family,
                                      max(self._font_size, 16),  # year labels larger
                                      QFont.Weight.Bold)
        # Accent-tint brush is keyed by colour; force a rebuild.
        self._accent_brush     = None
        self._accent_brush_key = None

    def set_theme_palette(self, p: dict) -> None:
        self._bg_color      = p.get("canvas_bg",      "#111816")
        self._dot_color     = p.get("canvas_dot",     "#1e2e24")
        self._year_text_col = p.get("year_band_text", "#6ee880")
        self._year_line_col = p.get("year_band_line", "#2a4030")
        self._year_band_bg  = p.get("year_band_bg",   "#0e1610")
        self._rebuild_paint_resources()
        self.setBackgroundBrush(self._bg_brush)
        self.viewport().update()

    def set_font_size(self, fs: int) -> None:
        self._font_size = fs
        self._rebuild_paint_resources()
        self.viewport().update()

    def set_font_family(self, ff: str) -> None:
        self._font_family = ff
        self._rebuild_paint_resources()
        self.viewport().update()

    def set_show_grid(self, v: bool) -> None:
        self._show_grid = v; self.viewport().update()

    # ── Background ────────────────────────────────────────────────────────────

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        # Per-lineage accent tint (very subtle overlay)
        tint_brush = self._accent_tint_brush()
        if tint_brush is not None:
            painter.fillRect(rect, tint_brush)
        if not self._show_grid: return
        # Batch all grid dots into one native call — far faster than calling
        # drawPoint() in a Python loop for large zoomed-out viewports.
        #
        # The pen and the accent brush are cached (see
        # _rebuild_paint_resources); only the point list is rebuilt per frame,
        # because the visible rect changes with every pan.
        sp = 40
        if sp * self.transform().m11() < 8: return
        x0 = int(rect.left())  - (int(rect.left())  % sp)
        y0 = int(rect.top())   - (int(rect.top())   % sp)
        pts: list[QPointF] = []
        x = x0
        while x <= rect.right():
            y = y0
            while y <= rect.bottom():
                pts.append(QPointF(x, y))
                y += sp
            x += sp
        if pts:
            painter.setPen(self._dot_pen)
            painter.drawPoints(QPolygonF(pts))

    def _accent_tint_brush(self):
        """Brush for the current lineage's accent tint, cached per colour.

        drawBackground() runs on every viewport repaint, so parsing the accent
        hex and building a QColor/QBrush per frame was pure overhead for a
        value that only changes when a different lineage is opened.
        """
        accent = self._scene.tree.accent_color if self._scene.tree else ""
        if not accent:
            self._accent_brush_key = None
            return None
        if accent != self._accent_brush_key:
            try:
                tint = QColor(accent)
                tint.setAlpha(18)
                self._accent_brush = QBrush(tint)
            except Exception:
                self._accent_brush = None
            self._accent_brush_key = accent
        return self._accent_brush

    # ── Year overlay ──────────────────────────────────────────────────────────

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        try:
            year_rows = self._scene.year_rows
            if not year_rows: return
            vp = self.viewport()
            vp_h = vp.height()
            painter.save()
            painter.resetTransform()
            painter.fillRect(QRectF(0, 0, YEAR_LABEL_W, vp_h), self._year_band_brush)
            painter.setPen(self._year_line_pen)
            painter.drawLine(YEAR_LABEL_W, 0, YEAR_LABEL_W, vp_h)
            painter.setFont(self._year_font)
            painter.setPen(self._year_text_pen)
            # year_rows is a static dict between layout changes — sorting it on
            # every repaint was re-doing the same work each frame.
            for year, scene_y in self._sorted_year_rows(year_rows):
                vy = self.mapFromScene(QPointF(0, scene_y)).y()
                painter.drawText(QRectF(4, vy-18, YEAR_LABEL_W-8, 36),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 str(year))
                painter.setPen(self._year_dash_pen)
                painter.drawLine(YEAR_LABEL_W, int(vy), self.viewport().width(), int(vy))
                painter.setPen(self._year_text_pen)
            painter.restore()
        except Exception as e: log.error("drawForeground: %s", e)

    def _sorted_year_rows(self, year_rows: dict):
        """(year, scene_y) pairs sorted by year, memoised per year_rows object.

        The dict is replaced wholesale whenever the layout changes, so
        identity is a sound cache key.
        """
        rev = getattr(self._scene, "_year_rev", 0)
        cached = self._year_rows_sorted
        if cached is not None and cached[0] == rev:
            return cached[1]
        ordered = sorted(year_rows.items())
        self._year_rows_sorted = (rev, ordered)
        return ordered

    # ── Mouse ─────────────────────────────────────────────────────────────────

    # ── Port proximity helper (scene coords) ─────────────────────────────────

    PORT_SNAP_SCREEN_PX = 80   # snap distance in SCREEN pixels (zoom-independent)

    def _find_port_at(self, scene_pos: QPointF):
        """
        Scan every PlantNodeItem and return (item, direction) if scene_pos
        is within PORT_SNAP_SCREEN_PX screen-pixels of a port, else (None, None).
        We convert the snap radius to scene units using the current view transform
        so the hit zone stays the same physical size regardless of zoom level.
        Top port    → "top"    (this node seeks a parent)
        Bottom port → "bottom" (this node seeks a child)
        """
        # Convert screen pixels → scene units using the view's current scale
        zoom = self.transform().m11()          # horizontal scale factor (=zoom)
        snap_scene = self.PORT_SNAP_SCREEN_PX / zoom if zoom > 0 else self.PORT_SNAP_SCREEN_PX
        snap2 = snap_scene ** 2

        _r = QRectF(scene_pos.x()-snap_scene, scene_pos.y()-snap_scene, 2*snap_scene, 2*snap_scene)
        from node_item import PlantNodeItem as _PNI
        for item in (i for i in self._scene.items(_r) if isinstance(i, _PNI)):
            tp = item.top_port_scene()
            dx, dy = scene_pos.x()-tp.x(), scene_pos.y()-tp.y()
            if dx*dx + dy*dy <= snap2:
                return item, "top"
            bp = item.bottom_port_scene()
            dx, dy = scene_pos.x()-bp.x(), scene_pos.y()-bp.y()
            if dx*dx + dy*dy <= snap2:
                return item, "bottom"
        return None, None

    def mousePressEvent(self, event):
        try:
            pos = event.position()
            if pos.x() <= YEAR_LABEL_W:
                event.accept(); return

            if (event.button() == Qt.MouseButton.LeftButton and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._rb_origin = event.position().toPoint()
                if self._rubber_band is None:
                    self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                self._rubber_band.setGeometry(QRect(self._rb_origin, QSize()))
                self._rubber_band.show()
                event.accept(); return

            if (event.button() == Qt.MouseButton.MiddleButton or
                    (event.button() == Qt.MouseButton.LeftButton and self._space_held)):
                self._pan_active = True; self._pan_start = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept(); return

            if self._scene.connect_mode:
                if event.button() == Qt.MouseButton.RightButton:
                    self._scene.cancel_connect(); event.accept(); return
                if event.button() == Qt.MouseButton.LeftButton:
                    sp   = self.mapToScene(event.position().toPoint())
                    item = self._scene.itemAt(sp, QTransform())
                    if isinstance(item, PlantNodeItem):
                        self._scene.finish_connect(item.node.id)
                    else:
                        self._scene.cancel_connect()
                    event.accept(); return

            sp   = self.mapToScene(event.position().toPoint())
            item = self._scene.itemAt(sp, QTransform())

            # Right-click on edge → delete connection menu
            if event.button() == Qt.MouseButton.RightButton:
                if isinstance(item, EdgeItem):
                    self._show_edge_context_menu(item, event.globalPosition().toPoint())
                    event.accept(); return
                # Right-click on empty canvas → canvas context menu
                if item is None:
                    self._show_canvas_context_menu(event.globalPosition().toPoint(), sp)
                    event.accept(); return
                # Right-click on PlantNodeItem or anything else: fall through to super()
                # so the item's contextMenuEvent fires normally

            if event.button() == Qt.MouseButton.LeftButton:
                if item is None or isinstance(item, EdgeItem):
                    self._scene.clearSelection()
                    self._pan_active = True; self._pan_start = event.position()
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    event.accept(); return
                # Left-click on PlantNodeItem: fall through to super() so the item's
                # mousePressEvent fires — it handles port detection internally

        except Exception as e: log.error("mousePressEvent: %s", e)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click on empty canvas → open Plant DB dialog.
        Double-click on a node is handled by PlantNodeItem itself and doesn't
        reach here, so this only fires on empty space.
        """
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                sp   = self.mapToScene(event.position().toPoint())
                item = self._scene.itemAt(sp, QTransform())
                if item is None or isinstance(item, EdgeItem):
                    # Empty canvas — open the Plant DB so the user can pick
                    # a plant to import into the current lineage
                    self._scene.open_plant_db_requested.emit()
                    event.accept()
                    return
        except Exception as e:
            log.error("mouseDoubleClickEvent: %s", e)
        super().mouseDoubleClickEvent(event)

    def _show_canvas_context_menu(self, global_pos, scene_pos: QPointF) -> None:
        menu = QMenu(self)
        a_plant   = menu.addAction("🌿  Add New Plant here")
        a_from_db = menu.addAction("📋  Add Plant from Database…")
        a_comment = menu.addAction("💬  Add Comment Box here")
        menu.addSeparator()
        a_color   = menu.addAction("🎨  Set Lineage Accent Color…")
        a_clr_col = menu.addAction("✖  Clear Accent Color")
        chosen = menu.exec(global_pos)
        if chosen == a_plant:
            self._scene.add_plant_at.emit(scene_pos.x(), scene_pos.y())
        elif chosen == a_from_db:
            self._scene.open_plant_db_requested.emit()
        elif chosen == a_comment:
            self._scene.add_comment_at.emit(scene_pos.x(), scene_pos.y())
        elif chosen == a_color:
            self._pick_lineage_color()
        elif chosen == a_clr_col:
            if self._scene.tree:
                self._scene.tree.accent_color = ""
                self._scene.tree.touch()
                self.viewport().update()

    def _show_edge_context_menu(self, edge: "EdgeItem", global_pos) -> None:
        menu = QMenu(self)
        a_del = menu.addAction("✂️  Delete this Connection")
        chosen = menu.exec(global_pos)
        if chosen == a_del:
            pid = edge.parent_item.node.id
            cid = edge.child_item.node.id
            self._scene.remove_connection(pid, cid)

    def mouseMoveEvent(self, event):
        try:
            if self._rb_origin is not None and self._rubber_band is not None:
                self._rubber_band.setGeometry(
                    QRect(self._rb_origin, event.position().toPoint()).normalized())
                event.accept(); return
            if self._pan_active and self._pan_start is not None:
                delta = event.position() - self._pan_start
                self._pan_start = event.position()
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - int(delta.x()))
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - int(delta.y()))
                event.accept(); return
            if self._scene.connect_mode and self._scene._temp_edge:
                fid = self._scene._connect_from_id
                fi  = self._scene._node_items.get(fid) if fid else None
                if fi:
                    sp = self.mapToScene(event.position().toPoint())
                    if self._scene._connect_direction == "top":
                        start_pt = fi.top_port_scene()
                    else:
                        start_pt = fi.bottom_port_scene()
                    self._scene._temp_edge.set_endpoints(start_pt, sp)
                    if not getattr(self._scene, "_connect_dragged", False):
                        self._scene._connect_dragged = True
                event.accept(); return
            # Show crosshair when hovering near a port
            if not self._pan_active:
                sp = self.mapToScene(event.position().toPoint())
                near_item, _ = self._find_port_at(sp)
                if near_item:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                elif not self._scene.connect_mode:
                    self.setCursor(Qt.CursorShape.ArrowCursor)
        except Exception as e: log.error("mouseMoveEvent: %s", e)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        try:
            if self._rb_origin is not None and self._rubber_band is not None:
                rb_rect    = QRect(self._rb_origin, event.position().toPoint()).normalized()
                scene_rect = self.mapToScene(rb_rect).boundingRect()
                self._rubber_band.hide(); self._rubber_band = None; self._rb_origin = None
                self._scene.clearSelection()
                for item in self._scene.items(scene_rect):
                    if isinstance(item, PlantNodeItem): item.setSelected(True)
                event.accept(); return

            # ── Drag-to-connect completion ────────────────────────────────────
            # Only fires when the user actually dragged (not a quick tap/click).
            # Click-to-connect is still handled by mousePressEvent below.
            if (self._scene.connect_mode
                    and event.button() == Qt.MouseButton.LeftButton
                    and getattr(self._scene, "_connect_dragged", False)):
                sp = self.mapToScene(event.position().toPoint())
                # 1. Try port-snap first (generous invisible radius)
                target_item, _ = self._find_port_at(sp)
                # 2. Fall back to any PlantNodeItem under the cursor
                if target_item is None:
                    raw = self._scene.itemAt(sp, QTransform())
                    if isinstance(raw, PlantNodeItem):
                        target_item = raw
                if target_item and target_item.node.id != self._scene._connect_from_id:
                    self._scene.finish_connect(target_item.node.id)
                else:
                    self._scene.cancel_connect()
                event.accept()
                return

            if self._pan_active:
                self._pan_active = False; self._pan_start = None
                self.setCursor(Qt.CursorShape.OpenHandCursor if self._space_held
                               else Qt.CursorShape.ArrowCursor)
                event.accept(); return
        except Exception as e: log.error("mouseReleaseEvent: %s", e)
        super().mouseReleaseEvent(event)
        self._scene._update_year_rows()
        # Flush any deferred tree_changed from node drag frames (set by _on_node_moved)
        if self._scene._drag_dirty:
            self._scene._drag_dirty = False
            self._scene.tree_changed.emit()
        self.viewport().update()

    # ── Zoom / pan ───────────────────────────────────────────────────────────
    # Convention matches most canvas/diagram tools (Figma, Miro, draw.io):
    # plain wheel = pan (shift = horizontal), Ctrl/Cmd+wheel = zoom at cursor.
    # "F" already exists as a dedicated fit-to-view shortcut, so it no longer
    # needs to double up on a wheel modifier that most users expect to zoom.

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_wheel(event)
            return
        self._pan_wheel(event)

    def _suspend_item_cache(self) -> None:
        if not getattr(self, "_cache_suspended", False):
            self._cache_suspended = True
            for it in self._scene._node_items.values():
                it.setCacheMode(QGraphicsItem.CacheMode.NoCache)
            t = getattr(self, "_cache_timer", None)
            if t is None:
                t = self._cache_timer = QTimer(self); t.setSingleShot(True); t.setInterval(180)
                t.timeout.connect(self._resume_item_cache)
        self._cache_timer.start()

    def _resume_item_cache(self) -> None:
        self._cache_suspended = False
        for it in self._scene._node_items.values():
            it.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.viewport().update()

    def _zoom_wheel(self, event) -> None:
        # Smooth zoom: finer steps near max/min so it doesn't feel snappy
        delta = event.angleDelta().y()
        # Support hi-res trackpad (pixel delta)
        if delta == 0:
            delta = event.pixelDelta().y() * 4
        steps   = delta / 120.0
        factor  = 1.10 ** steps
        new_z   = self._zoom_factor * factor
        if new_z < self._min_zoom:
            factor = self._min_zoom / self._zoom_factor
        elif new_z > self._max_zoom:
            factor = self._max_zoom / self._zoom_factor
        self._suspend_item_cache()
        self.scale(factor, factor)
        self._zoom_factor = max(self._min_zoom,
                                min(self._max_zoom, self._zoom_factor * factor))

    def _pan_wheel(self, event) -> None:
        dy = event.angleDelta().y()
        dx = event.angleDelta().x()
        if dx == 0 and dy == 0:
            px = event.pixelDelta()
            dx, dy = px.x(), px.y()
        # Shift+wheel pans horizontally on mice with no native horizontal wheel
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and dx == 0:
            dx, dy = dy, 0
        if dx:
            hbar = self.horizontalScrollBar()
            hbar.setValue(hbar.value() - dx)
        if dy:
            vbar = self.verticalScrollBar()
            vbar.setValue(vbar.value() - dy)
        event.accept()

    # ── Keys ─────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = True
            if not self._pan_active: self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept(); return
        if event.key() == Qt.Key.Key_Escape:
            if self._search_bar and self._search_bar.isVisible():
                self._close_search_bar()
            else:
                self._scene.cancel_connect()
            event.accept(); return
        if event.key() == Qt.Key.Key_F and not event.modifiers():
            self.fit_view(); event.accept(); return
        if (event.key() == Qt.Key.Key_F and
                event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._show_search_bar(); event.accept(); return
        super().keyPressEvent(event)

    # ── Floating search bar ───────────────────────────────────────────────────

    def _show_search_bar(self) -> None:
        """Show a floating search bar in the top-right of the canvas."""
        from PyQt6.QtWidgets import QLineEdit as _LE, QLabel as _Lbl
        if self._search_bar is None:
            container = QWidget(self.viewport())
            container.setObjectName("search_bar_container")
            container.setStyleSheet(
                "QWidget#search_bar_container {"
                "  background: rgba(20,36,24,220);"
                "  border: 1px solid #3a6040;"
                "  border-radius: 8px;"
                "}"
                "QLineEdit { background: #0e1a12; color: #e0f0e4;"
                "  border: 1px solid #3a6040; border-radius: 5px;"
                "  padding: 4px 8px; font-size: 13px; }"
            )
            lay = QHBoxLayout(container)
            lay.setContentsMargins(8, 6, 8, 6); lay.setSpacing(6)
            icon = _Lbl("🔍")
            icon.setStyleSheet("background: transparent; font-size: 14px;")
            edit = _LE(); edit.setPlaceholderText("Search plants…")
            edit.setFixedWidth(200)
            edit.textChanged.connect(self._on_search_text)
            edit.returnPressed.connect(self._search_next)
            lay.addWidget(icon); lay.addWidget(edit)
            self._search_result_lbl = _Lbl("")
            self._search_result_lbl.setStyleSheet(
                "background: transparent; color: #88cc88; font-size: 11px;")
            lay.addWidget(self._search_result_lbl)
            nav_prev = QPushButton("▲"); nav_prev.setFixedSize(24, 24)
            nav_next = QPushButton("▼"); nav_next.setFixedSize(24, 24)
            nav_prev.clicked.connect(self._search_prev)
            nav_next.clicked.connect(self._search_next)
            close_btn = QPushButton("✕"); close_btn.setFixedSize(24, 24)
            close_btn.clicked.connect(self._close_search_bar)
            lay.addWidget(nav_prev); lay.addWidget(nav_next); lay.addWidget(close_btn)
            container.adjustSize()
            self._search_bar = container
            self._search_edit = edit
            self._search_results: list = []
            self._search_idx: int = 0
        vp = self.viewport()
        sb = self._search_bar
        sb.resize(sb.sizeHint())
        sb.move(vp.width() - sb.width() - 16, 12)
        sb.show(); sb.raise_()
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    def _close_search_bar(self) -> None:
        if self._search_bar:
            self._search_bar.hide()
        self._scene.clear_search()

    def _on_search_text(self, text: str) -> None:
        results = self._scene.search_nodes(text)
        self._search_results = results
        self._search_idx = 0
        if results:
            self._search_result_lbl.setText(f"1/{len(results)}")
            self._pan_to_node(results[0])
        else:
            self._search_result_lbl.setText("" if not text.strip() else "0 results")

    def _search_next(self) -> None:
        if not self._search_results: return
        self._search_idx = (self._search_idx + 1) % len(self._search_results)
        self._search_result_lbl.setText(
            f"{self._search_idx+1}/{len(self._search_results)}")
        self._pan_to_node(self._search_results[self._search_idx])

    def _search_prev(self) -> None:
        if not self._search_results: return
        self._search_idx = (self._search_idx - 1) % len(self._search_results)
        self._search_result_lbl.setText(
            f"{self._search_idx+1}/{len(self._search_results)}")
        self._pan_to_node(self._search_results[self._search_idx])

    def _pick_lineage_color(self) -> None:
        from PyQt6.QtWidgets import QColorDialog
        if not self._scene.tree: return
        current = self._scene.tree.accent_color or "#1a3a22"
        color = QColorDialog.getColor(QColor(current), self, "Lineage Accent Color")
        if color.isValid():
            self._scene.tree.accent_color = color.name()
            self._scene.tree.touch()
            self.viewport().update()

    def _pan_to_node(self, node_id: str) -> None:
        item = self._scene._node_items.get(node_id)
        if item:
            self.ensureVisible(item, 80, 80)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = False
            if not self._pan_active: self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept(); return
        super().keyReleaseEvent(event)

    # ── Fit view ──────────────────────────────────────────────────────────────

    def fit_view(self) -> None:
        node_items = list(self._scene._node_items.values())      # F45: not scene.items()
        if not node_items: return
        from functools import reduce
        combined = reduce(
            lambda a, b: a.united(b),
            (i.mapToScene(i.boundingRect()).boundingRect() for i in node_items))
        margin = 60
        self.fitInView(combined.adjusted(-margin, -margin, margin, margin),
                       Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom_factor = self.transform().m11()
        self.viewport().update()

    # ── Export to PNG (full scene, no view state change) ─────────────────────

    def export_to_image(self, bg_color: str = "#111816",
                        watermark_text: str = "",
                        watermark_opacity: float = 0.35) -> Optional[QImage]:
        """
        Render ALL nodes and comments to a QImage at 2× resolution.
        Does NOT call auto_layout or fit_view — the live view is untouched.
        Rendering math: image_pixels = scene_units × scale (no DPR tricks).
        """
        all_items = [i for i in self._scene.items()
                     if isinstance(i, PlantNodeItem) or isinstance(i, CommentItem)]
        if not all_items:
            return None
        try:
            from functools import reduce
            combined = reduce(
                lambda a, b: a.united(b),
                (i.mapToScene(i.boundingRect()).boundingRect() for i in all_items))
            margin   = 100
            src_rect = combined.adjusted(-margin, -margin, margin, margin)

            label_col = YEAR_LABEL_W + 10          # px for the year-label strip
            scene_w   = max(1, int(src_rect.width()))
            scene_h   = max(1, int(src_rect.height()))
            total_w   = scene_w + label_col
            total_h   = scene_h

            EXPORT_SCALE = 2
            # Cap at 16 000 px per side
            max_dim = 16000
            s = min(EXPORT_SCALE,
                    max_dim / max(total_w, total_h, 1))

            out_w = max(1, int(total_w * s))
            out_h = max(1, int(total_h * s))
            lc_px = int(label_col * s)             # label strip width in pixels

            image = QImage(out_w, out_h, QImage.Format.Format_ARGB32)
            image.fill(QColor(bg_color))

            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

            # Scene content: target is pixel rect right of the label strip,
            # source is the scene bounding rect we computed above.
            target = QRectF(lc_px, 0, out_w - lc_px, out_h)
            self._scene.render(painter, target=target, source=src_rect)

            # Year label strip (drawn in pixel space, no scene scaling)
            painter.fillRect(QRectF(0, 0, lc_px, out_h), QColor(self._year_band_bg))
            painter.setPen(QPen(QColor(self._year_line_col), 1))
            painter.drawLine(lc_px, 0, lc_px, out_h)

            fs   = max(self._font_size, 16)  # year labels larger
            font = QFont(self._font_family, fs, QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QPen(QColor(self._year_text_col)))

            for year, scene_y in sorted(self._scene.year_rows.items()):
                # Convert scene Y to pixel Y
                iy = int((scene_y - src_rect.top()) * s)
                painter.drawText(QRectF(4, iy - 18, lc_px - 8, 36),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 str(year))
                painter.setPen(QPen(QColor(self._year_line_col), 1, Qt.PenStyle.DashLine))
                painter.drawLine(lc_px, iy, out_w, iy)
                painter.setPen(QPen(QColor(self._year_text_col)))

            # ── Watermark (bottom-right corner) ───────────────────────────────
            if watermark_text and watermark_opacity > 0:
                wm_text = watermark_text.strip()
                wm_font = QFont(self._font_family, max(12, int(14 * s)))
                wm_font.setBold(True)
                painter.setFont(wm_font)

                fm = painter.fontMetrics()
                margin_x = int(18 * s)
                margin_y = int(14 * s)
                text_w   = fm.horizontalAdvance(wm_text)
                text_h   = fm.height()

                # Semi-transparent dark pill background
                pill_pad = int(8 * s)
                pill_rect = QRectF(
                    out_w - text_w - margin_x - pill_pad * 2,
                    out_h - text_h - margin_y - pill_pad * 2,
                    text_w + pill_pad * 2,
                    text_h + pill_pad * 2,
                )
                pill_col = QColor(0, 0, 0, int(watermark_opacity * 160))
                painter.setBrush(QBrush(pill_col))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(pill_rect, 6, 6)

                # Text itself
                txt_col = QColor(255, 255, 255, int(watermark_opacity * 230))
                painter.setPen(QPen(txt_col))
                painter.drawText(
                    QRectF(
                        out_w - text_w - margin_x - pill_pad,
                        out_h - text_h - margin_y - pill_pad,
                        text_w + pill_pad,
                        text_h + pill_pad,
                    ),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    wm_text,
                )

            painter.end()
            return image
        except Exception as e:
            log.error("export_to_image: %s", e)
            return None

    # ── Drop ─────────────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
        else: super().dragEnterEvent(event)

    def dragMoveEvent(self, event): event.acceptProposedAction()

    def dropEvent(self, event):
        md = event.mimeData()
        sp = self.mapToScene(event.position().toPoint())
        item = self._scene.itemAt(sp, QTransform())
        if isinstance(item, PlantNodeItem):
            super().dropEvent(event); return
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if path.lower().endswith((".png",".jpg",".jpeg",".webp",".bmp",".gif")):
                    self._pending_drop_image = path
                    self._scene.add_plant_at.emit(sp.x(), sp.y())
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)

    # ── SVG Export ────────────────────────────────────────────────────────────

    def export_to_svg(self, filepath: str, title: str = "BreedingCharts") -> bool:
        """Render the full scene to an SVG file using QSvgGenerator.
        Returns True on success, False otherwise."""
        if not _SVG_AVAILABLE:
            log.error("export_to_svg: PyQt6.QtSvg not available — install PyQt6-Qt6-Svg")
            return False
        try:
            src_rect = self._scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
            if src_rect.isEmpty():
                return False

            gen = QSvgGenerator()
            gen.setFileName(filepath)
            gen.setTitle(title)
            gen.setDescription("Generated by BreedingCharts")
            gen.setSize(QSize(int(src_rect.width()), int(src_rect.height())))
            gen.setViewBox(QRect(0, 0, int(src_rect.width()), int(src_rect.height())))

            # Temporarily store current transform; render to SVG
            painter = QPainter(gen)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            self._scene.render(painter, QRectF(), src_rect)
            painter.end()
            log.info("SVG exported → %s", filepath)
            return True
        except Exception as e:
            log.error("export_to_svg: %s", e)
            return False

    # ── PDF Export ────────────────────────────────────────────────────────────

    def export_to_pdf(self, filepath: str, title: str = "BreedingCharts") -> bool:
        """Render the full scene to a PDF file using QPdfWriter.

        QPdfWriter is part of PyQt6.QtGui and is always available — no
        separate PyQt6-Qt6-PrintSupport package is required.

        The page is sized to match the lineage's own aspect ratio at a
        print-quality DPI rather than forced onto one fixed A3 sheet — a
        large tree squeezed onto a single fixed page via KeepAspectRatio
        scaling would come out too small to read even though every node was
        technically present on the page. Sizing the page to the content
        keeps everything at a legible, consistent scale regardless of how
        many generations/branches the lineage has.

        Returns True on success, False otherwise.
        """
        try:
            src_rect = self._scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
            if src_rect.isEmpty():
                return False

            from PyQt6.QtGui import QPdfWriter, QPageSize, QPageLayout
            from PyQt6.QtCore import QMarginsF, QSizeF

            writer = QPdfWriter(filepath)
            writer.setTitle(title)
            writer.setCreator("BreedingCharts")

            # 150 DPI keeps card text crisp without producing an
            # unreasonably large file; cap the physical page size at
            # 200 inches/side so an extreme lineage can't request an
            # absurd amount of memory from the PDF backend.
            dpi = 150.0
            max_inches = 200.0
            page_w_in = min(max_inches, max(1.0, src_rect.width()  / dpi))
            page_h_in = min(max_inches, max(1.0, src_rect.height() / dpi))

            writer.setResolution(int(dpi))
            page_size = QPageSize(QSizeF(page_w_in, page_h_in), QPageSize.Unit.Inch)
            layout = QPageLayout(page_size, QPageLayout.Orientation.Portrait,
                                  QMarginsF(0, 0, 0, 0))
            writer.setPageLayout(layout)

            painter = QPainter(writer)
            if not painter.isActive():
                log.error("export_to_pdf: QPainter could not start on QPdfWriter")
                return False

            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            target = QRectF(painter.viewport())
            self._scene.render(painter, target, src_rect,
                               Qt.AspectRatioMode.KeepAspectRatio)
            painter.end()
            log.info("PDF exported → %s", filepath)
            return True
        except Exception as e:
            log.error("export_to_pdf: %s", e)
            return False


def _bulk_wrap(fn):
    def w(self, *a, **k):
        self._bulk_move = True
        try:
            return fn(self, *a, **k)
        finally:
            self._bulk_move = False
            seen = set()
            for lst in self._node_edge_index.values():
                for e in lst:
                    if id(e) not in seen:
                        seen.add(id(e)); e.update_path()
    return w
LineageScene._do_auto_layout = _bulk_wrap(LineageScene._do_auto_layout)
LineageScene._do_auto_organize = _bulk_wrap(LineageScene._do_auto_organize)
