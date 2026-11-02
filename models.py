"""
models.py — Core data models for BreedingCharts.

v1.2.0 changes:
  • LineageTree.category: species/group tag for sidebar grouping
  • LineageTree.sort_index: custom drag order in sidebar
"""
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass
class PlantNode:
    id:           str            = field(default_factory=lambda: str(uuid.uuid4()))
    name:         str            = "Unnamed Plant"
    species:      str            = ""
    cultivar:     str            = ""
    description:  str            = ""
    year:         Optional[int]  = None
    images:       List[str]      = field(default_factory=list)   # full-res originals —
                                                                   # may be [] when not
                                                                   # loaded this session;
                                                                   # see PlantNode.has_photo
    thumb:        Optional[str]  = None   # small pre-scaled JPEG (b64) — always resident,
                                           # used for all normal card/preview rendering
    photo_count:  int            = 0      # len(images) as of the last time images were
                                           # actually loaded/edited — lets UI show "+N"
                                           # badges without touching full-res data
    parent_ids:   List[str]      = field(default_factory=list)   # max 2
    parent_roles: Dict[str, str] = field(default_factory=dict)   # id → "mother"|"father"|""
    x:            float          = 0.0
    y:            float          = 0.0
    tags:         List[str]      = field(default_factory=list)
    color:        str            = "#52a86a"
    notes:        str            = ""
    created_at:   str            = field(default_factory=lambda: datetime.now().isoformat())
    # ^ Set once, at construction, and never touched again by editing code
    #   (PlantDialog mutates the existing node's other fields in place) — so
    #   this stays a true "date added", not a "last edited" timestamp.
    #   Plants saved before this field existed load with "" (unknown/legacy)
    #   rather than being given today's date, which would be misleading and
    #   would make every pre-existing plant look freshly added.

    @property
    def has_photo(self) -> bool:
        """True if this plant has at least one photo, without requiring the
        full-resolution data to be loaded (checks the always-resident thumb/
        count first)."""
        return bool(self.thumb) or self.photo_count > 0 or bool(self.images)

    @property
    def image_data(self) -> Optional[str]:
        return self.images[0] if self.images else None

    @image_data.setter
    def image_data(self, v: Optional[str]) -> None:
        if v is None:
            if self.images: self.images[0:1] = []
        elif self.images:   self.images[0] = v
        else:               self.images.append(v)

    def mother_id(self) -> Optional[str]:
        for pid in self.parent_ids:
            if self.parent_roles.get(pid) == "mother":
                return pid
        return None

    def father_id(self) -> Optional[str]:
        for pid in self.parent_ids:
            if self.parent_roles.get(pid) == "father":
                return pid
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "species": self.species,
            "cultivar": self.cultivar, "description": self.description,
            "year": self.year, "images": list(self.images),
            "thumb": self.thumb, "photo_count": self.photo_count,
            "parent_ids": list(self.parent_ids),
            "parent_roles": dict(self.parent_roles),
            "x": self.x, "y": self.y,
            "tags": list(self.tags), "color": self.color, "notes": self.notes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlantNode":
        imgs = list(d.get("images", []))
        legacy = d.get("image_data")
        if legacy and not imgs:
            imgs = [legacy]
        photo_count = int(d.get("photo_count", 0)) or len(imgs)
        return cls(
            id           = d.get("id",           str(uuid.uuid4())),
            name         = d.get("name",         "Unnamed Plant"),
            species      = d.get("species",      ""),
            cultivar     = d.get("cultivar",     ""),
            description  = d.get("description",  ""),
            year         = d.get("year"),
            images       = imgs,
            thumb        = d.get("thumb"),
            photo_count  = photo_count,
            parent_ids   = list(d.get("parent_ids",   [])),
            parent_roles = dict(d.get("parent_roles",  {})),
            x            = float(d.get("x", 0.0)),
            y            = float(d.get("y", 0.0)),
            tags         = list(d.get("tags",    [])),
            color        = d.get("color",        "#52a86a"),
            notes        = d.get("notes",        ""),
            created_at   = d.get("created_at",   "") or "",
        )


@dataclass
class Note:
    """A single Google-Keep-style note: title + rich-text body.

    `html` is whatever QTextEdit.toHtml() produces — bold/italic/underline,
    bullet/numbered lists, and headings all round-trip through it directly.
    Pasted images are embedded as base64 data-URIs inline within `html`
    itself (see notes_view.NoteTextEdit.insertFromMimeData), so a Note is
    fully self-contained: no separate image sidecar to keep in sync.
    """
    id:           str  = field(default_factory=lambda: str(uuid.uuid4()))
    title:        str  = ""
    html:         str  = ""
    color:        str  = ""      # "" = default card color; else a hex tint
    pinned:       bool = False
    created_at:   str  = field(default_factory=lambda: datetime.now().isoformat())
    modified_at:  str  = field(default_factory=lambda: datetime.now().isoformat())

    def touch(self) -> None:
        self.modified_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "html": self.html,
            "color": self.color, "pinned": self.pinned,
            "created_at": self.created_at, "modified_at": self.modified_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        return cls(
            id          = d.get("id",          str(uuid.uuid4())),
            title       = d.get("title",        ""),
            html        = d.get("html",         ""),
            color       = d.get("color",        ""),
            pinned      = bool(d.get("pinned",  False)),
            created_at  = d.get("created_at",   datetime.now().isoformat()),
            modified_at = d.get("modified_at",  datetime.now().isoformat()),
        )


