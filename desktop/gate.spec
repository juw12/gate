# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for gate on Windows and macOS. Build with  python desktop/build.py
#   Windows  dist/gate/gate.exe (+ _internal/)  - no console; its window uses Edge WebView2 (pywebview)
#   macOS    dist/gate.app                      - a normal Dock app; its window uses WKWebView (pywebview)
import os
import sys

DESKTOP = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821 (provided by PyInstaller)
VERSION = os.environ.get("GATE_VERSION", "1.0.0")
VERSION_FILE = os.environ.get("GATE_VERSION_FILE") or None
ICON = os.environ.get("GATE_ICON") or None
IS_WIN = sys.platform == "win32"

EXCLUDES = ["tkinter", "unittest", "test", "idlelib", "lib2to3"]
# pywebview: bundle only the backend this system uses (the others would drag in Qt, GTK or CEF).
OTHER_BACKENDS = ["webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef",
                  "webview.platforms.android", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi"]
if IS_WIN:
    HIDDEN = ["webview", "webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]
    EXCLUDES += OTHER_BACKENDS + ["webview.platforms.cocoa"]
else:
    HIDDEN = ["webview", "webview.platforms.cocoa"]
    EXCLUDES += OTHER_BACKENDS + ["webview.platforms.winforms", "webview.platforms.edgechromium",
                                  "webview.platforms.mshtml", "webview.platforms.win32"]

a = Analysis(  # noqa: F821
    [os.path.join(DESKTOP, "gate_app.py")],
    pathex=[DESKTOP],
    datas=[(os.path.join(DESKTOP, "offline.html"), "."), (os.path.join(DESKTOP, "shell.json"), ".")],
    hiddenimports=HIDDEN,
    excludes=EXCLUDES,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gate",
    console=False,
    icon=ICON,
    version=VERSION_FILE if IS_WIN else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="gate", upx=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="gate.app",
        icon=ICON,
        bundle_identifier="io.github.juw12.gate.desktop",
        version=VERSION,
        info_plist={
            "CFBundleName": "gate",
            "CFBundleDisplayName": "gate",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSApplicationCategoryType": "public.app-category.lifestyle",
            "NSHumanReadableCopyright": "Open source - github.com/juw12/gate",
        },
    )
