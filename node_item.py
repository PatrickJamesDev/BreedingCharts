"""
node_item.py — Plant node card.

Performance improvements over v1.1.0:
  • card_h is now a cached int (invalidated by refresh/apply_settings).
    boundingRect(), shape(), port helpers all called it every frame via
    _card_font_size() + scene() — now a single attribute lookup.
  • scene() is called ONCE at the top of _do_paint(); all per-frame scene
    reads use the local variable.
  • Role label is looked up in O(1) from scene._role_map (a dict maintained
    by _rebuild_edges) instead of an O(n) scan of all tree nodes on every frame.
  • Six QFont objects are cached and rebuilt only when font settings change
    (_fonts_dirty flag, reset by refresh/invalidate_caches), not created fresh
    on every paint call.
  • _palette_cache avoids repeated scene() + hasattr() on every paint.
  • apply_settings() in the scene no longer re-decodes all images unless
    img_scale_factor or show_images actually changed.
"""
from __future__ import annotations
import base64
import logging
from typing import TYPE_CHECKING, Optional

from PyQt6.QtWidgets import QGraphicsItem, QMenu, QGraphicsSceneContextMenuEvent
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPixmap, QPainterPath,
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QObject, pyqtSignal, QTimer

if TYPE_CHECKING:
    from PyQt6.QtGui import QFontMetrics
    from models import PlantNode

log = logging.getLogger(__name__)

# ── Layout constants ──────────────────────────────────────────────────────────
CARD_W        = 220
IMG_H         = 140          # base display area height (scaled by img_scale_factor)
IMG_SCALE     = 3            # load pixmap at 3× for crisp rendering
TEXT_PAD      = 10
PORT_R        = 8
PORT_HIT_R    = PORT_R * 3   # invisible hit zone — kept within boundingRect margin
CORNER_R      = 12
SHADOW_MARGIN = 22
HEADER_H      = 6

_TEXT_BLOCK_H = 132
CARD_H_IMG    = HEADER_H + IMG_H + 8 + _TEXT_BLOCK_H
CARD_H_PLAIN  = HEADER_H + 44 + _TEXT_BLOCK_H

NO_PEN = QPen(Qt.PenStyle.NoPen)

# ── Pre-built paint resources ─────────────────────────────────────────────────
# These immutable value objects are shared by every card.  Building them once at
# import time removes several QBrush/QColor allocations from each paint() call.
# Shadow layers are constant (brush, grow, y-offset) — identical for every card.
_SHADOW_BRUSHES = (
    (QBrush(QColor(0, 0, 0, 26)), 3, 2),
    (QBrush(QColor(0, 0, 0, 15)), 7, 4),
)
# Hover shadow layers — alpha is scaled by hover progress at paint time, so the
# QBrush for these is only built while a card is actually animating.
_HOVER_SHADOW_LAYERS = ((4, 3, 30), (10, 7, 18), (16, 11, 8))

# The accent header bar geometry is identical for every card (it depends only on
# CARD_W / HEADER_H / CORNER_R), so intersect the two paths once instead of on
# every paint.  QPainterPath is a value type; sharing a read-only path is safe.
_ACCENT_BAR = QPainterPath()
_ACCENT_BAR.addRoundedRect(
    QRectF(-CARD_W/2, 0, CARD_W, HEADER_H + CORNER_R), CORNER_R, CORNER_R)
_ACCENT_CLIP = QPainterPath()
_ACCENT_CLIP.addRect(QRectF(-CARD_W/2, 0, CARD_W, HEADER_H))
_ACCENT_BAR_FILL = _ACCENT_BAR.intersected(_ACCENT_CLIP)

# Placeholder image area — constant when a card has no photo.
_PLACEHOLDER_RECT = QRectF(-CARD_W/2 + TEXT_PAD, HEADER_H + 6,
                           CARD_W - TEXT_PAD*2, 44)

# Search-highlight pen/brush — identical for every highlighted card.  Built once
# at import time instead of allocating them on every paint frame.
_SEARCH_PEN = QPen(QColor("#ffe066"), 3, Qt.PenStyle.DashLine)
_SEARCH_PEN.setDashPattern([4.0, 3.0])
_SEARCH_BRUSH = QBrush(QColor(255, 224, 0, 22))

# Role-badge backgrounds — fixed colours independent of theme/settings.
_ROLE_BADGE_BRUSHES = {
    "mother": QBrush(QColor("#3a0808")),
    "father": QBrush(QColor("#08203a")),
}

# Photo-count badge on the image corner — fixed colours, one pair for all cards.
_PHOTO_BADGE_BRUSH = QBrush(QColor(0, 0, 0, 170))
_PHOTO_BADGE_PEN   = QPen(QColor(255, 255, 255, 220))

# Fallback for palette lookups that miss — shared, so the hot path never
# builds a throwaway QColor just to use as a dict.get() default.
_DEFAULT_QCOLOR = QColor("#ffffff")


def _lerp_qcolor(a: QColor, b: QColor, t: float) -> QColor:
    """Linear-interpolate two QColors — used to animate hover bg/border
    smoothly instead of snapping between two fixed colours."""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
        int(a.alpha() + (b.alpha() - a.alpha()) * t),
    )

