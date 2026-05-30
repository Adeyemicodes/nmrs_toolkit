#!/usr/bin/env python3
"""
build.py — Cross-platform build script for NMRS Toolkit.

Runs PyInstaller against NMRSToolkit_v1.1.2.spec (PyInstaller's build/ and
dist/ stay at the project root, which is what we want), then wraps the
resulting binary into a shippable folder + zip under bundles/.

Layout after a run:
    build/                                          (PyInstaller intermediates)
    dist/NMRSToolkit_v1.1.2[.exe]                   (raw binary)
    bundles/NMRSToolkit_<OS>_v1_1_2/                (shippable folder)
        NMRSToolkit_v1.1.2[.exe]
        README.md
        .nmrs_config.example.ini
        decrypt_nmrs_backup.py
    bundles/NMRSToolkit_<OS>_v1_1_2.zip             (zipped folder)

Where <OS> is one of: Ubuntu, Windows, macOS.

Usage:
    # Linux / macOS:
    python3 build.py

    # Windows (CMD or PowerShell):
    python build.py

Rebuilds are incremental — PyInstaller caches in build/.
"""

import importlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

APP_NAME = "NMRSToolkit_v1.1.2"
APP_VERSION_TAG = "v1_1_2"
SPEC_FILE = f"{APP_NAME}.spec"

# Runtime imports the frozen app needs at startup. PyInstaller only bundles
# modules importable in THIS interpreter (sys.executable), so a dep missing
# here silently produces a broken binary that crashes on the user's machine
# (e.g. "ModuleNotFoundError: No module named 'cryptography'"). We check up
# front and fail loudly with the exact fix instead.
# Each entry: (import name, pip package | None for stdlib that pip can't install)
REQUIRED_IMPORTS = [
    ("cryptography", "cryptography"),
    ("mysql.connector", "mysql-connector-python"),
    ("tkinter", None),
]

# Files copied alongside the binary in the shippable bundle.
EXTRA_FILES = [
    "FACILITY_GUIDE.md",
    "README.md",
    ".nmrs_config.example.ini",
    "decrypt_nmrs_backup.py",
]


def os_tag() -> str:
    system = platform.system()
    if system == "Linux":
        return "Ubuntu"
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "macOS"
    return system  # fallback for anything exotic


def _import_error(module: str):
    """Actually import `module` in this interpreter and return the failure, or
    None on success. A real import (not just find_spec) is required: find_spec
    only locates the top-level package folder, so a broken install — e.g.
    cryptography present but its compiled Rust binding won't load — passes
    find_spec yet fails at runtime. PyInstaller then silently drops the package
    and the frozen app dies with "No module named 'cryptography'". Importing
    here reproduces what PyInstaller's analyser sees and fails the build first."""
    try:
        importlib.import_module(module)
        return None
    except Exception as e:  # ImportError, but also OSError/DLL-load failures
        return e


def check_dependencies():
    print(f"[build] Checking runtime dependencies in {sys.executable} ...")
    missing_pip = []
    missing_stdlib = []
    broken = []  # importable folder present but import raised — i.e. broken install
    for module, pip_name in REQUIRED_IMPORTS:
        err = _import_error(module)
        if err is None:
            continue
        if isinstance(err, ModuleNotFoundError) and err.name in (module, module.split(".")[0]):
            # genuinely not installed
            (missing_pip if pip_name else missing_stdlib).append(pip_name or module)
        else:
            # installed but broken (bad binding, partial wheel, sub-dep missing)
            broken.append((module, pip_name, err))
    if not missing_pip and not missing_stdlib and not broken:
        return
    lines = ["", "Build dependency problem in this interpreter:",
             f"  {sys.executable}", ""]
    if missing_pip:
        lines.append("Not installed — install, then re-run this build:")
        lines.append(f'  "{sys.executable}" -m pip install ' + " ".join(missing_pip))
        lines.append("  (or:  \"%s\" -m pip install -r requirements.txt)" % sys.executable)
    if broken:
        lines.append("")
        lines.append("Installed but failed to import (broken/partial install) — "
                     "reinstall, then re-run this build:")
        reinstallable = [p for _m, p, _e in broken if p]
        if reinstallable:
            lines.append(f'  "{sys.executable}" -m pip install --force-reinstall '
                         + " ".join(reinstallable))
        for module, _pip, err in broken:
            lines.append(f"    {module}: {type(err).__name__}: {err}")
    if missing_stdlib:
        lines.append("")
        lines.append("Missing standard-library module(s), not pip-installable: "
                     + ", ".join(missing_stdlib))
        lines.append("Reinstall Python from python.org with the 'tcl/tk and IDLE' "
                     "component selected.")
    sys.exit("\n".join(lines))


