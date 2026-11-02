#!/usr/bin/env python3
"""
build_exe.py — Cross-platform build script for BreedingCharts.

Usage:
    python build_exe.py               # auto-detect platform
    python build_exe.py --platform windows
    python build_exe.py --platform macos
    python build_exe.py --platform linux
    python build_exe.py --clean       # clean build artifacts first

Windows always produces a self-contained single-file .exe placed directly
in the project root:
    BreedingCharts.exe

macOS / Linux produce a folder bundle in dist/:
    dist/BreedingCharts/

Requirements:
    pip install pyinstaller pyqt6 pyqt6-qt6
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME    = "BreedingCharts"
ENTRY_POINT = "main.py"
ICON_ICO    = "icon.ico"
ICON_ICNS   = "icon.icns"
ICON_PNG    = "icon.png"

SRC_DIR   = Path(__file__).parent.resolve()
DIST_DIR  = SRC_DIR / "dist"
BUILD_DIR = SRC_DIR / "build"


def run(cmd: list, **kw) -> int:
    print(f"\n>>> {' '.join(str(c) for c in cmd)}\n")
    return subprocess.run(cmd, **kw).returncode


def clean():
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            print(f"Removing {d} …")
            shutil.rmtree(d)
    for ext in (".exe",):
        p = SRC_DIR / f"{APP_NAME}{ext}"
        if p.exists():
            p.unlink()
            print(f"Removed {p}")


def get_icon(system: str) -> list:
    mapping = {"windows": ICON_ICO, "darwin": ICON_ICNS, "linux": ICON_PNG}
    p = SRC_DIR / mapping.get(system, "")
    return ["--icon", str(p)] if p.exists() else []


def _write_win_runtime_hook() -> Path:
    """
    Write a runtime hook that registers sys._MEIPASS as a DLL directory.

    Python 3.8 changed Windows DLL loading: directories are no longer added
    to the search path implicitly.  When PyInstaller's --onefile bootloader
    extracts the bundle into %TEMP%\\_MEIxxxxxx it does NOT register that
    directory, so python3XX.dll (and Qt DLLs) cannot be found — producing
    "Failed to load python DLL".  This hook runs before any imports and fixes
    the search path in time.
    """
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    hook = BUILD_DIR / "_win_dll_hook.py"
    hook.write_text(
        "import os, sys\n"
        "# Register the PyInstaller extraction temp dir so Windows can find\n"
        "# python3XX.dll and Qt DLLs (required for --onefile on Python 3.8+).\n"
        "if sys.platform == 'win32' and hasattr(sys, '_MEIPASS'):\n"
        "    os.add_dll_directory(sys._MEIPASS)\n",
        encoding="utf-8",
    )
    return hook


def _find_python_dll() -> list:
    """
    Return --add-binary args to explicitly embed python3XX.dll.

    When building inside a venv, sys.executable points into the venv's
    Scripts/ folder, but python3XX.dll only exists in the BASE Python install
    (sys.base_exec_prefix).  PyInstaller misses it in that case, producing
    "Failed to load Python DLL" at runtime because the file is absent from
    the bundled archive.
    """
    if sys.platform != "win32":
        return []

    py_ver   = f"{sys.version_info.major}{sys.version_info.minor}"
    dll_name = f"python{py_ver}.dll"

    candidates = [
        # Base Python install — THE canonical location, reliable even in a venv
        Path(sys.base_exec_prefix) / dll_name,
        Path(sys.exec_prefix)      / dll_name,
        # Next to the Python executable (bare install, not a venv)
        Path(sys.executable).parent / dll_name,
        # One level up from Scripts/ (venv Scripts/ → venv root → base)
        Path(sys.executable).parent.parent / dll_name,
        # Windows system directories as last resort
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / dll_name,
    ]

    for p in candidates:
        if p.is_file():
            print(f"  Found {dll_name} at {p}")
            return ["--add-binary", f"{p}{os.pathsep}."]

    # Final fallback: ask Windows' own 'where' utility
    try:
        result = subprocess.run(
            ["where", dll_name], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            first = result.stdout.strip().splitlines()[0].strip()
            if first and Path(first).is_file():
                print(f"  Found {dll_name} via 'where': {first}")
                return ["--add-binary", f"{first}{os.pathsep}."]
    except Exception:
        pass

    print(f"  ⚠  {dll_name} not found — the exe may fail on machines "
          f"without Python {sys.version_info.major}.{sys.version_info.minor} installed.")
    return []


def build_windows():
    """
    Build a self-contained single-file exe placed in the project root.

    --onefile is always used so the exe can be run from any directory without
    needing adjacent DLLs.  The runtime hook fixes the DLL search path inside
    the %TEMP% extraction folder that PyInstaller uses at launch.

    Path / file-location notes
    --------------------------
    In the bundled exe ``__file__`` (and ``os.path.abspath(__file__)``) resolve
    to a path inside PyInstaller's temporary extraction folder (_MEIPASS), which
    is **read-only** and is deleted when the process exits.  The app detects this
    via ``sys.frozen`` / ``sys._MEIPASS`` and writes all persistent files (
    settings.json, undo_history.json, data_dir.txt, plant_database.json) next to
    the exe instead (``os.path.dirname(sys.executable)``).
    The user's lineage (.plf) files always go to the configured data directory
    (default: ~/BreedingCharts_Data).
    """
    print("=" * 60)
    print("Building for Windows…")
    print("=" * 60)

    hook           = _write_win_runtime_hook()
    python_dll_args = _find_python_dll()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name",        APP_NAME,
        "--windowed",                          # no console window
        "--noconfirm",
        "--clean",
        "--onefile",                           # single self-contained exe
        f"--distpath={SRC_DIR}",              # output → project root
        f"--workpath={BUILD_DIR}",
        f"--specpath={BUILD_DIR}",            # generated .spec → build/ not root
        "--runtime-hook", str(hook),           # DLL search-path fix
        "--add-data", f"{SRC_DIR}/*.py{os.pathsep}.",
        "--hidden-import", "PyQt6",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtGui",
        "--hidden-import", "PyQt6.QtCore",
        "--collect-all",     "PyQt6",
        "--copy-metadata",   "PyQt6",         # needed by some PyQt6 entry points
    ]
    cmd += python_dll_args
    cmd += get_icon("windows")
    cmd.append(str(SRC_DIR / ENTRY_POINT))

    rc = run(cmd)
    exe = SRC_DIR / f"{APP_NAME}.exe"
    if rc == 0:
        print(f"\n✅  Windows build complete → {exe}")
    else:
        print(f"\n❌  Windows build failed (exit code {rc})")
    return rc


def build_macos():
    print("=" * 60)
    print("Building for macOS…")
    print("=" * 60)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name",        APP_NAME,
        "--windowed",
        "--noconfirm",
        "--clean",
        "--osx-bundle-identifier", "com.breedingcharts.app",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={BUILD_DIR}",
        "--add-data", f"{SRC_DIR}/*.py:.",
        "--hidden-import", "PyQt6",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtGui",
        "--hidden-import", "PyQt6.QtCore",
        "--collect-all",   "PyQt6",
        "--copy-metadata", "PyQt6",
    ]
    cmd += get_icon("darwin")
    cmd.append(str(SRC_DIR / ENTRY_POINT))

    rc = run(cmd)
    if rc == 0:
        app = DIST_DIR / f"{APP_NAME}.app"
        print(f"\n✅  macOS build complete → {app}")
        print("   To create a DMG: hdiutil create -volname BreedingCharts "
              f"-srcfolder {app} -ov -format UDZO dist/BreedingCharts.dmg")
    else:
        print(f"\n❌  macOS build failed (exit code {rc})")
    return rc


def build_linux():
    print("=" * 60)
    print("Building for Linux…")
    print("=" * 60)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name",        APP_NAME,
        "--noconfirm",
        "--clean",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={BUILD_DIR}",
        "--add-data", f"{SRC_DIR}/*.py:.",
        "--hidden-import", "PyQt6",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtGui",
        "--hidden-import", "PyQt6.QtCore",
        "--collect-all",   "PyQt6",
        "--copy-metadata", "PyQt6",
    ]
    cmd += get_icon("linux")
    cmd.append(str(SRC_DIR / ENTRY_POINT))

    rc = run(cmd)
    if rc == 0:
        print(f"\n✅  Linux build complete → {DIST_DIR / APP_NAME}")
    else:
        print(f"\n❌  Linux build failed (exit code {rc})")
    return rc


def check_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        print(f"PyInstaller {PyInstaller.__version__} found.")
        return True
    except ImportError:
        print("PyInstaller not found. Installing…")
        return run([sys.executable, "-m", "pip", "install", "pyinstaller"]) == 0


def main():
    parser = argparse.ArgumentParser(description="Build BreedingCharts executable")
    parser.add_argument("--platform", choices=["windows", "macos", "linux", "auto"],
                        default="auto")
    parser.add_argument("--clean",   action="store_true", help="Clean build dirs first")
    parser.add_argument("--onefile", action="store_true",
                        help="Windows: always on (ignored). Reserved for future use on other platforms.")
    args = parser.parse_args()

    print("🌿  BreedingCharts — Build Script")
    print(f"   Python:   {sys.version.split()[0]}")
    print(f"   Platform: {platform.system()} {platform.machine()}")
    print(f"   Source:   {SRC_DIR}")

    if not check_pyinstaller():
        print("❌  Could not install PyInstaller. Aborting.")
        sys.exit(1)

    if args.clean:
        clean()

    system = platform.system().lower()
    target = args.platform if args.platform != "auto" else {
        "windows": "windows",
        "darwin":  "macos",
        "linux":   "linux",
    }.get(system, "linux")

    builders = {
        "windows": build_windows,
        "macos":   build_macos,
        "linux":   build_linux,
    }
    sys.exit(builders[target]())


if __name__ == "__main__":
    main()