_DEFAULT_PALETTE = {
    "node_bg":          "#1a2a1e",
    "node_border":      "#2e4a34",
    "node_name":        "#e8f5e8",
    "accent":           "#4ecb6a",
    "text_muted":       "#90b898",
    "year_bg":          "#3d2e00",
    "year_fg":          "#f0b84a",
    "tag_bg":           "#1a3a26",
    "tag_border":       "#3a7050",
    "tag_fg":           "#70d888",
    "placeholder":      "#1e2e22",
    "placeholder_icon": "#3a6040",
}


# ── Signals ───────────────────────────────────────────────────────────────────

class NodeSignals(QObject):
    moved            = pyqtSignal(str)
    move_start       = pyqtSignal(str)   # emitted on press — used to push undo BEFORE move
    edit_requested   = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    add_parent       = pyqtSignal(str)
    add_offspring    = pyqtSignal(str)
    image_changed    = pyqtSignal(str)
    connect_start    = pyqtSignal(str, str)   # (node_id, direction: "top"|"bottom")


# ── Node card ─────────────────────────────────────────────────────────────────


_STYLE_MEMO: dict = {}

def _shared_style(p, mother, father):
    """Pens/brushes/colours depend only on (palette, role colours): build once, share across all cards."""
    key = (id(p), mother, father)
    hit = _STYLE_MEMO.get(key)
    if hit is not None and hit[0] is p:
        return hit[1]
    from PyQt6.QtGui import QColor as _QColor
    qc = {k: _QColor(v) for k, v in p.items() if isinstance(v, str) and v.startswith("#")}
    pens = {
        "node_border": QPen(qc.get("node_border", _QColor("#2e4a34"))),
        "node_name": QPen(qc.get("node_name", _QColor("#e8f5e8"))),
        "text_muted": QPen(qc.get("text_muted", _QColor("#90b898"))),
        "year_fg": QPen(qc.get("year_fg", _QColor("#f0b84a"))),
        "tag_border": QPen(qc.get("tag_border", _QColor("#3a7050"))),
        "tag_fg": QPen(qc.get("tag_fg", _QColor("#70d888"))),
        "placeholder_icon": QPen(qc.get("placeholder_icon", _QColor("#3a6040"))),
        "port": QPen(qc.get("node_name", _QColor("#e8f5e8")), 1.5),
        "mother_role": QPen(QColor(mother)), "father_role": QPen(QColor(father)),
    }
    brushes = {
        "node_bg": QBrush(qc.get("node_bg", _QColor("#1a2a1e"))),
        "accent": QBrush(qc.get("accent", _QColor("#4ecb6a"))),
        "year_bg": QBrush(qc.get("year_bg", _QColor("#3d2e00"))),
        "tag_bg": QBrush(qc.get("tag_bg", _QColor("#1a3a26"))),
        "placeholder": QBrush(qc.get("placeholder", _QColor("#1e2e22"))),
    }
    if len(_STYLE_MEMO) > 16: _STYLE_MEMO.clear()
    _STYLE_MEMO[key] = (p, (qc, pens, brushes))
    return _STYLE_MEMO[key][1]


