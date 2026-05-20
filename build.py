#!/usr/bin/env python3
"""
build.py — Cross-platform build script for NMRS Toolkit.

Runs PyInstaller against NMRSToolkit_v1.0.0.spec (PyInstaller's build/ and
dist/ stay at the project root, which is what we want), then wraps the
resulting binary into a shippable folder + zip under bundles/.

Layout after a run:
    build/                                          (PyInstaller intermediates)
    dist/NMRSToolkit_v1.0.0[.exe]                   (raw binary)
    bundles/NMRSToolkit_<OS>_v1_0_0/                (shippable folder)
        NMRSToolkit_v1.0.0[.exe]
        README.md
        .nmrs_config.example.ini
        decrypt_nmrs_backup.py
    bundles/NMRSToolkit_<OS>_v1_0_0.zip             (zipped folder)

Where <OS> is one of: Ubuntu, Windows, macOS.

Usage:
    # Linux / macOS:
    python3 build.py

    # Windows (CMD or PowerShell):
    python build.py

Rebuilds are incremental — PyInstaller caches in build/.
"""

import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_NAME = "NMRSToolkit_v1.0.0"
APP_VERSION_TAG = "v1_0_0"
SPEC_FILE = f"{APP_NAME}.spec"

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


def wrap_bundle(binary: Path, os_label: str) -> Path:
    bundle_name = f"NMRSToolkit_{os_label}_{APP_VERSION_TAG}"
    bundles_dir = Path("bundles")
    wrapped = bundles_dir / bundle_name

    print(f"[build] Wrapping into {wrapped}/ ...")
    if wrapped.exists():
        shutil.rmtree(wrapped)
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
