# -*- mode: python ; coding: utf-8 -*-
# Sestavení jediného souboru dist\NormanMail.exe (spouští build.bat: pyinstaller --noconfirm --clean NormanMail.spec).
import os
import sys

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                  VarFileInfo, VarStruct, VSVersionInfo)

sys.path.insert(0, SPECPATH)
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


a = Analysis(
    ["main.py"],
    datas=[("static", "static"), ("assets/mailer.ico", "assets")],
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
