"""
settings.py — Visual settings, keybinds, card styling, undo persistence.

New features:
  • ui_scale: independent DPI-like scale multiplier (1.0–3.0)
  • img_scale_factor: multiplier for image display area inside cards (1.0–3.0)
  • card_border_width, card_corner_radius, card_border_color_override
  • font_family: choice from 6 clean/scientific fonts
  • font_size: 8–30
  • default font_size = 13 (was 12, user wants 22 out of box so set to 22)
  • undo_history: persisted as JSON between sessions (up to 50 states)
  • window_maximized: remember window state
  • props_panel_visible: optional properties panel (off by default)
"""
from __future__ import annotations
import json, logging, os, sys
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional, List


def _get_writable_dir() -> str:
    """Return the writable config directory.

    PyInstaller --onefile extracts to a read-only temp folder at runtime;
    sys.executable always points to the actual .exe, so its parent is safe
    for writing.  In source mode we use the directory of this file.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QComboBox, QCheckBox, QDialogButtonBox,
    QWidget, QFrame, QTabWidget, QKeySequenceEdit, QScrollArea, QSpinBox,
    QDoubleSpinBox, QColorDialog, QSlider,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QColor

from ui_common import FadeDialog

log = logging.getLogger(__name__)

# ── Colour palettes ───────────────────────────────────────────────────────────

THEMES: dict[str, dict] = {
    "dark": {
        "label":            "🌑  Dark",
        "canvas_bg":        "#111816",
        "canvas_dot":       "#1e2e24",
        "app_bg":           "#111816",
        "panel_bg":         "#182018",
        "panel_bg2":        "#1c2a1c",
        "border":           "#2a4030",
        "border2":          "#223028",
        "text":             "#e8f5e8",
        "text_muted":       "#90b898",
        "text_header":      "#6ee880",
        "accent":           "#4ecb6a",
        "btn_bg":           "#1e3028",
        "btn_hover":        "#284038",
        "btn_border":       "#3a5a40",
        "btn_disabled":     "#3a4a3c",
        "input_bg":         "#111816",
        "sel_bg":           "#2a6040",
        "tab_bg":           "#141e14",
        "edge_color":       "#4a7a58",
        "scrollbar":        "#304838",
        "status_fg":        "#70b880",
        "node_bg":          "#1a2a1e",
        "node_border":      "#2e4a34",
        "node_name":        "#e8f5e8",
        "year_bg":          "#3d2e00",
        "year_fg":          "#f0b84a",
        "year_band_bg":     "#0e1610",
        "year_band_line":   "#2a4030",
        "year_band_text":   "#6ee880",
        "tag_bg":           "#1a3a26",
        "tag_border":       "#3a7050",
        "tag_fg":           "#70d888",
        "comment_bg":       "#2a2400",
        "comment_border":   "#6a5800",
        "comment_text":     "#f0d870",
        "placeholder":      "#1e2e22",
        "placeholder_icon": "#3a6040",
    },
    "light": {
        "label":            "☀️  Light",
        "canvas_bg":        "#ffffff",
        "canvas_dot":       "#e0e0e0",
        "app_bg":           "#f5f5f5",
        "panel_bg":         "#ffffff",
        "panel_bg2":        "#f8f8f8",
        "border":           "#d0d0d0",
        "border2":          "#e8e8e8",
        "text":             "#1a1a1a",
        "text_muted":       "#555555",
        "text_header":      "#1a6040",
        "accent":           "#1a7a42",
        "btn_bg":           "#eeeeee",
        "btn_hover":        "#dde8de",
        "btn_border":       "#aaaaaa",
        "btn_disabled":     "#bbbbbb",
        "input_bg":         "#ffffff",
        "sel_bg":           "#a8d8b8",
        "tab_bg":           "#eeeeee",
        "edge_color":       "#1a7a42",
        "scrollbar":        "#b0b0b0",
        "status_fg":        "#2a6040",
        "node_bg":          "#ffffff",
        "node_border":      "#cccccc",
        "node_name":        "#111111",
        "year_bg":          "#fff3c0",
        "year_fg":          "#7a4800",
        "year_band_bg":     "#f0f0f0",
        "year_band_line":   "#cccccc",
        "year_band_text":   "#1a4a2a",
        "tag_bg":           "#e0f0e6",
        "tag_border":       "#88c0a0",
        "tag_fg":           "#0a4028",
        "comment_bg":       "#fffde8",
        "comment_border":   "#c8a800",
        "comment_text":     "#3a2e00",
        "placeholder":      "#f0f0f0",
        "placeholder_icon": "#aaaaaa",
    },
    "forest": {
        "label":            "🌲  Forest",
        "canvas_bg":        "#070e08",
        "canvas_dot":       "#101c10",
        "app_bg":           "#070e08",
        "panel_bg":         "#0a1409",
        "panel_bg2":        "#0d1a0c",
        "border":           "#1c3018",
        "border2":          "#122010",
        "text":             "#c0f0a0",
        "text_muted":       "#70a050",
        "text_header":      "#88e060",
        "accent":           "#60c038",
        "btn_bg":           "#101c08",
        "btn_hover":        "#182c0c",
        "btn_border":       "#284816",
        "btn_disabled":     "#2a3c18",
        "input_bg":         "#0a1409",
        "sel_bg":           "#205010",
        "tab_bg":           "#091208",
        "edge_color":       "#488830",
        "scrollbar":        "#284818",
        "status_fg":        "#70a050",
        "node_bg":          "#0c1a0a",
        "node_border":      "#1e3010",
        "node_name":        "#c0f0a0",
        "year_bg":          "#201c00",
        "year_fg":          "#c0a830",
        "year_band_bg":     "#060c06",
        "year_band_line":   "#1c3018",
        "year_band_text":   "#88e060",
        "tag_bg":           "#101e08",
        "tag_border":       "#285018",
        "tag_fg":           "#70d840",
        "comment_bg":       "#1a1800",
        "comment_border":   "#585000",
        "comment_text":     "#d0c040",
        "placeholder":      "#0e1a0a",
        "placeholder_icon": "#285018",
    },
    "paper": {
        "label":            "📜  Paper",
        "canvas_bg":        "#f5eedc",
        "canvas_dot":       "#c8b880",
        "app_bg":           "#f5eedc",
        "panel_bg":         "#fdf8ec",
        "panel_bg2":        "#f8f0de",
        "border":           "#c0a060",
        "border2":          "#d8c890",
        "text":             "#2a1e08",
        "text_muted":       "#6a5030",
        "text_header":      "#4a2c08",
        "accent":           "#7a4808",
        "btn_bg":           "#ecdcc0",
        "btn_hover":        "#e0cc9c",
        "btn_border":       "#b09050",
        "btn_disabled":     "#a89060",
        "input_bg":         "#fdf8ec",
        "sel_bg":           "#d8c070",
        "tab_bg":           "#ede4c8",
        "edge_color":       "#7a4808",
        "scrollbar":        "#b09050",
        "status_fg":        "#6a5030",
        "node_bg":          "#faf4e4",
        "node_border":      "#c0a060",
        "node_name":        "#2a1e08",
        "year_bg":          "#e8d890",
        "year_fg":          "#4a2808",
        "year_band_bg":     "#ede4c8",
        "year_band_line":   "#c0a060",
        "year_band_text":   "#4a2808",
        "tag_bg":           "#e8d8b0",
        "tag_border":       "#b09050",
        "tag_fg":           "#4a2808",
        "comment_bg":       "#fdf0c0",
        "comment_border":   "#c09800",
        "comment_text":     "#3a2800",
        "placeholder":      "#ede4c8",
        "placeholder_icon": "#b09050",
    },
}

# ── Font families ─────────────────────────────────────────────────────────────
FONT_FAMILIES = [
    "Segoe UI",          # Windows system default, very clean
    "Inter",             # Modern geometric, widely used in scientific apps
    "Helvetica Neue",    # Classic clean sans-serif
    "Calibri",           # Microsoft Office standard
    "Source Sans Pro",   # Adobe open-source, great readability
    "Roboto",            # Material Design, crisp at all sizes
    "Georgia",           # Elegant serif, good for scientific papers
    "Consolas",          # Monospace, great for IDs/years/codes
]

FONT_SIZES = list(range(8, 31))   # 8 to 30 inclusive
FONT_LABELS = [str(s) for s in FONT_SIZES]

DEFAULT_KEYBINDS: dict[str, str] = {
    "save":          "Ctrl+S",
    "save_as":       "Ctrl+Shift+S",
    "new_lineage":   "Ctrl+Shift+N",
    "add_plant":     "Ctrl+N",
    "plant_db":      "Ctrl+D",
    "grid_view":     "Ctrl+Tab",
    "auto_layout":   "Ctrl+L",
    "fit_view":      "F",
    "connect":       "Ctrl+E",
    "export_png":    "Ctrl+Shift+E",
    "toggle_grid":   "Ctrl+G",
    "paste_image":   "Ctrl+V",
    "delete_node":   "Delete",
    "open_settings": "Ctrl+,",
    "add_comment":   "Ctrl+T",
    "undo":          "Ctrl+Z",
    "redo":          "Ctrl+Y",
    "copy_nodes":    "Ctrl+C",
    "paste_nodes":   "Ctrl+Shift+V",
}

KEYBIND_LABELS: dict[str, str] = {
    "save":          "Save",
    "save_as":       "Save As",
    "new_lineage":   "New Lineage",
    "add_plant":     "Add Plant",
    "plant_db":      "Open Plant Database",
    "grid_view":     "Toggle Grid / Lineage View",
    "auto_layout":   "Auto-Layout",
    "fit_view":      "Fit Canvas",
    "connect":       "Connect Nodes",
    "export_png":    "Export PNG",
    "toggle_grid":   "Toggle Grid",
    "paste_image":   "Paste Image from Clipboard",
    "delete_node":   "Delete Selected Node",
    "open_settings": "Open Settings",
    "add_comment":   "Add Comment Box",
    "undo":          "Undo",
    "redo":          "Redo",
    "copy_nodes":    "Copy Selected Nodes",
    "paste_nodes":   "Paste Nodes",
}


@dataclass
class CardStyle:
    """Styling options for plant node cards."""
    border_width:   float = 1.5      # px
    corner_radius:  float = 12.0     # px (0 = square)
    border_color:   str   = ""       # "" = use theme default
    header_height:  float = 6.0      # coloured accent bar at top

    def to_dict(self) -> dict:
        return {"border_width": self.border_width,
                "corner_radius": self.corner_radius,
                "border_color": self.border_color,
                "header_height": self.header_height}

    @classmethod
    def from_dict(cls, d: dict) -> "CardStyle":
        return cls(
            border_width  = float(d.get("border_width",  1.5)),
            corner_radius = float(d.get("corner_radius", 12.0)),
            border_color  = d.get("border_color", ""),
            header_height = float(d.get("header_height", 6.0)),
        )


@dataclass
class AppSettings:
    theme:           str       = "light"
    font_family:     str       = "Segoe UI"
    card_font_family:str       = "Georgia"    # font used on plant cards
    card_font_size:  int       = 10           # pt size for card text
    font_size:       int       = 22        # default 22pt as requested
    ui_scale:        float     = 1.0       # independent UI scale multiplier
    img_scale_factor:float     = 0.9      # card image area multiplier
    show_grid:       bool      = True
    show_images:     bool      = True
    show_props_panel:bool      = False     # off by default
    autosave:        bool      = True
    autosave_mins:   int       = 2
    window_maximized:bool      = False
    auto_name_f1:    bool      = True       # auto-name "Mother x Father F1"
    color_code_lines:bool      = True       # color mother/father lines differently
    mother_line_color:str      = "#e87060"  # color for mother lineage lines
    father_line_color:str      = "#60a0e8"  # color for father lineage lines
    last_open_filepath:str     = ""         # reopen last lineage on startup
    tour_shown:      bool      = False      # True once the guided tour has been seen
    # ── v1.2.0 additions ──────────────────────────────────────────────────────
    data_dir:        str       = ""            # "" = use ~/BreedingCharts_Data default
    watermark_enabled: bool    = False      # add watermark to PNG exports
    watermark_text:    str     = "BreedingCharts"   # watermark label
    watermark_opacity: float   = 0.35      # 0.0–1.0 alpha
    sidebar_sort:      str     = "name_asc"  # name_asc|name_desc|date_desc|date_asc|plants_desc|custom
    sidebar_show_categories: bool = True   # group sidebar by category
    # ── v1.3.0 additions ──────────────────────────────────────────────────────
    auto_layout_on_open: bool  = True      # run auto-layout whenever a lineage is opened
    card_style:      CardStyle = field(default_factory=CardStyle)
    keybinds:        dict      = field(default_factory=lambda: dict(DEFAULT_KEYBINDS))
    # ── v1.3.1 additions ──────────────────────────────────────────────────────
    grid_sort:       str       = "name"     # name|species|year|newest — Grid/Spreadsheet sort, remembered between sessions
    window_geometry: str       = ""         # base64 QMainWindow.saveGeometry() blob — remembers size/position/monitor
    # ── v1.17.1 additions ─────────────────────────────────────────────────────
    pixmap_cache_mb: int       = 0          # thumbnail RAM cache size in MB; 0 = Auto (scales with DB size)
    # ── v1.19 additions ───────────────────────────────────────────────────────
    last_active_view: str      = "lineage"  # "lineage"|"grid"|"sheet" — restored on startup

    def to_dict(self) -> dict:
        d = {
            "theme":            self.theme,
            "font_family":      self.font_family,
            "card_font_family": self.card_font_family,
            "card_font_size":   self.card_font_size,
            "font_size":        self.font_size,
            "ui_scale":         self.ui_scale,
            "img_scale_factor": self.img_scale_factor,
            "show_grid":        self.show_grid,
            "show_images":      self.show_images,
            "show_props_panel": self.show_props_panel,
            "autosave":         self.autosave,
            "autosave_mins":    self.autosave_mins,
            "window_maximized": self.window_maximized,
            "auto_name_f1":     self.auto_name_f1,
            "color_code_lines": self.color_code_lines,
            "mother_line_color":self.mother_line_color,
            "father_line_color":self.father_line_color,
            "last_open_filepath":self.last_open_filepath,
            "tour_shown":        self.tour_shown,
            "data_dir":          self.data_dir,
            "watermark_enabled": self.watermark_enabled,
            "watermark_text":    self.watermark_text,
            "watermark_opacity": self.watermark_opacity,
            "sidebar_sort":      self.sidebar_sort,
            "sidebar_show_categories": self.sidebar_show_categories,
            "auto_layout_on_open": self.auto_layout_on_open,
            "card_style":       self.card_style.to_dict(),
            "keybinds":         dict(self.keybinds),
            "grid_sort":        self.grid_sort,
            "window_geometry":  self.window_geometry,
            "pixmap_cache_mb":  self.pixmap_cache_mb,
            "last_active_view": self.last_active_view,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AppSettings":
        kb = dict(DEFAULT_KEYBINDS)
        kb.update(d.get("keybinds", {}))
        cs = CardStyle.from_dict(d.get("card_style", {}))
        return cls(
            theme            = d.get("theme",            "light"),
            font_family      = d.get("font_family",      "Segoe UI"),
            font_size        = d.get("font_size",        22),
            ui_scale         = float(d.get("ui_scale",         1.0)),
            img_scale_factor = float(d.get("img_scale_factor", 0.9)),
            show_grid        = d.get("show_grid",        True),
            show_images      = d.get("show_images",      True),
            show_props_panel = d.get("show_props_panel", False),
            autosave         = d.get("autosave",         True),
            autosave_mins    = d.get("autosave_mins",    2),
            window_maximized  = d.get("window_maximized",  False),
            auto_name_f1     = d.get("auto_name_f1",     True),
            color_code_lines = d.get("color_code_lines", True),
            mother_line_color= d.get("mother_line_color","#e87060"),
            father_line_color= d.get("father_line_color","#60a0e8"),
            card_font_family = d.get("card_font_family", "Georgia"),
            card_font_size   = int(d.get("card_font_size",   10)),
            last_open_filepath=d.get("last_open_filepath",""),
            tour_shown       = d.get("tour_shown",       False),
            data_dir         = d.get("data_dir",         ""),
            watermark_enabled= d.get("watermark_enabled", False),
            watermark_text   = d.get("watermark_text",   "BreedingCharts"),
            watermark_opacity= float(d.get("watermark_opacity", 0.35)),
            sidebar_sort     = d.get("sidebar_sort",     "name_asc"),
            sidebar_show_categories = d.get("sidebar_show_categories", True),
            auto_layout_on_open = d.get("auto_layout_on_open", True),
            card_style       = cs,
            keybinds         = kb,
            grid_sort        = d.get("grid_sort",        "name"),
            window_geometry  = d.get("window_geometry",  ""),
            pixmap_cache_mb  = int(d.get("pixmap_cache_mb", 0)),
            last_active_view = d.get("last_active_view", "lineage"),
        )

    def key(self, action: str) -> str:
        return self.keybinds.get(action, DEFAULT_KEYBINDS.get(action, ""))


_FILE = os.path.join(_get_writable_dir(), "settings.json")


def load_settings() -> AppSettings:
    try:
        with open(_FILE, "r", encoding="utf-8") as fh:
            return AppSettings.from_dict(json.load(fh))
    except Exception:
        return AppSettings()


def save_settings(s: AppSettings) -> None:
    try:
        from fileio import atomic_write_json
        atomic_write_json(_FILE, s.to_dict(), pretty=True)
    except Exception as e:
        log.error("save_settings: %s", e)


# ── Stylesheet builder ────────────────────────────────────────────────────────

def build_stylesheet(s: AppSettings) -> str:
    p   = THEMES.get(s.theme, THEMES["dark"])
    fs  = s.font_size
    fsm = max(fs - 2, 8)
    fss = max(fs - 4, 7)
    ff  = s.font_family or "Segoe UI"
    sc  = s.ui_scale

    # Scale key UI geometry with ui_scale
    btn_pad_v = int(6 * sc)
    btn_pad_h = int(14 * sc)

    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: {p['app_bg']};
    color: {p['text']};
    font-family: "{ff}", "Segoe UI", sans-serif;
    font-size: {fs}px;
}}
QMenuBar {{
    background: {p['panel_bg']}; border-bottom: 1px solid {p['border']};
    padding: 2px 6px; color: {p['text']};
}}
QMenuBar::item {{ padding: 4px 8px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {p['btn_hover']}; color: {p['accent']}; }}
QMenu {{
    background: {p['panel_bg']}; border: 1px solid {p['border']};
    border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: 4px; color: {p['text']}; }}
QMenu::item:selected {{ background: {p['btn_hover']}; color: {p['accent']}; }}
QMenu::item:checked {{ color: {p['accent']}; font-weight: bold; }}
QMenu::separator {{ height: 1px; background: {p['border2']}; margin: 3px 6px; }}
QToolBar {{
    background: {p['panel_bg']}; border-bottom: 1px solid {p['border']};
    padding: 4px 8px; spacing: 4px;
}}
QToolBar QToolButton {{
    background: transparent; color: {p['text']};
    border: 1px solid transparent; border-radius: 6px;
    padding: {btn_pad_v}px {btn_pad_h}px; font-size: {fsm}px;
}}
QToolBar QToolButton:hover {{
    background: {p['btn_hover']}; border-color: {p['btn_border']}; color: {p['accent']};
}}
QToolBar QToolButton:pressed {{ background: {p['btn_bg']}; }}
QToolBar::separator {{ background: {p['border2']}; width: 1px; margin: 4px 6px; }}
QPushButton {{
    background: {p['btn_bg']}; color: {p['text']};
    border: 1px solid {p['btn_border']}; border-radius: 6px;
    padding: {btn_pad_v}px {btn_pad_h}px; font-size: {fsm}px;
}}
QPushButton:hover {{ background: {p['btn_hover']}; border-color: {p['accent']}; color: {p['accent']}; }}
QPushButton:pressed {{ background: {p['btn_bg']}; }}
QPushButton:disabled {{ color: {p['btn_disabled']}; border-color: {p['border2']}; }}
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QKeySequenceEdit, QPlainTextEdit {{
    background: {p['input_bg']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 6px; padding: 5px 8px;
    font-family: "{ff}", sans-serif; font-size: {fs}px;
    selection-background-color: {p['sel_bg']};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QKeySequenceEdit:focus {{
    border-color: {p['accent']};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: {p['btn_bg']}; border: none; width: 18px;
}}
QLabel {{ background: transparent; color: {p['text']};
    font-family: "{ff}", sans-serif; font-size: {fs}px; }}
QSplitter::handle {{ background: {p['border2']}; }}
QScrollBar:vertical {{ background: {p['panel_bg']}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p['scrollbar']}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {p['panel_bg']}; height: 8px; }}
QScrollBar::handle:horizontal {{
    background: {p['scrollbar']}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p['accent']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QTabWidget::pane {{
    border: 1px solid {p['border']}; border-radius: 0 6px 6px 6px; background: {p['panel_bg']};
}}
QTabBar::tab {{
    background: {p['tab_bg']}; color: {p['text_muted']};
    border: 1px solid {p['border2']}; border-bottom: none;
    border-radius: 6px 6px 0 0; padding: 6px 14px; margin-right: 2px;
    font-size: {fsm}px;
}}
QTabBar::tab:selected {{ background: {p['panel_bg']}; color: {p['accent']}; border-color: {p['border']}; }}
QTabBar::tab:hover:!selected {{ background: {p['btn_hover']}; color: {p['text']}; }}
QGroupBox {{
    border: 1px solid {p['border']}; border-radius: 6px;
    margin-top: 10px; padding: 10px 8px 8px 8px;
    font-weight: bold; color: {p['text_muted']}; font-size: {fsm}px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p['text_muted']}; }}
QListWidget {{
    background: {p['input_bg']}; border: 1px solid {p['border']};
    border-radius: 6px; color: {p['text']}; outline: none; font-size: {fs}px;
}}
QListWidget::item {{ padding: 5px 8px; border-bottom: 1px solid {p['border2']}; }}
QListWidget::item:selected {{ background: {p['btn_hover']}; color: {p['accent']}; }}
QListWidget::item:hover {{ background: {p['btn_bg']}; }}
QComboBox {{
    background: {p['input_bg']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 6px; padding: 4px 8px;
    font-size: {fs}px;
}}
QComboBox:focus {{ border-color: {p['accent']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {p['panel_bg']}; color: {p['text']}; border: 1px solid {p['border']};
    selection-background-color: {p['btn_hover']}; selection-color: {p['accent']};
    font-size: {fs}px;
}}
QCheckBox {{ color: {p['text']}; spacing: 6px; font-size: {fs}px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid {p['border']};
    border-radius: 4px; background: {p['input_bg']};
}}
QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; }}
QCheckBox::indicator:hover {{ border-color: {p['accent']}; }}
QStatusBar {{
    background: {p['panel_bg']}; border-top: 1px solid {p['border2']};
    color: {p['status_fg']}; font-size: {fss}px; padding: 0 8px;
}}
QGraphicsView {{ border: none; background: {p['canvas_bg']}; }}
QDialogButtonBox QPushButton {{ min-width: 80px; }}
QWidget#sidebar_panel, QWidget#props_panel {{ background: {p['panel_bg']}; }}
QFrame#props_frame {{
    background: {p['panel_bg2']}; border: 1px solid {p['border']}; border-radius: 8px;
}}
QLabel#panel_header {{
    color: {p['text_header']}; font-weight: bold; font-size: {fs}px; background: transparent;
    padding: 4px 2px 2px 2px;
}}
QLabel#node_name_label {{
    font-size: {fs+1}px; font-weight: bold; color: {p['text']}; background: transparent;
}}
QLabel#node_species_label {{
    font-style: italic; color: {p['text_muted']}; font-size: {fss}px; background: transparent;
}}
QLabel#node_year_label {{ color: {p['year_fg']}; font-size: {fss}px; background: transparent; }}
QLabel#node_desc_label {{ color: {p['text_muted']}; font-size: {fss}px; background: transparent; }}
QLabel#node_tags_label {{ color: {p['accent']}; font-size: {max(fss-1,7)}px; background: transparent; }}
QLabel#node_gen_label {{ color: {p['text_muted']}; font-size: {max(fss-1,7)}px; background: transparent; }}
QLabel#node_img_label {{
    border: 1px solid {p['border']}; border-radius: 6px;
    background: {p['panel_bg']}; color: {p['text_muted']};
}}
QLineEdit#sidebar_search {{
    background: {p['input_bg']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 6px;
    padding: 4px 8px; font-size: {fss}px;
}}
QLabel#sidebar_caption {{ font-size: {fss}px; background: transparent; color: {p['text_muted']}; }}
QComboBox#sidebar_sort_combo {{ font-size: {fss}px; }}
QPushButton#sidebar_action_btn {{ font-size: {fss}px; }}
QCompleter QAbstractItemView {{
    background: {p['panel_bg']}; color: {p['text']};
    border: 1px solid {p['accent']}; font-size: {fs}px;
    selection-background-color: {p['btn_hover']};
}}
"""


