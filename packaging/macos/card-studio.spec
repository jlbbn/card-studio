from importlib.util import find_spec
from pathlib import Path

project_root = Path(SPECPATH).resolve().parents[1]
app_name = "Swift Ledger Card Studio"

jpype_module = find_spec("jpype")
if jpype_module is None or jpype_module.origin is None:
    raise RuntimeError("JPype1 debe estar instalado para crear el bundle de macOS.")
jpype_support_jar = Path(jpype_module.origin).resolve().parent.parent / "org.jpype.jar"
if not jpype_support_jar.is_file():
    raise FileNotFoundError(f"No se encontró la biblioteca de soporte de JPype: {jpype_support_jar}")

a = Analysis(
    [str(project_root / "card_studio_app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "card_studio" / "cardpeek" / "studio.lua"), "card_studio/cardpeek"),
        (
            str(project_root / "vendor/GlobalPlatformPro-next/tool/target/gp.jar"),
            "vendor/GlobalPlatformPro-next/tool/target",
        ),
        (str(jpype_support_jar), "."),
    ],
    hiddenimports=[
        "_jpype",
        "gi.repository.Adw",
        "gi.repository.Gdk",
        "gi.repository.Gio",
        "gi.repository.GLib",
        "gi.repository.GObject",
        "gi.repository.Gtk",
        "jpype",
        "packaging.version",
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
    icon=None,
    bundle_identifier="us.swiftledger.CardStudio",
    info_plist={
        "CFBundleDisplayName": app_name,
        "CFBundleShortVersionString": "1.2.3",
        "CFBundleVersion": "2",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
