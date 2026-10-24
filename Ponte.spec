# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

# The bundled Python has an explicit CA rejection list loaded by SSLContext.
# Preserve that policy in the executable instead of changing SSL verification.
ssl_policy = Path(sys.base_prefix) / 'Lib' / 'codex-rejected-cas.pem'


a = Analysis(
    ['entrypoint.py'],
    pathex=['.'],
    binaries=[],
    datas=[('assets/ponte.svg', 'assets'), ('assets/ponte.ico', 'assets'),
           ('pontelan/update_config.json', 'pontelan')]
          + ([(str(ssl_policy), '.')] if ssl_policy.exists() else []),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt uses the Windows system ICU API (unversioned exports). The desktop
# dependency PATH also includes Poppler's ICU78, which PyInstaller mistakenly
# collects. Its versioned exports are incompatible; use the system ICU instead.
a.binaries = [item for item in a.binaries if item[0].replace('\\', '/').split('/')[-1].lower() not in {'icuuc.dll', 'icudt78.dll'}]
# Pin Python's OpenSSL DLLs as well: another library's DLL with the same name
# can import successfully yet fail when SSLContext creates its providers.
import sys
from pathlib import Path
a.binaries = [(item[0], str(Path(sys.base_prefix) / 'DLLs' / Path(item[0]).name), item[2])
              if item[0].lower() in {'libcrypto-3-x64.dll', 'libssl-3-x64.dll'} else item
              for item in a.binaries]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Ponte',
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
    version='version_info.txt',
    icon=['assets/ponte.ico'],
)





