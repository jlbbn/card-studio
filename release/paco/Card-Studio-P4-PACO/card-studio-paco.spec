import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(os.environ["CARD_STUDIO_SOURCE"]).resolve()
app_name = "Card Studio"

a = Analysis(
    [str(project_root / "card_studio_app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "card_studio" / "cardpeek" / "studio.lua"), "card_studio/cardpeek")],
    hiddenimports=collect_submodules("card_studio") + [
        "gi.repository.Adw",
        "gi.repository.Gdk",
        "gi.repository.Gio",
        "gi.repository.GLib",
        "gi.repository.GObject",
        "gi.repository.Gtk",
        "smartcard.System",
        "smartcard.scard",
    ],
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
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name=f"{app_name}.app",
    icon="assets/swift-ledger-card-studio.icns",
    bundle_identifier="us.swiftledger.CardStudio",
    info_plist={
        "CFBundleDisplayName": app_name,
        "CFBundleShortVersionString": "1.2.2",
        "CFBundleVersion": "1",
        "CardStudioReleaseVersion": "1.2.2-p4-rc2",
        "CardStudioSourceCommit": "12ffad68c0cf86fdadb301d331f61f02feb5acf7",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