# ── Settings Dialog ───────────────────────────────────────────────────────────

class SettingsDialog(FadeDialog):
    def __init__(self, settings: AppSettings,
                 on_change: Optional[Callable[[AppSettings], None]] = None,
                 on_data_dir_changed: Optional[Callable[[str], bool]] = None,
                 parent=None):
        super().__init__(parent)
        self._s         = AppSettings.from_dict(settings.to_dict())
        self._original  = AppSettings.from_dict(settings.to_dict())
        self._on_change = on_change
        self._on_data_dir_changed = on_data_dir_changed
        self.setWindowTitle("⚙️  Settings")
        self.setMinimumWidth(600)
        self.setMinimumHeight(560)

        # Debounce live-preview: rapid spinbox / slider changes (e.g. holding
        # arrow key on font-size) would otherwise fire _apply_settings on every
        # tick, triggering full canvas + grid rebuilds tens of times per second.
        # 280 ms feels instant to a human but coalesces bursts of ticks.
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.setInterval(280)
        self._emit_timer.timeout.connect(self._emit_now)

        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12); root.setContentsMargins(16, 16, 16, 16)
        tabs = QTabWidget()
        tabs.addTab(self._tab_appearance(), "🎨  Appearance")
        tabs.addTab(self._tab_cards(),      "🃏  Cards")
        tabs.addTab(self._tab_canvas(),     "🖼️  Canvas & Saves")
        tabs.addTab(self._tab_keybinds(),   "⌨️  Keybinds")
        root.addWidget(tabs)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self._commit)
        bb.rejected.connect(self._cancel)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._reset)
        root.addWidget(bb)

    def _tab_appearance(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setSpacing(14); lay.setContentsMargins(12, 12, 12, 12)

        tg = QGroupBox("Theme"); tgl = QVBoxLayout(tg); tgl.setSpacing(8)
        r1 = QHBoxLayout(); r1.setSpacing(8)
        r2 = QHBoxLayout(); r2.setSpacing(8)
        self._theme_btns: dict[str, QPushButton] = {}
        for i, (tid, td) in enumerate(THEMES.items()):
            btn = QPushButton(td["label"])
            btn.setCheckable(True); btn.setChecked(tid == self._s.theme)
            btn.setMinimumHeight(42)
            ac, bg, bgh, tx = td["accent"], td["btn_bg"], td["btn_hover"], td["text"]
            btn.setStyleSheet(f"""
                QPushButton {{ background:{bg}; color:{tx}; border:2px solid {bg};
                    border-radius:8px; font-size:12px; padding:4px 8px; }}
                QPushButton:hover {{ background:{bgh}; border-color:{ac}; }}
                QPushButton:checked {{ background:{bgh}; border-color:{ac};
                    font-weight:bold; color:{ac}; }}
            """)
            btn.clicked.connect(lambda _, t=tid: self._set_theme(t))
            self._theme_btns[tid] = btn
            (r1 if i < 2 else r2).addWidget(btn)
        tgl.addLayout(r1); tgl.addLayout(r2)
        self._preview = _ThemePreviewStrip()
        self._preview.setFixedHeight(46); tgl.addWidget(self._preview)
        lay.addWidget(tg)

        fg = QGroupBox("Font"); fgl = QFormLayout(fg); fgl.setSpacing(8)
        self._font_family_combo = QComboBox()
        for ff in FONT_FAMILIES: self._font_family_combo.addItem(ff)
        try: fidx = FONT_FAMILIES.index(self._s.font_family)
        except ValueError: fidx = 0
        self._font_family_combo.setCurrentIndex(fidx)
        self._font_family_combo.currentIndexChanged.connect(
            lambda i: self._update(font_family=FONT_FAMILIES[i]))
        fgl.addRow("UI font family:", self._font_family_combo)

        self._font_combo = QComboBox()
        for lbl in FONT_LABELS: self._font_combo.addItem(lbl)
        try: idx = FONT_SIZES.index(self._s.font_size)
        except ValueError: idx = FONT_SIZES.index(22)
        self._font_combo.setCurrentIndex(idx)
        self._font_combo.currentIndexChanged.connect(
            lambda i: self._update(font_size=FONT_SIZES[i]))
        fgl.addRow("Font size:", self._font_combo)
        lay.addWidget(fg)

        sg = QGroupBox("Scale"); sgl = QFormLayout(sg); sgl.setSpacing(8)
        self._ui_scale_spin = QDoubleSpinBox()
        self._ui_scale_spin.setRange(0.5, 3.0); self._ui_scale_spin.setSingleStep(0.1)
        self._ui_scale_spin.setDecimals(1); self._ui_scale_spin.setSuffix("×")
        self._ui_scale_spin.setValue(self._s.ui_scale)
        self._ui_scale_spin.valueChanged.connect(lambda v: self._update(ui_scale=v))
        sgl.addRow("UI scale:", self._ui_scale_spin)
        lay.addWidget(sg)

        lay.addStretch(); self._refresh_preview()
        return w

    def _tab_cards(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setSpacing(10); lay.setContentsMargins(12, 12, 12, 12)

        # ── Card Font ──────────────────────────────────────────────────────────
        fg = QGroupBox("Card Font"); fgl = QFormLayout(fg); fgl.setSpacing(8)
        self._card_font_combo = QComboBox()
        CARD_FONTS = ["Georgia", "Segoe UI", "Arial", "Times New Roman",
                      "Palatino Linotype", "Garamond", "Verdana", "Trebuchet MS",
                      "Consolas", "Courier New"]
        for cf in CARD_FONTS: self._card_font_combo.addItem(cf)
        try: cidx = CARD_FONTS.index(self._s.card_font_family)
        except ValueError: cidx = 0
        self._card_font_combo.setCurrentIndex(cidx)
        self._card_font_combo.currentIndexChanged.connect(
            lambda i: self._update(card_font_family=CARD_FONTS[i]))
        fgl.addRow("Card font family:", self._card_font_combo)

        self._card_font_size_spin = QSpinBox()
        self._card_font_size_spin.setRange(6, 28)
        self._card_font_size_spin.setSuffix(" pt")
        self._card_font_size_spin.setValue(self._s.card_font_size)
        self._card_font_size_spin.valueChanged.connect(
            lambda v: self._update(card_font_size=v))
        fgl.addRow("Card font size:", self._card_font_size_spin)
        lay.addWidget(fg)

        # ── Card Image ─────────────────────────────────────────────────────────
        ig = QGroupBox("Card Image"); igl = QFormLayout(ig); igl.setSpacing(8)
        self._img_scale_spin = QDoubleSpinBox()
        self._img_scale_spin.setRange(0.25, 4.0); self._img_scale_spin.setSingleStep(0.25)
        self._img_scale_spin.setDecimals(2); self._img_scale_spin.setSuffix("×")
        self._img_scale_spin.setValue(self._s.img_scale_factor)
        self._img_scale_spin.valueChanged.connect(lambda v: self._update(img_scale_factor=v))
        igl.addRow("Card image scale:", self._img_scale_spin)
        lay.addWidget(ig)

        # ── Card Border & Shape ────────────────────────────────────────────────
        bg = QGroupBox("Card Border & Shape"); bgl = QFormLayout(bg); bgl.setSpacing(10)

        self._border_w_spin = QDoubleSpinBox()
        self._border_w_spin.setRange(0, 8); self._border_w_spin.setSingleStep(0.5)
        self._border_w_spin.setDecimals(1); self._border_w_spin.setSuffix(" px")
        self._border_w_spin.setValue(self._s.card_style.border_width)
        self._border_w_spin.valueChanged.connect(
            lambda v: self._update_card("border_width", v))
        bgl.addRow("Border width:", self._border_w_spin)

        self._corner_spin = QDoubleSpinBox()
        self._corner_spin.setRange(0, 40); self._corner_spin.setSingleStep(1)
        self._corner_spin.setDecimals(0); self._corner_spin.setSuffix(" px")
        self._corner_spin.setValue(self._s.card_style.corner_radius)
        self._corner_spin.valueChanged.connect(
            lambda v: self._update_card("corner_radius", v))
        bgl.addRow("Corner radius (0=square):", self._corner_spin)

        self._border_color_btn = QPushButton()
        self._border_color_btn.setFixedSize(80, 28)
        self._card_border_color = self._s.card_style.border_color or ""
        self._refresh_border_color_btn()
        self._border_color_btn.clicked.connect(self._pick_border_color)
        bgl.addRow("Border colour override:", self._border_color_btn)

        self._header_h_spin = QDoubleSpinBox()
        self._header_h_spin.setRange(0, 20); self._header_h_spin.setSingleStep(1)
        self._header_h_spin.setDecimals(0); self._header_h_spin.setSuffix(" px")
        self._header_h_spin.setValue(self._s.card_style.header_height)
        self._header_h_spin.valueChanged.connect(
            lambda v: self._update_card("header_height", v))
        bgl.addRow("Accent bar height:", self._header_h_spin)

        lay.addWidget(bg)
        lay.addStretch()
        return w

    def _tab_canvas(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setSpacing(10); lay.setContentsMargins(12, 12, 12, 12)
        cg = QGroupBox("Canvas Display"); cgl = QVBoxLayout(cg); cgl.setSpacing(10)
        self._auto_layout_cb = QCheckBox("Auto-layout when opening a lineage  (Ctrl+L)")
        self._auto_layout_cb.setChecked(self._s.auto_layout_on_open)
        self._auto_layout_cb.toggled.connect(lambda v: self._update(auto_layout_on_open=v))
        cgl.addWidget(self._auto_layout_cb)
        self._props_cb = QCheckBox("Show Properties panel (right sidebar)")
        self._props_cb.setChecked(self._s.show_props_panel)
        self._props_cb.toggled.connect(lambda v: self._update(show_props_panel=v))
        cgl.addWidget(self._props_cb)
        lay.addWidget(cg)

        ng = QGroupBox("Naming & Lines"); ngl = QVBoxLayout(ng); ngl.setSpacing(10)
        self._auto_name_cb = QCheckBox(
            "Auto-name new plants as \"Mother × Father F1\" when connected")
        self._auto_name_cb.setChecked(self._s.auto_name_f1)
        self._auto_name_cb.toggled.connect(lambda v: self._update(auto_name_f1=v))
        ngl.addWidget(self._auto_name_cb)
        self._color_lines_cb = QCheckBox("Color-code lineage lines by parent role")
        self._color_lines_cb.setChecked(self._s.color_code_lines)
        self._color_lines_cb.toggled.connect(lambda v: self._update(color_code_lines=v))
        ngl.addWidget(self._color_lines_cb)
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("Mother line color:"))
        self._mother_col_btn = QPushButton()
        self._mother_col_btn.setFixedSize(60, 24)
        self._mother_col_btn.setStyleSheet(f"background:{self._s.mother_line_color}; border-radius:4px;")
        self._mother_col_btn.clicked.connect(self._pick_mother_color)
        col_row.addWidget(self._mother_col_btn)
        col_row.addSpacing(16)
        col_row.addWidget(QLabel("Father line color:"))
        self._father_col_btn = QPushButton()
        self._father_col_btn.setFixedSize(60, 24)
        self._father_col_btn.setStyleSheet(f"background:{self._s.father_line_color}; border-radius:4px;")
        self._father_col_btn.clicked.connect(self._pick_father_color)
        col_row.addWidget(self._father_col_btn)
        col_row.addStretch()
        ngl.addLayout(col_row)
        lay.addWidget(ng)

        # ── Data folder ────────────────────────────────────────────────────────
        from database import get_data_dir as _get_data_dir, set_data_dir as _set_data_dir
        dg = QGroupBox("Data Folder"); dgl = QVBoxLayout(dg); dgl.setSpacing(6)
        dg_info = QLabel(
            "All lineage files (.plf) and the plant database are stored here.<br>"
            "Keep this folder separate from the app so software updates don't "
            "require migrating your data.")
        dg_info.setWordWrap(True)
        dg_info.setStyleSheet("font-size:11px; color:#90b898;")
        dgl.addWidget(dg_info)
        dir_row = QHBoxLayout()
        self._data_dir_lbl = QLabel(_get_data_dir())
        self._data_dir_lbl.setStyleSheet("font-family:monospace; font-size:11px;")
        self._data_dir_lbl.setWordWrap(True)
        dir_row.addWidget(self._data_dir_lbl, 1)
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedHeight(26)
        def _browse_data_dir():
            from PyQt6.QtWidgets import QFileDialog, QMessageBox
            current_dir = _get_data_dir()
            new_d = QFileDialog.getExistingDirectory(
                self, "Choose Data Folder", current_dir,
                QFileDialog.Option.ShowDirsOnly)
            if not new_d or new_d == current_dir:
                return
            reply = QMessageBox.question(
                self, "Change Data Folder",
                f"<b>New folder:</b><br>{new_d}<br><br>"
                f"BreedingCharts will look for and save lineages there from now on.<br>"
                f"Existing files in <code>{current_dir}</code> are <b>not</b> moved "
                f"automatically — copy them across manually if needed.<br><br>Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return

            if self._on_data_dir_changed is not None:
                # Preferred path: MainWindow._apply_new_data_dir() — this is
                # what actually refreshes the sidebar/grid/notes and reopens
                # a lineage for the new folder. Without this callback wired
                # up, the folder pointer itself would still change correctly
                # but every view would keep showing the *old* folder's data
                # until something else happened to refresh them.
                ok = self._on_data_dir_changed(new_d)
            else:
                ok = _set_data_dir(new_d)
                try:
                    import plant_db as _plant_db
                    _plant_db.reset_cache()
                except Exception:
                    pass

            if ok:
                self._s.data_dir = new_d
                self._data_dir_lbl.setText(new_d)
                self._emit()
        btn_browse.clicked.connect(_browse_data_dir)
        dir_row.addWidget(btn_browse)
        dgl.addLayout(dir_row)
        lay.addWidget(dg)

        ag = QGroupBox("Autosave"); agl = QFormLayout(ag); agl.setSpacing(10)
        self._autosave_cb = QCheckBox("Enable autosave")
        self._autosave_cb.setChecked(self._s.autosave)
        self._autosave_cb.toggled.connect(lambda v: self._update(autosave=v))
        agl.addRow("", self._autosave_cb)
        self._autosave_spin = QSpinBox()
        self._autosave_spin.setRange(1, 60); self._autosave_spin.setSuffix(" min")
        self._autosave_spin.setValue(self._s.autosave_mins)
        self._autosave_spin.valueChanged.connect(lambda v: self._update(autosave_mins=v))
        agl.addRow("Save every:", self._autosave_spin)
        lay.addWidget(ag)

        pg = QGroupBox("Performance"); pgl = QVBoxLayout(pg); pgl.setSpacing(6)
        pg_info = QLabel(
            "Controls how much RAM the Grid View's on-screen thumbnail cache is "
            "allowed to use. \"Auto\" scales it with the number of photos in "
            "your plant database. This is a soft ceiling, not a hard limit — "
            "going over it doesn't crash the app, it just means Qt discards "
            "older thumbnails and redecodes them if you scroll back, which "
            "costs a little time.")
        pg_info.setWordWrap(True)
        pg_info.setStyleSheet("font-size:11px; color:#90b898;")
        pgl.addWidget(pg_info)

        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel("Thumbnail cache:"))
        self._cache_auto_cb = QCheckBox("Auto")
        self._cache_auto_cb.setChecked(self._s.pixmap_cache_mb <= 0)
        cache_row.addWidget(self._cache_auto_cb)
        self._cache_spin = QSpinBox()
        self._cache_spin.setRange(20, 1000)
        self._cache_spin.setSuffix(" MB")
        self._cache_spin.setEnabled(self._s.pixmap_cache_mb > 0)
        try:
            import plant_db as _plant_db
            auto_mb = max(20, int(_plant_db.recommended_pixmap_cache_kb() / 1024))
        except Exception:
            auto_mb = 80
        self._cache_spin.setValue(self._s.pixmap_cache_mb if self._s.pixmap_cache_mb > 0 else auto_mb)
        cache_row.addWidget(self._cache_spin)
        cache_row.addStretch()
        pgl.addLayout(cache_row)

        cache_note = QLabel(f"Current auto-computed size for this database: ~{auto_mb} MB")
        cache_note.setStyleSheet("font-size:11px; color:#90b898;")
        pgl.addWidget(cache_note)

        def _on_cache_auto_toggled(checked: bool) -> None:
            self._cache_spin.setEnabled(not checked)
            self._update(pixmap_cache_mb=(0 if checked else self._cache_spin.value()))
        self._cache_auto_cb.toggled.connect(_on_cache_auto_toggled)
        self._cache_spin.valueChanged.connect(
            lambda v: (not self._cache_auto_cb.isChecked()) and self._update(pixmap_cache_mb=v))

        cache_restart_note = QLabel("Takes effect on next launch.")
        cache_restart_note.setStyleSheet("font-size:11px; color:#90b898; font-style:italic;")
        pgl.addWidget(cache_restart_note)

        lay.addWidget(pg)
        lay.addStretch()
        return w

    def _pick_mother_color(self) -> None:
        col = QColorDialog.getColor(QColor(self._s.mother_line_color), self, "Mother line color")
        if col.isValid():
            self._s.mother_line_color = col.name()
            self._mother_col_btn.setStyleSheet(f"background:{col.name()}; border-radius:4px;")
            self._emit()

    def _pick_father_color(self) -> None:
        col = QColorDialog.getColor(QColor(self._s.father_line_color), self, "Father line color")
        if col.isValid():
            self._s.father_line_color = col.name()
            self._father_col_btn.setStyleSheet(f"background:{col.name()}; border-radius:4px;")
            self._emit()

    def _tab_keybinds(self) -> QWidget:
        outer = QWidget(); ol = QVBoxLayout(outer); ol.setContentsMargins(4, 8, 4, 4)
        info = QLabel("Click a field and press your desired key combination.")
        info.setStyleSheet("color: #888; font-size: 11px;")
        ol.addWidget(info)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(); form = QFormLayout(inner)
        form.setSpacing(8); form.setContentsMargins(8, 8, 8, 8)
        self._kb_edits: dict[str, QKeySequenceEdit] = {}
        for aid, label in KEYBIND_LABELS.items():
            edit = QKeySequenceEdit(QKeySequence(self._s.keybinds.get(aid, "")))
            edit.keySequenceChanged.connect(
                lambda ks, a=aid: self._update_keybind(a, ks.toString()))
            self._kb_edits[aid] = edit
            row = QHBoxLayout(); row.addWidget(edit)
            rb = QPushButton("↺"); rb.setFixedWidth(32)
            rb.setToolTip(f"Reset: {DEFAULT_KEYBINDS.get(aid, '')}")
            rb.clicked.connect(lambda _, a=aid: self._reset_one_keybind(a))
            row.addWidget(rb); form.addRow(label + ":", row)
        scroll.setWidget(inner); ol.addWidget(scroll)
        return outer

    def _pick_border_color(self) -> None:
        col = QColorDialog.getColor(
            QColor(self._card_border_color) if self._card_border_color else QColor("#4a7a58"),
            self, "Card border colour")
        if col.isValid():
            self._card_border_color = col.name()
            self._refresh_border_color_btn()
            self._update_card("border_color", self._card_border_color)

    def _refresh_border_color_btn(self) -> None:
        col = self._card_border_color or "#888888"
        self._border_color_btn.setStyleSheet(
            f"background:{col}; border-radius:4px; border:none;")
        self._border_color_btn.setText("" if self._card_border_color else "Theme")

    def _set_theme(self, tid: str) -> None:
        self._update(theme=tid)
        for t, btn in self._theme_btns.items(): btn.setChecked(t == tid)
        self._refresh_preview()

    def _update(self, **kw) -> None:
        for k, v in kw.items(): setattr(self._s, k, v)
        self._emit()

    def _update_card(self, field_name: str, value) -> None:
        setattr(self._s.card_style, field_name, value)
        self._emit()

    def _update_keybind(self, aid: str, ks: str) -> None:
        self._s.keybinds[aid] = ks; self._emit()

    def _reset_one_keybind(self, aid: str) -> None:
        d = DEFAULT_KEYBINDS.get(aid, "")
        self._s.keybinds[aid] = d
        if e := self._kb_edits.get(aid): e.setKeySequence(QKeySequence(d))
        self._emit()

    def _refresh_preview(self) -> None:
        self._preview.set_theme(THEMES.get(self._s.theme, THEMES["dark"]))

    def _emit(self) -> None:
        """Schedule a live-preview update.  Debounced so rapid changes
        (holding arrow key on font-size spinbox) don't stall the UI."""
        self._emit_timer.start()   # restarts the timer if already running

    def _emit_now(self) -> None:
        """Fire the actual on_change callback immediately (called by timer)."""
        if self._on_change: self._on_change(AppSettings.from_dict(self._s.to_dict()))

    def _commit(self) -> None:
        self._emit_timer.stop()
        self._emit_now()   # flush any pending change before accepting
        self.accept()
    def _cancel(self) -> None:
        self._emit_timer.stop()
        if self._on_change: self._on_change(self._original)
        self.reject()

    def _reset(self) -> None:
        d = AppSettings(); self._s = d
        try: idx = FONT_SIZES.index(d.font_size)
        except ValueError: idx = FONT_SIZES.index(22)
        self._font_combo.setCurrentIndex(idx)
        try: fidx = FONT_FAMILIES.index(d.font_family)
        except ValueError: fidx = 0
        self._font_family_combo.setCurrentIndex(fidx)
        CARD_FONTS = ["Georgia", "Segoe UI", "Arial", "Times New Roman",
                      "Palatino Linotype", "Garamond", "Verdana", "Trebuchet MS",
                      "Consolas", "Courier New"]
        try: cidx = CARD_FONTS.index(d.card_font_family)
        except ValueError: cidx = 0
        self._card_font_combo.setCurrentIndex(cidx)
        self._ui_scale_spin.setValue(d.ui_scale)
        self._img_scale_spin.setValue(d.img_scale_factor)
        self._card_font_size_spin.setValue(d.card_font_size)
        self._props_cb.setChecked(d.show_props_panel)
        self._auto_layout_cb.setChecked(d.auto_layout_on_open)
        self._auto_name_cb.setChecked(d.auto_name_f1)
        self._color_lines_cb.setChecked(d.color_code_lines)
        self._mother_col_btn.setStyleSheet(f"background:{d.mother_line_color}; border-radius:4px;")
        self._father_col_btn.setStyleSheet(f"background:{d.father_line_color}; border-radius:4px;")
        self._autosave_cb.setChecked(d.autosave)
        self._autosave_spin.setValue(d.autosave_mins)
        self._cache_auto_cb.setChecked(d.pixmap_cache_mb <= 0)
        self._cache_spin.setEnabled(d.pixmap_cache_mb > 0)
        if d.pixmap_cache_mb > 0:
            self._cache_spin.setValue(d.pixmap_cache_mb)
        self._border_w_spin.setValue(d.card_style.border_width)
        self._corner_spin.setValue(d.card_style.corner_radius)
        self._header_h_spin.setValue(d.card_style.header_height)
        self._card_border_color = d.card_style.border_color
        self._refresh_border_color_btn()
        for tid, btn in self._theme_btns.items(): btn.setChecked(tid == d.theme)
        for aid, edit in self._kb_edits.items():
            edit.setKeySequence(QKeySequence(DEFAULT_KEYBINDS.get(aid, "")))
        self._refresh_preview(); self._emit()

    @property
    def result_settings(self) -> AppSettings:
        return AppSettings.from_dict(self._s.to_dict())


class _ThemePreviewStrip(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QHBoxLayout(self); lay.setContentsMargins(10, 5, 10, 5); lay.setSpacing(7)
        self._swatches: list[QFrame] = []
        for _ in range(7):
            sw = QFrame(); sw.setFixedSize(26, 26); sw.setFrameShape(QFrame.Shape.NoFrame)
            lay.addWidget(sw); self._swatches.append(sw)
        lay.addStretch()
        self._lbl = QLabel(); lay.addWidget(self._lbl)

    def set_theme(self, p: dict) -> None:
        keys = ["canvas_bg","panel_bg","accent","btn_bg","text_muted","edge_color","node_bg"]
        for sw, col in zip(self._swatches, [p.get(k,"#888") for k in keys]):
            sw.setStyleSheet(f"background:{col}; border-radius:5px;")
        self.setStyleSheet(
            f"background:{p['panel_bg']}; border:1px solid {p['border']}; border-radius:8px;")
        self._lbl.setStyleSheet(f"color:{p['text']}; background:transparent;")
        self._lbl.setText(p.get("label", ""))
