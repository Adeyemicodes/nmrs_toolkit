# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for NMRS Toolkit v2 (PyWebView).

Differences from the v1.2.1 spec:
  * Entry is run.py (-> nmrs_toolkit package) instead of the legacy single file.
  * Bundles nmrs_toolkit/frontend/ (HTML/CSS/JS) at the package-relative path
    that app.frontend_dir() looks up under sys._MEIPASS.
  * Collects pywebview + its GTK backend (gi/WebKit2 are loaded dynamically).

The libpython / cryptography / mysql-connector handling is carried over verbatim
from the v1.2.1 spec (same hard-won fixes).
"""
import ctypes.util
import os
import shutil
import sys
import tempfile

from PyInstaller.utils.hooks import collect_all, collect_submodules

# cryptography ships a compiled Rust extension PyInstaller's static analysis
# doesn't reliably follow into — collect everything explicitly.
_crypto_datas, _crypto_binaries, _crypto_hiddenimports = collect_all('cryptography')

# pywebview ships per-platform backends + JS assets loaded at runtime.
_wv_datas, _wv_binaries, _wv_hiddenimports = collect_all('webview')

_extra_binaries = []
_lib_name = f"python{sys.version_info.major}.{sys.version_info.minor}"
_lib_path = ctypes.util.find_library(_lib_name)
if _lib_path:
    if not os.path.isabs(_lib_path):
        for prefix in ("/usr/lib/x86_64-linux-gnu",
                       "/lib/x86_64-linux-gnu", "/usr/lib", "/lib"):
            candidate = os.path.join(prefix, _lib_path)
            if os.path.exists(candidate):
                _lib_path = candidate
                break
    if os.path.isabs(_lib_path) and os.path.exists(_lib_path):
        _unversioned = f"lib{_lib_name}.so"
        _staging = tempfile.mkdtemp(prefix="pyi_libpython_")
        _dest = os.path.join(_staging, _unversioned)
        shutil.copy2(_lib_path, _dest)
        _extra_binaries.append((_dest, "."))


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=_extra_binaries + _crypto_binaries + _wv_binaries,
    datas=[
        # Ship only the template — NEVER bundle a live config.
        ('.nmrs_config.example.ini', '.'),
        ('scripts', 'scripts'),
        # v2 frontend bundle. app.frontend_dir() resolves this under _MEIPASS.
        ('nmrs_toolkit/frontend', 'nmrs_toolkit/frontend'),
    ] + _crypto_datas + _wv_datas,
    hiddenimports=[
        # Package modules that __main__ imports lazily (PyInstaller's static
        # analysis won't see the in-function `from .app import run_gui`, etc.).
        'nmrs_toolkit', 'nmrs_toolkit.__main__', 'nmrs_toolkit.app',
        'nmrs_toolkit.bridge', 'nmrs_toolkit.headless', 'nmrs_toolkit.ui_tk',
        # pywebview GTK backend (chosen at runtime via importlib).
        'webview.platforms.gtk',
        'gi', 'gi.repository.Gtk', 'gi.repository.Gdk',
        'gi.repository.WebKit2', 'gi.repository.Soup',
        # mysql-connector lazy imports (carried over from the v1.2.1 spec).
        'mysql.connector.locales',
        'mysql.connector.locales.eng',
        'mysql.connector.locales.eng.client_error',
        'mysql.connector.plugins',
        'mysql.connector.plugins.mysql_native_password',
        'mysql.connector.plugins.caching_sha2_password',
        'mysql.connector.plugins.sha256_password',
        'mysql.connector.plugins.mysql_clear_password',
    ] + _crypto_hiddenimports + _wv_hiddenimports
       + collect_submodules('nmrs_toolkit.workflows'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NMRSToolkit_v2.0.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
