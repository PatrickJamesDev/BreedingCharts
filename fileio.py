"""Atomic, GIL-friendly JSON file writes (F3/F58)."""
from __future__ import annotations
import io, json, os, tempfile, threading

try:
    import orjson
except ImportError:          # optional dependency
    orjson = None

_locks: dict = {}
_guard = threading.Lock()


def path_lock(path: str) -> threading.Lock:
    key = os.path.abspath(path)
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomically replace `path` with raw `data` (temp + fsync + os.replace)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def atomic_write_json(path: str, obj, *, pretty: bool = False) -> None:
    """Serialise, write to a temp file in the same folder, fsync, os.replace.

    * An exception while serialising (or writing) leaves the existing file untouched.
    * Never calls json.dumps() on a large object: its C encoder holds the GIL for
      the whole document (measured 517 ms UI freeze from a worker thread).
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    payload = None
    if orjson is not None:
        try:
            payload = orjson.dumps(obj, option=(orjson.OPT_INDENT_2 if pretty else 0)
                                   | orjson.OPT_NON_STR_KEYS)
        except TypeError:
            payload = None          # let the stdlib encoder raise the same error
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            if payload is not None:
                fh.write(payload)
            else:                    # streamed, yields the GIL between chunks
                w = io.TextIOWrapper(fh, encoding="utf-8", write_through=False)
                json.dump(obj, w, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))
                w.flush(); w.detach()
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
