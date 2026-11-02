"""
perf.py — Performance profiling and debug logging for BreedingCharts.

Enable via:
  • CLI flag:  python main.py --debug
  • At runtime via Help → Enable Debug Logging

Usage in code:
  import perf

  with perf.timed("scene.load_tree"):
      ... heavy work ...

  @perf.timeit("my_function")
  def my_function():
      ...

  perf.mark("canvas.fit_view called")

All output goes to:
  • The rotating debug.log file (always when debug=True)
  • stderr  (only when debug=True)
  • The in-process stats dict accessible via perf.stats()
"""
from __future__ import annotations

import contextlib
import functools
import logging
import sys
import time
import os
from collections import defaultdict
from typing import Callable, Optional

_enabled: bool = False
_log    = logging.getLogger("bc.perf")

# Accumulate call counts and total ms per label
_call_counts: dict[str, int]   = defaultdict(int)
_total_ms:    dict[str, float] = defaultdict(float)
_last_ms:     dict[str, float] = {}


# ── Public API ────────────────────────────────────────────────────────────────

def enable(log_filepath: Optional[str] = None) -> None:
    """Enable debug/perf logging.  Call before any timed() / timeit() usage."""
    global _enabled
    _enabled = True
    _log.setLevel(logging.DEBUG)

    # Console handler
    if not any(isinstance(h, logging.StreamHandler) for h in _log.handlers):
        sh = logging.StreamHandler()
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d  PERF  %(message)s",
            datefmt="%H:%M:%S"))
        _log.addHandler(sh)

    # File handler — rotating so it doesn't grow unboundedly
    if log_filepath:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_filepath, maxBytes=4*1024*1024, backupCount=2)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S"))
        _log.addHandler(fh)

    _log.debug("=== BreedingCharts debug logging enabled ===")


def disable() -> None:
    global _enabled
    _enabled = False


def is_enabled() -> bool:
    return _enabled


def mark(label: str) -> None:
    """Log a one-off event with a high-resolution timestamp."""
    if not _enabled:
        return
    _log.debug("MARK   %-40s", label)


def stats() -> dict:
    """Return a snapshot of accumulated timing stats."""
    return {
        label: {
            "calls":    _call_counts[label],
            "total_ms": round(_total_ms[label], 2),
            "avg_ms":   round(_total_ms[label] / _call_counts[label], 2)
                        if _call_counts[label] else 0.0,
            "last_ms":  round(_last_ms.get(label, 0.0), 2),
        }
        for label in _call_counts
    }


def reset_stats() -> None:
    _call_counts.clear()
    _total_ms.clear()
    _last_ms.clear()


def format_stats_table() -> str:
    """Return a formatted stats table string for display."""
    rows = sorted(stats().items(), key=lambda x: -x[1]["total_ms"])
    if not rows:
        return "No timing data collected yet."
    lines = [
        f"{'Operation':<42} {'Calls':>6} {'Total ms':>10} {'Avg ms':>9} {'Last ms':>9}",
        "-" * 82,
    ]
    for label, d in rows:
        lines.append(
            f"{label:<42} {d['calls']:>6} {d['total_ms']:>10.1f}"
            f" {d['avg_ms']:>9.2f} {d['last_ms']:>9.2f}"
        )
    return "\n".join(lines)


# ── Context manager ───────────────────────────────────────────────────────────

@contextlib.contextmanager
def timed(label: str):
    """Context manager: measure and log the wall-clock time of a block."""
    if not _enabled:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = (time.perf_counter() - t0) * 1000.0
        _call_counts[label] += 1
        _total_ms[label]    += dt
        _last_ms[label]      = dt
        _log.debug("%-42s  %7.2f ms", label, dt)


# ── Decorator ─────────────────────────────────────────────────────────────────

def timeit(label: Optional[str] = None):
    """Decorator: measure every call to a function.

    Usage:
        @perf.timeit("scene._rebuild_edges")
        def _rebuild_edges(self):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        lbl = label or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _enabled:
                return fn(*args, **kwargs)
            t0 = time.perf_counter()
            result = fn(*args, **kwargs)
            dt = (time.perf_counter() - t0) * 1000.0
            _call_counts[lbl] += 1
            _total_ms[lbl]    += dt
            _last_ms[lbl]      = dt
            _log.debug("%-42s  %7.2f ms", lbl, dt)
            return result

        return wrapper
    return decorator


# ── Qt paint-frame counter ────────────────────────────────────────────────────

_frame_count: int  = 0
_frame_t0:    float = 0.0


def frame_start() -> None:
    """Call at the start of a QGraphicsScene.drawBackground for FPS tracking."""
    global _frame_count, _frame_t0
    if not _enabled:
        return
    now = time.perf_counter()
    _frame_count += 1
    if now - _frame_t0 >= 1.0:
        fps = _frame_count / (now - _frame_t0)
        _log.debug("CANVAS FPS  %.1f", fps)
        _frame_count = 0
        _frame_t0    = now


# ── Process memory (RSS) ──────────────────────────────────────────────────────
# No extra dependency (e.g. psutil) is required — this reads whatever the OS
# already exposes for the current process.

def process_rss_bytes() -> Optional[int]:
    """Best-effort resident-set-size of this process, in bytes.

    Returns None if it can't be determined (unsupported platform, or the
    underlying OS call failed) — callers should display that as "unknown"
    rather than a bogus 0.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb",                          wintypes.DWORD),
                    ("PageFaultCount",               wintypes.DWORD),
                    ("PeakWorkingSetSize",           ctypes.c_size_t),
                    ("WorkingSetSize",                ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage",      ctypes.c_size_t),
                    ("QuotaPagedPoolUsage",          ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage",   ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage",       ctypes.c_size_t),
                    ("PagefileUsage",                 ctypes.c_size_t),
                    ("PeakPagefileUsage",            ctypes.c_size_t),
                ]

            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)

            # GetCurrentProcess() returns the pseudo-handle (HANDLE)-1 — all 64
            # bits set.  Left to ctypes' default 32-bit int restype it comes
            # back as -1 and is then marshalled as 0xFFFFFFFF, which is not a
            # valid handle; the call failed and this reported "unknown" on
            # every Windows machine.  Declaring the signatures explicitly
            # passes the full pointer-width pseudo-handle.
            kernel32 = ctypes.windll.kernel32
            psapi    = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(),
                ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize) if ok else None
        else:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss is bytes on macOS, KiB on Linux/other Unix.
            return ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024
    except Exception as e:
        _log.debug("process_rss_bytes: unavailable: %s", e)
        return None


def format_bytes(n: Optional[int]) -> str:
    """Human-readable byte count, e.g. 4.32 MB. None → 'unknown'."""
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
