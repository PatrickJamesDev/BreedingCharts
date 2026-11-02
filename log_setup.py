"""
log_setup.py — Logging configuration and global crash handler.

Call setup_logging() once at startup. After that, all modules can do:
    import logging
    log = logging.getLogger(__name__)
    log.info("...")

Crashes are caught by the excepthook and written to breedingcharts.log
before the default traceback is shown.
"""
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, "breedingcharts.log")


def setup_logging() -> None:
    """Configure root logger: file (rotating 2 MB × 3) + stderr."""
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  —  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    stderr_handler.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    # Global exception hook — logs tracebacks before crashing
    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.getLogger("crash").critical("Unhandled exception:\n%s", msg)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook
    logging.info("BreedingCharts starting — log: %s", LOG_FILE)