class PlantNodeItem(QGraphicsItem):

    def __init__(self, node: "PlantNode", signals: NodeSignals):
        super().__init__()
        self.node    = node
        self.signals = signals
        self._pixmap: Optional[QPixmap] = None
        self._pixmap_dirty: bool = True    # True → needs (re)decode
        self._decode_pending: bool = False # True → scene's thread pool owns the decode;
                                           # paint() shows placeholder, does NOT decode
        self._hovered          = False
        self._search_highlight = False

        # ── Hover elevation animation ────────────────────────────────────
        # A tiny self-driving timer (only active while transitioning, not
        # persistent) lerps a 0→1 progress value used to smoothly grow the
        # drop shadow and border on hover/un-hover, instead of the flat
        # on/off state swap. Cheap: at most a couple of items are ever
        # mid-transition at once since it's driven by mouse hover.
        self._hover_progress: float = 0.0
        self._hover_target: float = 0.0
        self._hover_timer: Optional[QTimer] = None

        # ── Per-item caches — invalidated by invalidate_caches() ────────────
        self._card_h_cache: Optional[int] = None
        self._palette_cache: Optional[dict] = None
        # Pre-built QColor objects for every key in the palette.
        # QColor(hex_string) allocates and parses on every call — caching them
        # here eliminates ~25 allocations per card per paint frame.
        self._qcolor_cache: Optional[dict] = None
        # Pre-built QPen/QBrush for palette keys — avoids allocating them in
        # paint() on every frame.  Rebuilt together with _palette_cache.
        self._pen_cache:   Optional[dict] = None
        self._brush_cache: Optional[dict] = None
        # Accent colour for this node's custom colour (keyed by the hex string).
        self._accent_cache:     Optional[QColor] = None
        self._accent_cache_key: str = ""
        # Port fill brushes — derived from the accent colour; recomputed only
        # when the accent changes instead of allocating QColor/QBrush per paint.
        self._port_brush:         Optional[QBrush] = None
        self._port_brush_connect: Optional[QBrush] = None
        # Cached bounding rect / picking shape — both depend only on card_h.
        self._br_cache:    Optional[QRectF] = None
        self._br_cache_h:  int = -1
        self._shape_cache: Optional[QPainterPath] = None
        self._shape_cache_h: int = -1
        # Cached rounded image clip path — depends only on the effective image h.
        self._img_clip_cache:   Optional[QPainterPath] = None
        self._img_clip_cache_h: int = -1

        # Fonts: QFont creation is not free; rebuild only when settings change.
        self._font_name:        Optional[QFont] = None
        self._font_species:     Optional[QFont] = None
        self._font_badge:       Optional[QFont] = None
        self._font_year:        Optional[QFont] = None
        self._font_tag:         Optional[QFont] = None
        self._font_counter:     Optional[QFont] = None
        self._font_placeholder: QFont = QFont("Segoe UI", 18)
        # Cached QFontMetrics alongside each font — avoids painter.fontMetrics()
        # (which allocates) on every paint call.
        self._fm_name:    Optional["QFontMetrics"] = None
        self._fm_species: Optional["QFontMetrics"] = None
        self._fm_badge:   Optional["QFontMetrics"] = None
        self._fm_year:    Optional["QFontMetrics"] = None
        self._fm_tag:     Optional["QFontMetrics"] = None
        self._fonts_dirty: bool = True
        self._last_cf:  str = ""
        self._last_cfs: int = 0

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,            True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable,         True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setAcceptDrops(True)
        self.setAcceptHoverEvents(True)
        self.setPos(node.x, node.y)
        # Image decode is deferred: the scene calls _enqueue_decode(item) after
        # addItem(), which sets _decode_pending=True and submits a QRunnable.
        # Until the worker finishes, paint() shows the green-leaf placeholder.

    # ── Cache management ──────────────────────────────────────────────────────

    def invalidate_caches(self) -> None:
        """Called by the scene when font/scale/theme settings change."""
        self._card_h_cache  = None
        self._palette_cache = None
        self._qcolor_cache  = None
        self._pen_cache     = None
        self._brush_cache   = None
        self._accent_cache  = None
        self._accent_cache_key = ""
        self._port_brush         = None
        self._port_brush_connect = None
        # Geometry caches are keyed by card_h / image height, so a height change
        # invalidates them lazily; clearing here also frees the memory promptly.
        self._br_cache         = None
        self._br_cache_h       = -1
        self._shape_cache      = None
        self._shape_cache_h    = -1
        self._img_clip_cache   = None
        self._img_clip_cache_h = -1
        self._fonts_dirty   = True

    def _get_cached_palette(self) -> dict:
        if self._palette_cache is None:
            sc = self.scene()
            p  = (sc.theme_palette
                  if sc is not None and hasattr(sc, "theme_palette")
                  else _DEFAULT_PALETTE)
            mo = getattr(sc, "mother_line_color", "#e87060") if sc is not None else "#e87060"
            fa = getattr(sc, "father_line_color", "#60a0e8") if sc is not None else "#60a0e8"
            st = _shared_style(p, mo, fa)
            self._palette_cache = p
            self._qcolor_cache, self._pen_cache, self._brush_cache = st
        return self._palette_cache

    def _qc(self, key: str) -> "QColor":
        """Return the pre-built QColor for a palette key (fast path)."""
        if self._qcolor_cache is None:
            self._get_cached_palette()
        # _DEFAULT_QCOLOR, not QColor("#ffffff"): a default argument to
        # dict.get() is evaluated on every call, so the literal would re-parse
        # the hex string even on the (always taken) hit path.
        return self._qcolor_cache.get(key, _DEFAULT_QCOLOR)

    def _ensure_fonts(self, cf: str, cfs: int) -> None:
        """Rebuild cached QFont + QFontMetrics objects when font params change."""
        if not self._fonts_dirty and cf == self._last_cf and cfs == self._last_cfs:
            return
        from PyQt6.QtGui import QFontMetrics as _FM
        self._font_name    = QFont(cf, cfs, QFont.Weight.Bold)
        fi = QFont(cf, max(6, cfs - 2)); fi.setItalic(True)
        self._font_species = fi
        self._font_badge   = QFont(cf, max(6, cfs - 2), QFont.Weight.Bold)
        self._font_year    = QFont(cf, max(6, cfs - 1), QFont.Weight.Bold)
        self._font_tag     = QFont(cf, max(6, cfs - 2))
        self._font_counter = QFont(cf, max(6, cfs - 3))
        # Build QFontMetrics alongside — avoids painter.fontMetrics() in hot paint path
        self._fm_name    = _FM(self._font_name)
        self._fm_species = _FM(self._font_species)
        self._fm_badge   = _FM(self._font_badge)
        self._fm_year    = _FM(self._font_year)
        self._fm_tag     = _FM(self._font_tag)
        self._last_cf    = cf
        self._last_cfs   = cfs
        self._fonts_dirty = False

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _effective_img_h(self) -> int:
        """Return IMG_H scaled by the scene's img_scale_factor setting."""
        sc = self.scene()
        if sc is not None and hasattr(sc, "img_scale_factor"):
            return max(40, int(IMG_H * sc.img_scale_factor))
        return IMG_H

    def _card_font(self) -> str:
        sc = self.scene()
        if sc is not None and hasattr(sc, "card_font_family"):
            return sc.card_font_family
        return "Georgia"

    def _card_font_size(self) -> int:
        sc = self.scene()
        if sc is not None and hasattr(sc, "card_font_size"):
            return max(6, sc.card_font_size)
        return 10

    @property
    def card_h(self) -> int:
        """Cached card height — computed from actual font sizes to prevent cutoff."""
        if self._card_h_cache is not None:
            return self._card_h_cache
        cfs = self._card_font_size()

        # --- Per-element height estimates (font-size aware) ---
        name_h    = max(26, cfs + 14)       # 1–2 line name, never clips
        species_h = max(20, cfs + 8)  if self.node.species else 0
        role_h    = max(20, cfs + 8)  if any(
            p in (self.node.parent_roles or {}).values()
            for p in ("mother", "Mother", "father", "Father")) else 0
        year_h    = max(20, cfs + 8)  if self.node.year else 0
        tags_h    = max(20, cfs + 7)  if self.node.tags else 0
        bottom_pad = 10

        text_block = name_h + species_h + role_h + year_h + tags_h + bottom_pad
        text_block = max(text_block, 80 + cfs * 4)   # enforce a minimum

        if self.node.has_photo:
            result = HEADER_H + self._effective_img_h() + 10 + text_block
        else:
            result = HEADER_H + 48 + text_block

        self._card_h_cache = result
        return result

    def node_color(self) -> str:
        p = self._get_cached_palette()
        return self.node.color or p.get("accent", "#4ecb6a")

    def boundingRect(self) -> QRectF:
        # Qt calls this on every scene query (pan/zoom/hit-test).  The result only
        # changes when card_h changes, so cache it and compare by height.
        ch = self.card_h
        if self._br_cache is not None and self._br_cache_h == ch:
            return self._br_cache
        m = max(SHADOW_MARGIN + PORT_R + 2, PORT_HIT_R + 4)
        r = QRectF(-CARD_W/2 - m, -m, CARD_W + m*2, ch + m*2)
        self._br_cache   = r
        self._br_cache_h = ch
        return r

    def shape(self) -> QPainterPath:
        """Shape used for mouse picking — card body PLUS both port hit zones."""
        ch = self.card_h
        if self._shape_cache is not None and self._shape_cache_h == ch:
            return self._shape_cache
        p  = QPainterPath()
        body = QRectF(-CARD_W/2, HEADER_H, CARD_W, ch - HEADER_H)
        p.addRoundedRect(body, CORNER_R, CORNER_R)
        p.addEllipse(QPointF(0,  0),  PORT_HIT_R, PORT_HIT_R)
        p.addEllipse(QPointF(0, ch),  PORT_HIT_R, PORT_HIT_R)
        self._shape_cache   = p
        self._shape_cache_h = ch
        return p

    # ── Port hit tests (in local coordinates) ─────────────────────────────────

    def _hit_top_port(self, local: QPointF) -> bool:
        return local.x()**2 + local.y()**2 <= PORT_HIT_R**2

    def _hit_bottom_port(self, local: QPointF) -> bool:
        dy = local.y() - self.card_h
        return local.x()**2 + dy**2 <= PORT_HIT_R**2

    def top_port_scene(self)    -> QPointF: return self.mapToScene(QPointF(0, 0))
    def bottom_port_scene(self) -> QPointF: return self.mapToScene(QPointF(0, self.card_h))

    # ── Image ─────────────────────────────────────────────────────────────────

    def _reload_pixmap(self) -> None:
        """Decode the current image for on-card display.  Called eagerly when
        settings change, and lazily (via _ensure_pixmap) when the card first
        becomes visible.

        Renders from node.thumb (a small pre-scaled JPEG, always resident)
        rather than the full-resolution original — the full-res copy is kept
        on disk but normal card rendering never needs to touch it. Falls
        back to the full image only if a node somehow has no thumb yet but
        does have full-res data already loaded in memory (defensive; should
        be rare once everything's migrated)."""
        img = self.node.thumb or (self.node.images[0] if self.node.images else None)
        if not img:
            self._pixmap = None
            self._pixmap_dirty = False
            return
        try:
            raw = base64.b64decode(img)
            pix = QPixmap()
            pix.loadFromData(raw)
            if pix.isNull():
                self._pixmap = None
            else:
                target_w = (CARD_W - TEXT_PAD*2) * IMG_SCALE
                target_h = self._effective_img_h() * IMG_SCALE
                self._pixmap = pix.scaled(
                    target_w, target_h,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
        except Exception as e:
            log.warning("reload_pixmap: %s", e)
            self._pixmap = None
        self._pixmap_dirty = False

    def _ensure_pixmap(self) -> None:
        """Decode image when dirty — but only if NOT already being decoded by
        the scene's thread-pool worker (_decode_pending=True).  When pending,
        paint() shows the placeholder; the worker calls _reload_pixmap() and
        triggers a repaint when done.  This path only fires for standalone
        refreshes (e.g. user drops a new image) where no queue is involved."""
        if self._pixmap_dirty and not self._decode_pending:
            self._reload_pixmap()

    def refresh(self) -> None:
        self._pixmap_dirty   = True
        self._pixmap         = None
        self._decode_pending = False  # cancel queued decode; next paint reloads immediately
        self._card_h_cache   = None   # image presence may have changed
        self._fonts_dirty    = True
        self._palette_cache  = None
        self._pen_cache      = None
        self._brush_cache    = None
        # Accent-derived state: colour/role/theme may have changed, so drop the
        # cached QColor and the port brushes built from it.  They are rebuilt
        # lazily in _do_paint() when the accent key no longer matches.
        self._accent_cache     = None
        self._accent_cache_key = ""
        self._port_brush         = None
        self._port_brush_connect = None
        self._br_cache       = None
        self._br_cache_h     = -1
        self._shape_cache    = None
        self._shape_cache_h  = -1
        self._img_clip_cache = None
        self._img_clip_cache_h = -1
        self.prepareGeometryChange()
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paint(self, painter: QPainter, option, widget=None) -> None:
        try:   self._do_paint(painter)
        except Exception as e: log.error("paint: %s", e)

    def _do_paint(self, painter: QPainter) -> None:  # noqa: C901
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # ── ONE scene() call for the entire paint method ─────────────────────
        sc = self.scene()
        ch = self.card_h   # uses cache → no scene() calls

        # Card font (read scene attrs directly; sc is already a local)
        cf  = (sc.card_font_family if sc is not None and hasattr(sc, "card_font_family")
               else "Georgia")
        cfs = (max(6, sc.card_font_size) if sc is not None and hasattr(sc, "card_font_size")
               else 10)
        self._ensure_fonts(cf, cfs)  # no-op unless dirty

        # ── Role lookup: O(1) from scene._role_map ───────────────────────────
        # _role_map is maintained by LineageScene._rebuild_edges()
        role = ""
        if sc is not None and hasattr(sc, "_role_map"):
            role = sc._role_map.get(self.node.id, "")

        # Accent colour — node's own colour, optionally role-overridden.
        # Cached by key because this runs on every paint and QColor(hex) parsing
        # is comparatively expensive.
        color_code = bool(sc is not None and getattr(sc, "color_code_lines", False))
        m_col = getattr(sc, "mother_line_color", "#e87060") if sc is not None else "#e87060"
        f_col = getattr(sc, "father_line_color", "#60a0e8") if sc is not None else "#60a0e8"
        acc_key = f"{self.node.color}|{role}|{color_code}|{m_col}|{f_col}"
        if self._accent_cache is not None and self._accent_cache_key == acc_key:
            accent = self._accent_cache
        else:
            accent = QColor(self.node.color) if self.node.color else self._qc("accent")
            if color_code:
                if role in ("mother", "Mother"):
                    accent = QColor(m_col)
                elif role in ("father", "Father"):
                    accent = QColor(f_col)
            self._accent_cache     = accent
            self._accent_cache_key = acc_key
            # Port fill brushes — derived from the accent colour.  Built here
            # (not in paint) so the two QColor/QBrush pairs are only allocated
            # when the accent actually changes.  "connect" is the lighter
            # highlight used while connect-mode is active.
            _port_fill = QColor(accent)
            _port_fill.setAlpha(220)
            self._port_brush         = QBrush(_port_fill)
            self._port_brush_connect = QBrush(QColor(accent).lighter(150))

        selected  = self.isSelected()
        card_rect = QRectF(-CARD_W/2, 0, CARD_W, ch)

        # ── Soft drop shadow ──────────────────────────────────────────────
        # Manual layered-rect approximation rather than QGraphicsDropShadowEffect:
        # that effect forces an offscreen render pass per item, which adds up
        # fast with hundreds of cards on canvas. A small ambient shadow is
        # always present (depth cue at rest); hover/selection smoothly adds
        # an extra "lift" driven by _hover_progress so the card doesn't just
        # snap into an elevated state.
        hp = self._hover_progress
        if selected and hp < 1.0:
            hp = 1.0
        painter.setPen(NO_PEN)
        # Resting shadow: module-level brushes — zero allocations per paint.
        for brush, grow, y_off in _SHADOW_BRUSHES:
            painter.setBrush(brush)
            painter.drawRoundedRect(
                card_rect.adjusted(-grow, -grow + y_off, grow, grow + y_off),
                CORNER_R + grow, CORNER_R + grow)
        # Hover/selected shadow: alpha scales with hover progress, so only this
        # transient branch allocates (at most a couple of cards animate at once).
        if hp > 0.001:
            for grow, y_off, alpha in _HOVER_SHADOW_LAYERS:
                g, yo, a = grow * hp, y_off * hp, int(alpha * hp)
                if a <= 0:
                    continue
                painter.setBrush(QBrush(QColor(0, 0, 0, a)))
                painter.drawRoundedRect(
                    card_rect.adjusted(-g, -g + yo, g, g + yo),
                    CORNER_R + g, CORNER_R + g)

        if selected:
            bg = QColor(accent).darker(300)
            border_col = accent; border_w = 2.5
        elif hp > 0.001:
            base_bg     = self._qc("node_bg")
            base_border = self._qc("node_border")
            bg          = _lerp_qcolor(base_bg, base_bg.lighter(112), hp)
            border_col  = _lerp_qcolor(base_border, base_border.lighter(130), hp)
            border_w    = 1.0 + 0.5 * hp
        else:
            bg = None   # signal "use cached rest-state pen/brush"

        if bg is None:
            # Resting state — reuse pre-built pen/brush (no allocations).
            pen_cache   = self._pen_cache   or {}
            brush_cache = self._brush_cache or {}
            painter.setPen(pen_cache.get("node_border")
                           or QPen(self._qc("node_border"), 1.0))
            painter.setBrush(brush_cache.get("node_bg")
                             or QBrush(self._qc("node_bg")))
        else:
            painter.setPen(QPen(border_col, border_w))
            painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(card_rect, CORNER_R, CORNER_R)

        # Accent bar — geometry is constant for every card; reuse the shared path.
        painter.setPen(NO_PEN); painter.setBrush(QBrush(accent))
        painter.drawPath(_ACCENT_BAR_FILL)

        # Lazy-decode image on first paint
        self._ensure_pixmap()

        # Image
        show_img = sc is None or not hasattr(sc, "show_images") or sc.show_images
        y = float(HEADER_H + 6)
        eff_img_h = self._effective_img_h()
        img_rect = QRectF(-CARD_W/2 + TEXT_PAD, y, CARD_W - TEXT_PAD*2, eff_img_h)

        if self._pixmap and show_img:
            # Reuse a cached clip path — its geometry depends only on eff_img_h.
            clip2 = self._img_clip_cache
            if clip2 is None or self._img_clip_cache_h != eff_img_h:
                clip2 = QPainterPath()
                clip2.addRoundedRect(img_rect, 8, 8)
                self._img_clip_cache   = clip2
                self._img_clip_cache_h = eff_img_h
            painter.save()
            painter.setClipPath(clip2)
            pw, ph = self._pixmap.width(), self._pixmap.height()
            if pw > 0 and ph > 0:
                sc2 = max(img_rect.width()/pw, img_rect.height()/ph)
                dw, dh = pw*sc2, ph*sc2
                dx  = img_rect.x() + (img_rect.width()  - dw)/2
                dy_ = img_rect.y() + (img_rect.height() - dh)/2
                painter.drawPixmap(QRectF(dx, dy_, dw, dh), self._pixmap,
                                   QRectF(self._pixmap.rect()))
            if self.node.photo_count > 1:
                badge = QRectF(img_rect.right()-30, img_rect.bottom()-16, 28, 14)
                painter.setClipping(False)
                painter.setBrush(_PHOTO_BADGE_BRUSH)
                painter.setPen(NO_PEN)
                painter.drawRoundedRect(badge, 4, 4)
                painter.setPen(_PHOTO_BADGE_PEN)
                painter.setFont(self._font_counter)
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter,
                                 f"+{self.node.photo_count-1}")
            painter.restore()
            y += eff_img_h + 10
        else:
            ph_rect = _PLACEHOLDER_RECT
            painter.setBrush(self._brush_cache.get("placeholder")
                             or QBrush(self._qc("placeholder")))
            painter.setPen(NO_PEN)
            painter.drawRoundedRect(ph_rect, 6, 6)
            painter.setPen(self._pen_cache.get("placeholder_icon")
                           or QPen(self._qc("placeholder_icon")))
            painter.setFont(self._font_placeholder)
            painter.drawText(ph_rect, Qt.AlignmentFlag.AlignCenter, "🌿")
            y += 52

        tx, tw = -CARD_W/2 + TEXT_PAD, CARD_W - TEXT_PAD*2

        # ── Name — measure actual height, allow 2-line wrap ──────────────────
        painter.setPen(self._pen_cache["node_name"])
        painter.setFont(self._font_name)
        fm_name   = self._fm_name    # cached — no allocation needed
        name_line = fm_name.height()
        name_h    = max(name_line + 10, name_line * 2 + 4)   # room for 2 lines
        name_rect = QRectF(tx, y, tw, name_h)
        painter.drawText(name_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop |
                         Qt.TextFlag.TextWordWrap,
                         self.node.name or "Unnamed")
        y += name_h

        # ── Species — measured height ─────────────────────────────────────────
        if self.node.species:
            painter.setFont(self._font_species)
            fm_sp  = self._fm_species   # cached
            sp_h   = fm_sp.height() + 4
            sp_rect = QRectF(tx, y, tw, sp_h)
            painter.setPen(self._pen_cache["text_muted"])
            sp_text = self.node.species
            # Elide if still too wide for one line
            sp_text = fm_sp.elidedText(sp_text, Qt.TextElideMode.ElideRight, int(tw))
            painter.drawText(sp_rect,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             sp_text)
            y += sp_h

        # Mother/Father role badge — role already read above from O(1) map
        if role in ("mother", "Mother"):
            role_label = "♀ Mother"
        elif role in ("father", "Father"):
            role_label = "♂ Father"
        else:
            role_label = ""
        if role_label:
            if "Mother" in role_label:
                rl_col = getattr(sc, "mother_line_color", "#e87060") if sc else "#e87060"
            else:
                rl_col = getattr(sc, "father_line_color", "#60a0e8") if sc else "#60a0e8"
            painter.setFont(self._font_badge)
            fm_badge = self._fm_badge  # cached
            badge_h  = fm_badge.height() + 6
            badge_w  = max(72, fm_badge.horizontalAdvance(role_label) + 16)
            badge    = QRectF(tx, y, badge_w, badge_h)
            role_key = "mother" if "Mother" in role_label else "father"
            painter.setBrush(_ROLE_BADGE_BRUSHES[role_key])
            painter.setPen(NO_PEN)
            painter.drawRoundedRect(badge, badge_h / 2, badge_h / 2)
            painter.setPen(self._pen_cache.get(role_key + "_role")
                           or QPen(QColor(rl_col)))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, role_label)
            y += badge_h + 4

        # Year badge
        if self.node.year:
            painter.setFont(self._font_year)
            fm_year  = self._fm_year   # cached
            badge_h  = fm_year.height() + 6
            badge_w  = max(52, fm_year.horizontalAdvance(str(self.node.year)) + 16)
            badge    = QRectF(tx, y, badge_w, badge_h)
            painter.setBrush(self._brush_cache["year_bg"])
            painter.setPen(NO_PEN)
            painter.drawRoundedRect(badge, badge_h / 2, badge_h / 2)
            painter.setPen(self._pen_cache["year_fg"])
            painter.setFont(self._font_year)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(self.node.year))
            y += badge_h + 4

        # Tags — wrap to a second row if needed
        if self.node.tags:
            painter.setFont(self._font_tag)
            fm_tag = self._fm_tag   # cached — avoids allocation in hot path
            tag_h  = fm_tag.height() + 6
            txp    = tx
            for tag in self.node.tags[:6]:
                lbl  = fm_tag.elidedText(tag, Qt.TextElideMode.ElideRight, 110)
                tw2  = fm_tag.horizontalAdvance(lbl) + 14
                tw2  = min(tw2, 110)
                if txp + tw2 > CARD_W/2 - TEXT_PAD:
                    break
                tr = QRectF(txp, y, tw2, tag_h)
                painter.setBrush(self._brush_cache["tag_bg"])
                painter.setPen(self._pen_cache["tag_border"])
                painter.drawRoundedRect(tr, tag_h / 2, tag_h / 2)
                painter.setPen(self._pen_cache["tag_fg"])
                painter.drawText(tr, Qt.AlignmentFlag.AlignCenter, lbl)
                txp += tw2 + 4

        # ── Search highlight ring ─────────────────────────────────────────────
        if self._search_highlight:
            painter.setPen(_SEARCH_PEN)
            painter.setBrush(_SEARCH_BRUSH)
            painter.drawRoundedRect(card_rect.adjusted(-3, -3, 3, 3),
                                    CORNER_R + 3, CORNER_R + 3)

        # Ports — semi-transparent accent with white outline.
        # Brushes are cached on the item (rebuilt only when the accent changes);
        # only the connect-mode switch picks between the two.
        in_connect = sc is not None and getattr(sc, "connect_mode", False)
        port_brush = (self._port_brush_connect if in_connect else self._port_brush)
        if port_brush is None:
            # Defensive fallback (e.g. accent cache cleared by a subclass).
            _pc = QColor(accent); _pc.setAlpha(220)
            port_brush = QBrush(_pc.lighter(150) if in_connect else _pc)
        painter.setBrush(port_brush)
        painter.setPen(self._pen_cache.get("port")
                       or QPen(self._qc("node_name"), 1.5))
        painter.drawEllipse(QPointF(0,  0),  PORT_R, PORT_R)
        painter.drawEllipse(QPointF(0, ch),  PORT_R, PORT_R)

    def _role_label(self) -> str:
        """Return '♀ Mother' / '♂ Father' / '' using the scene's O(1) role map.
        Kept for backwards compatibility; paint now reads _role_map directly."""
        sc = self.scene()
        if sc is None or not hasattr(sc, "_role_map"):
            return ""
        role = sc._role_map.get(self.node.id, "")
        if role in ("mother", "Mother"): return "♀ Mother"
        if role in ("father", "Father"): return "♂ Father"
        return ""

    # ── Events ────────────────────────────────────────────────────────────────

    def hoverEnterEvent(self, event):
        self._hovered = True
        self._start_hover_anim(1.0)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self._start_hover_anim(0.0)
        super().hoverLeaveEvent(event)

    def _start_hover_anim(self, target: float) -> None:
        self._hover_target = target
        if self._hover_timer is None:
            self._hover_timer = QTimer()
            self._hover_timer.setInterval(16)   # ~60fps
            self._hover_timer.timeout.connect(self._tick_hover_anim)
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _tick_hover_anim(self) -> None:
        diff = self._hover_target - self._hover_progress
        if abs(diff) < 0.02:
            self._hover_progress = self._hover_target
            self._hover_timer.stop()
        else:
            self._hover_progress += diff * 0.30   # ease toward target
        self.update()

    def mouseDoubleClickEvent(self, event):
        self.signals.edit_requested.emit(self.node.id)
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            local = event.pos()
            sc = self.scene()
            already_connecting = sc is not None and getattr(sc, "connect_mode", False)
            if not already_connecting:
                if self._hit_top_port(local):
                    self.signals.connect_start.emit(self.node.id, "top")
                    event.accept(); return
                if self._hit_bottom_port(local):
                    self.signals.connect_start.emit(self.node.id, "bottom")
                    event.accept(); return
            if not already_connecting:
                self.signals.move_start.emit(self.node.id)
        super().mousePressEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.node.x = self.pos().x()
            self.node.y = self.pos().y()
            self.signals.moved.emit(self.node.id)
        return super().itemChange(change, value)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent):
        try:
            menu = QMenu()
            a_edit      = menu.addAction("✏️  Edit Details")
            menu.addSeparator()
            a_parent    = menu.addAction("⬆️  Add / Connect Parent")
            a_offspring = menu.addAction("⬇️  Add Offspring")
            a_connect   = menu.addAction("🔗  Connect to Another Node…")
            menu.addSeparator()
            a_delete    = menu.addAction("🗑️  Delete Node")
            chosen = menu.exec(event.screenPos())
            if chosen == a_edit:        self.signals.edit_requested.emit(self.node.id)
            elif chosen == a_parent:    self.signals.add_parent.emit(self.node.id)
            elif chosen == a_offspring: self.signals.add_offspring.emit(self.node.id)
            elif chosen == a_connect:   self.signals.connect_start.emit(self.node.id, "bottom")
            elif chosen == a_delete:    self.signals.delete_requested.emit(self.node.id)
        except Exception as e:
            log.error("contextMenuEvent: %s", e)

    # ── Image drop ────────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
        else: event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
        else: event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if path.lower().endswith((".png",".jpg",".jpeg",".webp",".bmp",".gif")):
                    self._set_image_from_path(path)
                    event.acceptProposedAction(); return
        if md.hasImage():
            img = md.imageData()
            if img is not None:
                self._set_image_from_qimage(img)
                event.acceptProposedAction()

    def _ensure_images_loaded(self) -> None:
        """Before mutating node.images, make sure it reflects the node's true
        current state. If this node's photos haven't been loaded this
        session (the common case — cards render from .thumb, not .images),
        node.images is [] even though the node may already have other
        photos on disk; loading first stops us from clobbering them when we
        only mean to set/replace the main one."""
        sc = self.scene()
        tree = getattr(sc, "tree", None)
        if tree is not None and self.node.id in tree.nodes:
            import lineage_images
            lineage_images.populate_node_images(tree, getattr(sc, "filepath", None), self.node.id)

    def _mark_image_touched(self) -> None:
        sc = self.scene()
        tree = getattr(sc, "tree", None)
        if tree is not None:
            import lineage_images
            lineage_images.mark_images_authoritative(tree, self.node.id)

    def _set_image_from_path(self, path: str) -> None:
        try:
            with open(path, "rb") as fh: raw = fh.read()
            b64 = base64.b64encode(raw).decode()
            self._ensure_images_loaded()
            if not self.node.images: self.node.images.append(b64)
            else:                    self.node.images[0] = b64
            self.node.photo_count = len(self.node.images)
            import image_utils
            self.node.thumb = image_utils.make_thumb_from_bytes(raw)
            self._mark_image_touched()
            self._card_h_cache = None   # image presence changed
            self._reload_pixmap(); self.prepareGeometryChange(); self.update()
            self.signals.image_changed.emit(self.node.id)
        except Exception as e: log.error("_set_image_from_path: %s", e)

    def _set_image_from_qimage(self, qimage) -> None:
        try:
            from PyQt6.QtCore import QBuffer, QByteArray
            buf = QByteArray(); qbuf = QBuffer(buf)
            qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
            ok = qimage.save(qbuf, *__import__("image_utils").photo_format(qimage)); qbuf.close()
            if ok and buf.size() > 0:
                b64 = base64.b64encode(bytes(buf)).decode()
                self._ensure_images_loaded()
                if not self.node.images: self.node.images.append(b64)
                else:                    self.node.images[0] = b64
                self.node.photo_count = len(self.node.images)
                import image_utils
                self.node.thumb = image_utils.make_thumb_from_qimage(qimage)
                self._mark_image_touched()
                self._card_h_cache = None   # image presence changed
                self._reload_pixmap(); self.prepareGeometryChange(); self.update()
                self.signals.image_changed.emit(self.node.id)
        except Exception as e: log.error("_set_image_from_qimage: %s", e)
