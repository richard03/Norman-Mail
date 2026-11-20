# -*- mode: python ; coding: utf-8 -*-
# Sestavení jediného souboru dist\NormanMail.exe (spouští build.bat: pyinstaller --noconfirm --clean NormanMail.spec).
import json
import os
import sys

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                  VarFileInfo, VarStruct, VSVersionInfo)

sys.path.insert(0, SPECPATH)
from google_client import BUILTIN_NAME, build_client_file, parse_client  # noqa: E402
from version import VERSION  # noqa: E402

APP_NAME = "Norman Mail"

# Moduly standardní knihovny, které aplikace ani její závislosti za běhu nepotřebují (menší exe).
# Ne: decimal (Flask), asyncio, xml (pywebview), unicodedata (IDNA).
EXCLUDES = [
    "tkinter", "_tkinter", "unittest", "pydoc", "pydoc_data", "xmlrpc", "lib2to3", "setuptools", "pkg_resources",
    "bz2", "_bz2", "lzma", "_lzma", "multiprocessing", "_multiprocessing",
]


def unneeded(dest):
    """Soubory, které aplikace nepotřebuje: dokumentace pythonnet. (WebView2Loader pro x86/arm64 zůstává –
    pywebview při importu ověřuje, že složky všech architektur existují.)"""
    return dest.replace("\\", "/").lower().endswith("python.runtime.xml")


def google_client_datas():
    """OAuth klient Norman Mail (přihlášení přes Google) z počítače vývojáře – do exe jako google_client.json.
    Bez něj se sestaví exe, ve kterém si uživatel musí zadat vlastního klienta (release.py takové vydání odmítne)."""
    src = build_client_file()
    try:
        cfg = parse_client(src.read_text("utf-8"))
    except OSError:
        cfg = None
    if not cfg:
        print(f"UPOZORNĚNÍ: {src} chybí nebo neobsahuje platného OAuth klienta – exe bude bez přihlášení přes Google.")
        return []
    out = os.path.join(workpath, BUILTIN_NAME)
    os.makedirs(workpath, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return [(out, ".")]


a = Analysis(
    ["main.py"],
    datas=[("static", "static"), ("assets/mailer.ico", "assets")] + google_client_datas(),
    excludes=EXCLUDES,
    noarchive=False,
    optimize=2,  # bez docstringů a assertů (kontroly za běhu proto nesmí stát na assert)
)
a.binaries = [t for t in a.binaries if not unneeded(t[0])]
a.datas = [t for t in a.datas if not unneeded(t[0])]
pyz = PYZ(a.pure)

nums = tuple(int(x) for x in VERSION.split(".")) + (0,) * (4 - len(VERSION.split(".")))
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=nums, prodvers=nums),
    kids=[
        StringFileInfo([StringTable("040504B0", [
            StringStruct("ProductName", APP_NAME),
            StringStruct("FileDescription", APP_NAME),
            StringStruct("ProductVersion", VERSION),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "NormanMail"),
            StringStruct("OriginalFilename", "NormanMail.exe"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0405, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NormanMail",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX zvyšuje falešná hlášení antivirů
    runtime_tmpdir=None,
    console=os.environ.get("NORMANMAIL_CONSOLE") == "1",  # ladění: set NORMANMAIL_CONSOLE=1 před sestavením
    disable_windowed_traceback=False,
    icon=["assets/mailer.ico"],
    version=version_info,
)
