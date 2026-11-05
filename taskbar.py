"""Identita aplikace pro hlavní panel Windows.

Bez ní okno patří procesu pythonw.exe (při spuštění ze zdrojových kódů) a „Připnout na hlavní panel“
připne Python (IDLE), ne Mailer. Okno proto dostane vlastní AppUserModelID a příkaz, kterým se má
připnutá ikona spouštět (RelaunchCommand), spolu s názvem a ikonou.
"""

import ctypes
import os
import sys
from ctypes import wintypes

APP_ID = "Mailer.Desktop"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        import uuid
        u = uuid.UUID(text)
        super().__init__(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("pwszVal", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]


VT_LPWSTR = 31
_FMTID = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"   # PKEY_AppUserModel_*
PKEY_RELAUNCH_COMMAND, PKEY_RELAUNCH_ICON, PKEY_RELAUNCH_NAME, PKEY_ID = 2, 3, 4, 5
IID_IPropertyStore = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"


def set_process_app_id():
    """Volat co nejdřív – okna procesu se pak na hlavním panelu seskupí jako Mailer, ne jako Python."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except Exception:
        pass


def relaunch_command():
    """Příkaz pro připnutou ikonu: sestavená aplikace = Mailer.exe, jinak pythonw.exe main.py."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{exe}" "{main}"'


def apply_to_window(hwnd, icon_path=None):
    """Nastaví oknu AppUserModelID a údaje pro připnutí (příkaz, název, ikona)."""
    if sys.platform != "win32" or not hwnd:
        return False
    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
    ole32.CoInitialize(None)
    store = ctypes.c_void_p()
    hr = shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(GUID(IID_IPropertyStore)),
                                             ctypes.byref(store))
    if hr != 0 or not store:
        return False
    vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    set_value = proto(vtbl[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    values = [(PKEY_RELAUNCH_COMMAND, relaunch_command()), (PKEY_RELAUNCH_NAME, "Mailer")]
    if icon_path and os.path.exists(icon_path):
        values.append((PKEY_RELAUNCH_ICON, os.path.abspath(icon_path) + ",0"))
    values.append((PKEY_ID, APP_ID))  # bez ID by Windows údaje pro připnutí ignorovaly
    ok = True
    try:
        for pid, text in values:
            key = PROPERTYKEY(GUID(_FMTID), pid)
            var = PROPVARIANT(vt=VT_LPWSTR, pwszVal=text)
            ok &= set_value(store, ctypes.byref(key), ctypes.byref(var)) == 0
        ok &= commit(store) == 0
    finally:
        release(store)
    return ok


def find_window(title, pid=None):
    """Viditelné okno s daným názvem patřící procesu pid (výchozí: tento proces)."""
    if sys.platform != "win32":
        return 0
    pid = pid or os.getpid()
    user32, found = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
                return False
        return True
    user32.EnumWindows(visit, 0)
    return found[0] if found else 0