@dataclass
class CommentNote:
    id:    str   = field(default_factory=lambda: str(uuid.uuid4()))
    text:  str   = "Add a note…"
    x:     float = 0.0
    y:     float = 0.0
    w:     float = 220.0
    h:     float = 90.0
    color: str   = "#c8963c"

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text,
                "x": self.x, "y": self.y, "w": self.w, "h": self.h, "color": self.color}

    @classmethod
    def from_dict(cls, d: dict) -> "CommentNote":
        return cls(
            id=d.get("id", str(uuid.uuid4())), text=d.get("text", ""),
            x=float(d.get("x", 0.0)),  y=float(d.get("y", 0.0)),
            w=float(d.get("w", 220.0)), h=float(d.get("h", 90.0)),
            color=d.get("color", "#c8963c"),
        )


@dataclass
class LineageTree:
    id:           str                    = field(default_factory=lambda: str(uuid.uuid4()))
    name:         str                    = "New Lineage"
    description:  str                    = ""
    accent_color: str                    = ""   # per-lineage canvas tint (hex or "")
    category:     str                    = ""   # v1.2.0: species/group for sidebar grouping
    sort_index:   int                    = 0    # v1.2.0: custom sidebar sort order
    created_at:   str                    = field(default_factory=lambda: datetime.now().isoformat())
    modified_at:  str                    = field(default_factory=lambda: datetime.now().isoformat())
    nodes:        Dict[str, PlantNode]   = field(default_factory=dict)
    comments:     Dict[str, CommentNote] = field(default_factory=dict)

    # Runtime-only (never persisted): node IDs whose .images currently hold
    # authoritative full-resolution data this session — see lineage_images.py.
    _image_loaded_ids: set = field(default_factory=set, repr=False, compare=False)

    # Runtime-only: set when a node is removed, so save_lineage() knows the
    # images sidecar may contain an orphaned entry that needs pruning even
    # though nothing was newly loaded/edited this session — see
    # database.save_lineage() and lineage_images.merge_and_save().
    _images_maybe_stale: bool = field(default=False, repr=False, compare=False)

    def add_node(self, node: PlantNode) -> None:
        self.nodes[node.id] = node; self.touch()

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.nodes: return
        for n in self.nodes.values():
            if node_id in n.parent_ids:
                n.parent_ids.remove(node_id)
                n.parent_roles.pop(node_id, None)
        del self.nodes[node_id]; self._image_loaded_ids.discard(node_id)
        self._images_maybe_stale = True
        self.touch()

    def connect_parent(self, parent_id: str, child_id: str) -> bool:
        if parent_id not in self.nodes or child_id not in self.nodes: return False
        child = self.nodes[child_id]
        if parent_id in child.parent_ids: return False
        if len(child.parent_ids) >= 2: return False
        if child_id in self._ancestors(parent_id): return False
        child.parent_ids.append(parent_id)
        self.touch(); return True

    def disconnect_parent(self, parent_id: str, child_id: str) -> None:
        child = self.nodes.get(child_id)
        if child and parent_id in child.parent_ids:
            child.parent_ids.remove(parent_id)
            child.parent_roles.pop(parent_id, None)
            self.touch()

    def get_offspring(self, node_id: str) -> List[str]:
        return [n.id for n in self.nodes.values() if node_id in n.parent_ids]

    def get_generation(self, node_id: str, _cache: dict = None) -> int:
        if _cache is None:
            _cache = {}
        if node_id in _cache:
            return _cache[node_id]
        node = self.nodes.get(node_id)
        if not node or not node.parent_ids:
            _cache[node_id] = 0
            return 0
        result = 1 + max(
            (self.get_generation(pid, _cache)
             for pid in node.parent_ids if pid in self.nodes), default=-1)
        _cache[node_id] = result
        return result

    def _ancestors(self, node_id: str, visited: set = None) -> set:
        if visited is None: visited = set()
        if node_id in visited: return visited
        visited.add(node_id)
        node = self.nodes.get(node_id)
        if node:
            for pid in node.parent_ids: self._ancestors(pid, visited)
        return visited

    def add_comment(self, c: CommentNote) -> None:
        self.comments[c.id] = c; self.touch()

    def remove_comment(self, cid: str) -> None:
        self.comments.pop(cid, None); self.touch()

    def touch(self) -> None:
        self.modified_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "accent_color": self.accent_color,
            "category": self.category,
            "sort_index": self.sort_index,
            "created_at": self.created_at, "modified_at": self.modified_at,
            "nodes":    {nid: n.to_dict() for nid, n in self.nodes.items()},
            "comments": {cid: c.to_dict() for cid, c in self.comments.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LineageTree":
        try:
            tree = cls(
                id=d.get("id", str(uuid.uuid4())), name=d.get("name", "Unnamed"),
                description=d.get("description", ""),
                accent_color=d.get("accent_color", ""),
                category=d.get("category", ""),
                sort_index=int(d.get("sort_index", 0)),
                created_at=d.get("created_at",  datetime.now().isoformat()),
                modified_at=d.get("modified_at", datetime.now().isoformat()),
            )
            for nid, nd in d.get("nodes", {}).items():
                try:    tree.nodes[nid] = PlantNode.from_dict(nd)
                except Exception as e: log.error("Skipping node %s: %s", nid, e)
            for cid, cd in d.get("comments", {}).items():
                try:    tree.comments[cid] = CommentNote.from_dict(cd)
                except Exception as e: log.error("Skipping comment %s: %s", cid, e)
            return tree
        except Exception as e:
            log.error("Error loading LineageTree: %s", e); raise
