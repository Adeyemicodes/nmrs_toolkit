# -*- mode: python ; coding: utf-8 -*-

# Workaround for a PyInstaller (6.x) issue on Linux with system Python:
# the bootloader looks for libpython3.X.so (unversioned), but Analysis
# only auto-discovers libpython3.X.so.1.0 — so the bundled binary fails
# at startup with "Failed to load Python shared library".
# Resolve the unversioned name via ctypes.util.find_library() and copy
# it into a build-time temp dir under that exact name so it lands in
# the bundle as "libpython3.X.so".
import ctypes.util
import os
import shutil
import sys
import tempfile

from PyInstaller.utils.hooks import collect_all

# cryptography ships its crypto primitives as a compiled Rust extension
# (cryptography.hazmat.bindings._rust). PyInstaller's static import analysis
# does not reliably follow into it, so the package gets dropped and the frozen
# app dies with "No module named 'cryptography'". collect_all() pulls the
# package's submodules, binaries, and data files in explicitly.
_crypto_datas, _crypto_binaries, _crypto_hiddenimports = collect_all('cryptography')

_extra_binaries = []
_lib_name = f"python{sys.version_info.major}.{sys.version_info.minor}"
_lib_path = ctypes.util.find_library(_lib_name)
if _lib_path:
    if not os.path.isabs(_lib_path):
        # find_library returns the soname on Linux; resolve to an absolute path.
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
    ['nmrs_toolkit.py'],
    pathex=[],
    binaries=_extra_binaries + _crypto_binaries,
    datas=[
        # Ship only the template — NEVER bundle a live config (would bake
        # real DB credentials and backup keys into the binary).
        ('.nmrs_config.example.ini', '.'),
        ('scripts', 'scripts'),
    ] + _crypto_datas,
    hiddenimports=[
        # mysql.connector lazy-imports its locale module when formatting any
        # error message. PyInstaller's static analyser misses this, so we list
        # it explicitly — otherwise an SQL error surfaces as the misleading
        # "no localization support for language 'eng'" instead of the real cause.
        'mysql.connector.locales',
        'mysql.connector.locales.eng',
        'mysql.connector.locales.eng.client_error',
        # mysql-connector-python 9.x loads each auth plugin via a dynamic
        # importlib.import_module(f".{plugin_name}", "mysql.connector.plugins")
        # in get_auth_plugin(). PyInstaller's static analyser can't see that
        # f-string import, so the plugin modules are dropped and login fails
        # with "Authentication plugin '...' is not supported". List the
        # password-based plugins explicitly. (Kerberos/LDAP/OCI/WebAuthn plugins
        # are omitted: they need heavy optional third-party packages and aren't
        # used for standard MySQL password auth.)
        'mysql.connector.plugins',
        'mysql.connector.plugins.mysql_native_password',
        'mysql.connector.plugins.caching_sha2_password',
        'mysql.connector.plugins.sha256_password',
        'mysql.connector.plugins.mysql_clear_password',
    ] + _crypto_hiddenimports,
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
    name='NMRSToolkit_v1.1.1',
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