def run_pyinstaller():
    print(f"[build] Running PyInstaller (spec: {SPEC_FILE})...")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", SPEC_FILE]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit("PyInstaller not found. Install with:  pip install pyinstaller")
    except subprocess.CalledProcessError as e:
        sys.exit(f"PyInstaller failed (exit code {e.returncode})")


def find_binary() -> Path:
    candidates = [
        Path("dist") / f"{APP_NAME}.exe",   # Windows
        Path("dist") / APP_NAME,            # Linux / macOS
    ]
    for p in candidates:
        if p.is_file():
            return p
    sys.exit(f"Expected binary not found. Looked in: "
             + ", ".join(str(c) for c in candidates))


def _robust_rmtree(path: Path):
    """shutil.rmtree that survives Windows read-only files and transient locks.

    Windows raises PermissionError (WinError 5) when deleting a file that is
    read-only or held open by another process — the leftover .exe from a prior
    build is the usual victim (antivirus scanning a fresh binary, an Explorer
    preview, or a still-running instance of the app). We clear the read-only
    bit and retry on the offending file, and retry the whole tree a few times
    to ride out transient antivirus locks. If it's a hard lock (the app is
    actually running), we bail with an actionable message instead of a stack
    trace."""
    def _clear_readonly_and_retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise  # genuinely locked — let the outer retry loop handle it

    last_err = None
    for attempt in range(5):
        try:
            # rmtree's callback kwarg was renamed onerror -> onexc in 3.12.
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_clear_readonly_and_retry)
            else:
                shutil.rmtree(path, onerror=_clear_readonly_and_retry)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(1.0)  # give antivirus a moment to release the handle
    sys.exit(
        f"Could not remove the previous bundle folder:\n  {path}\n  {last_err}\n\n"
        f"On Windows this means the old {APP_NAME}.exe is still locked. Try:\n"
        f"  1. Close any running {APP_NAME}.exe (check Task Manager).\n"
        "  2. Close any Explorer window showing the bundles\\ folder.\n"
        "  3. Pause antivirus real-time scanning briefly, then re-run the build.\n"
        f"Or delete it manually:  rmdir /s /q {path}"
    )


def wrap_bundle(binary: Path, os_label: str) -> Path:
    bundle_name = f"NMRSToolkit_{os_label}_{APP_VERSION_TAG}"
    bundles_dir = Path("bundles")
    wrapped = bundles_dir / bundle_name

    print(f"[build] Wrapping into {wrapped}/ ...")
    if wrapped.exists():
        _robust_rmtree(wrapped)
    wrapped.mkdir(parents=True)

    dst_binary = wrapped / binary.name
    shutil.copy2(binary, dst_binary)
    if os_label != "Windows":
        dst_binary.chmod(0o755)

    for name in EXTRA_FILES:
        src = Path(name)
        if src.exists():
            shutil.copy2(src, wrapped / name)
        else:
            print(f"[build]   (skip missing: {name})")
    return wrapped


def make_zip(wrapped: Path) -> Path:
    zip_path = wrapped.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    print(f"[build] Zipping {zip_path} ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in wrapped.rglob("*"):
            zf.write(p, p.relative_to(wrapped.parent))
    return zip_path


def main():
    if not Path(SPEC_FILE).exists():
        sys.exit(f"Spec file not found: {SPEC_FILE}\nRun this from the project root.")
    check_dependencies()
    run_pyinstaller()
    binary = find_binary()
    print(f"[build] Built: {binary}")
    label = os_tag()
    wrapped = wrap_bundle(binary, label)
    zip_path = make_zip(wrapped)
    print()
    print("Folder:")
    for p in sorted(wrapped.iterdir()):
        print(f"  {p}")
    print()
    print(f"Zip:    {zip_path}  ({zip_path.stat().st_size:,} bytes)")
    print("[build] Done.")


if __name__ == "__main__":
    main()
