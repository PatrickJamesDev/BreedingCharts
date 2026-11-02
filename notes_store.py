"""
notes_store.py — Persistence for the Notes feature (breeder's log /
free-form notes, shown as a Google-Keep-style grid).

Deliberately much simpler than plant_db.py / lineage_images.py: one
notes.json file in the data folder, loaded once and cached, rewritten on
every change. Notes are expected to be modest in number (dozens, not
hundreds), so the lazy-load/evict machinery those modules use to stay
scalable to large photo collections wouldn't earn its keep here — and
pasted images live inline in each note's HTML rather than in a separate
sidecar (see models.Note), so there's no image store to manage at all.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List, Optional

from models import Note

log = logging.getLogger(__name__)

_cache: Optional[Dict[str, Note]] = None
# Derived views over the cache.  Both are invalidated by any mutation, so
# repeated get_all()/search() calls (typing in the search box, refreshing
# after an edit) never re-sort thousands of notes or re-strip HTML twice.
_sorted_cache: Optional[List[Note]] = None
_plain_cache: Dict[str, tuple[str, str]] = {}   # id -> (modified_at, text)


def _notes_file() -> str:
    import database as _db
    return os.path.join(_db.get_data_dir(), "notes.json")


def _invalidate() -> None:
    global _sorted_cache
    _sorted_cache = None


def reset_cache() -> None:
    """Invalidate the cache (call when the data directory changes)."""
    global _cache, _sorted_cache
    _cache = None
    _sorted_cache = None
    _plain_cache.clear()


def _load() -> Dict[str, Note]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    _invalidate()
    _plain_cache.clear()
    path = _notes_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for nid, d in raw.items():
            try:
                _cache[nid] = Note.from_dict(d)
            except Exception as e:
                log.warning("notes_store: skipping bad note %s: %s", nid, e)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("notes_store: load error: %s", e)
    return _cache


def _save() -> None:
    path = _notes_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        out = {nid: n.to_dict() for nid, n in (_cache or {}).items()}
        from fileio import atomic_write_json
        atomic_write_json(path, out)
    except Exception as e:
        log.error("notes_store: save error: %s", e)


def get_all() -> List[Note]:
    """Pinned notes first; each group most-recently-modified first —
    matches Google Keep's default ordering."""
    global _sorted_cache
    if _sorted_cache is None:
        notes = list(_load().values())
        notes.sort(key=lambda n: n.modified_at, reverse=True)
        notes.sort(key=lambda n: 0 if n.pinned else 1)
        _sorted_cache = notes
    return _sorted_cache


def get(note_id: str) -> Optional[Note]:
    return _load().get(note_id)


def upsert(note: Note) -> None:
    note.touch()
    _load()[note.id] = note
    _invalidate()
    _save()


def delete(note_id: str) -> None:
    if _load().pop(note_id, None) is not None:
        _invalidate()
        _save()


import html as _html_lib

_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head>", re.IGNORECASE | re.DOTALL)
_TAG_RE  = re.compile(r"<[^>]+>")


def plain_text(html: str) -> str:
    """Strip tags for search/preview purposes. Not meant to be a real HTML
    parser — good enough for the markup QTextEdit produces. Qt's
    toHtml() always includes a <head><style>…</style></head> block full
    of raw CSS, so that has to be dropped wholesale (not just its tags)
    before stripping the rest, or the CSS text leaks straight into note
    previews and search results."""
    text = _HEAD_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", text)
    text = _html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _plain_for(note: Note) -> str:
    """plain_text() memoised per note.  Stripping Qt's full HTML (with its
    CSS <head> block and any inline base64 images) is expensive, and the
    search box calls this on every keystroke; the cache key is
    (id, modified_at) so an edited note is re-stripped exactly once."""
    cached = _plain_cache.get(note.id)
    if cached is not None and cached[0] == note.modified_at:
        return cached[1]
    text = plain_text(note.html)
    _plain_cache[note.id] = (note.modified_at, text)
    return text


def plain_text_for(note: Note) -> str:
    """Memoised plain_text(note.html) — the accessor callers should use.

    Note cards render this preview and search matches against it, so both
    walk the whole list; calling plain_text() directly re-strips Qt's full
    HTML — including any inline base64 image — on every single call.
    """
    return _plain_for(note)


def search(query: str) -> List[Note]:
    q = query.strip().lower()
    if not q:
        return get_all()
    return [n for n in get_all()
            if q in n.title.lower() or q in _plain_for(n).lower()]
