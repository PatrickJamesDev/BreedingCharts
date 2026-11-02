"""
main.py — Entry point for BreedingCharts.

Usage:
    python main.py           # normal mode
    python main.py --debug   # enable performance profiling + verbose logging
                             # output goes to debug.log in the app directory

v1.2.0:
  • --debug flag enables perf.py timing on all key operations
  • data_dir setting applied before any file I/O
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from log_setup import setup_logging
setup_logging()

import logging
log = logging.getLogger(__name__)

_DEBUG = "--debug" in sys.argv

# ── Activate profiling ASAP so all imports are timed if desired ───────────────
import perf
if _DEBUG:
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
    perf.enable(log_filepath=log_path)
    perf.mark("main.py startup")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from settings import load_settings, save_settings, build_stylesheet
import database
from ui_main import MainWindow


__version__ = "1.24"


def main() -> None:
    log.info("Launching BreedingCharts v%s (debug=%s)", __version__, _DEBUG)
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("BreedingCharts")
        app.setOrganizationName("Botanical Tools")
        app.setApplicationVersion(__version__)

        with perf.timed("load_settings"):
            s = load_settings()

        # ── Wire data directory from settings ─────────────────────────────────
        # If a data_dir was previously chosen, tell database.py to use it so
        # all file operations (load, save, sidebar scan) go to the right folder.
        if getattr(s, "data_dir", ""):
            import os as _os
            if _os.path.isdir(s.data_dir):
                database.set_data_dir(s.data_dir)
                # Invalidate plant_db cache so it reads from the correct location
                import plant_db as _plant_db
                _plant_db.reset_cache()
            else:
                log.warning("Saved data_dir does not exist: %s — using default", s.data_dir)

        # Set the thumbnail RAM cache limit (default Qt limit is only 10 MB,
        # too small once a database has meaningfully more than ~100 photos).
        # A fixed number is either wasteful for a small collection or
        # undersized for a huge one, so by default (pixmap_cache_mb == 0)
        # this scales with how many plants in the database actually have a
        # photo. The person can pin it to a fixed value instead in Settings
        # if they'd rather have a predictable RAM ceiling. See
        # plant_db.recommended_pixmap_cache_kb() for the sizing formula and
        # a note on what happens if it's ever exceeded (cache eviction, not
        # a crash).
        from PyQt6.QtGui import QPixmapCache
        import plant_db as _plant_db
        cache_kb = (s.pixmap_cache_mb * 1024 if getattr(s, "pixmap_cache_mb", 0) > 0
                    else _plant_db.recommended_pixmap_cache_kb())
        QPixmapCache.setCacheLimit(cache_kb)
        log.info("Pixmap cache limit set to %.0f MB (%s)", cache_kb / 1024,
                  "manual" if s.pixmap_cache_mb > 0 else "auto")

        with perf.timed("build_stylesheet"):
            app.setStyleSheet(build_stylesheet(s))

        with perf.timed("MainWindow.__init__"):
            window = MainWindow(s)

        window.show()
        perf.mark("window.show() complete")

        from canvas_view import warmup_decode_threadpool
        QTimer.singleShot(0, warmup_decode_threadpool)

        code = app.exec()
        log.info("Application exited with code %d", code)

        if _DEBUG:
            # Print a full timing summary to the log on exit
            summary = perf.format_stats_table()
            perf.mark("=== TIMING SUMMARY ===")
            for line in summary.splitlines():
                perf._log.debug(line)
            print("\n" + summary)

        sys.exit(code)
    except Exception as e:
        log.critical("Fatal error in main(): %s", e, exc_info=True)
        raise


if __name__ == "__main__":
    main()
