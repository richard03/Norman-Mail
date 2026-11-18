"""Identita aplikace pro hlavní panel Windows.

Bez ní okno patří procesu pythonw.exe (při spuštění ze zdrojových kódů) a „Připnout na hlavní panel“
připne Python (IDLE), ne Norman Mail. Okno proto dostane vlastní AppUserModelID a příkaz, kterým se má
připnutá ikona spouštět (RelaunchCommand), spolu s názvem a ikonou.
"""

import ctypes
import os
import sys
from ctypes import wintypes

APP_ID = "Mailer.Desktop"  # neměnit: připnuté ikony a zástupci se drží tohoto ID


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
    """Volat co nejdřív – okna procesu se pak na hlavním panelu seskupí jako Norman Mail, ne jako Python."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except Exception:
        pass


def relaunch_command():
    """Příkaz pro připnutou ikonu: sestavená aplikace = NormanMail.exe, jinak pythonw.exe main.py."""
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
    values = [(PKEY_RELAUNCH_COMMAND, relaunch_command()), (PKEY_RELAUNCH_NAME, "Norman Mail")]
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


CLSID_ShellLink = "00021401-0000-0000-C000-000000000046"
IID_IShellLinkW = "000214F9-0000-0000-C000-000000000046"
IID_IPersistFile = "0000010B-0000-0000-C000-000000000046"
CLSCTX_INPROC_SERVER = 1
CSIDL = {"programs": 0x02, "desktop": 0x10}  # nabídka Start (Programy) a plocha aktuálního uživatele


def launch_target():
    """(program, argumenty, ikona) pro spuštění aplikace – sestavená verze, nebo zdrojové kódy."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", sys.executable + ",0"
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    here = os.path.dirname(os.path.abspath(__file__))
    return (pyw if os.path.exists(pyw) else exe), f'"{os.path.join(here, "main.py")}"', \
        os.path.join(here, "assets", "mailer.ico") + ",0"


def shortcut_path(where, name):
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL[where], None, 0, buf) != 0:
        raise OSError("Složku pro zástupce nelze zjistit.")
    return os.path.join(buf.value, name + ".lnk")


def _method(ptr, index, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def create_shortcut(where, name):
    """Vytvoří (přepíše) zástupce aplikace v nabídce Start nebo na ploše. Zástupce nese stejné
    AppUserModelID jako okno, takže se na hlavním panelu seskupí s běžící aplikací. Vrací cestu."""
    if sys.platform != "win32":
        raise OSError("Zástupce jde vytvořit jen ve Windows.")
    path = shortcut_path(where, name)
    target, args, icon = launch_target()
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    link, store, persist = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(GUID(CLSID_ShellLink)), None, CLSCTX_INPROC_SERVER,
                                ctypes.byref(GUID(IID_IShellLinkW)), ctypes.byref(link))
    if hr != 0 or not link:
        raise OSError(f"Zástupce nelze vytvořit (0x{hr & 0xFFFFFFFF:08X}).")
    wstr, hres = ctypes.c_wchar_p, ctypes.c_long
    try:
        icon_file, _, icon_index = icon.rpartition(",")
        _method(link, 20, hres, wstr)(link, target)                               # SetPath
        _method(link, 11, hres, wstr)(link, args)                                 # SetArguments
        _method(link, 9, hres, wstr)(link, os.path.dirname(target))               # SetWorkingDirectory
        _method(link, 17, hres, wstr, ctypes.c_int)(link, icon_file, int(icon_index))  # SetIconLocation
        _method(link, 7, hres, wstr)(link, name)                                  # SetDescription
        query = _method(link, 0, hres, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        if query(link, ctypes.byref(GUID(IID_IPropertyStore)), ctypes.byref(store)) == 0 and store:
            key = PROPERTYKEY(GUID(_FMTID), PKEY_ID)
            var = PROPVARIANT(vt=VT_LPWSTR, pwszVal=APP_ID)
            _method(store, 6, hres, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
                store, ctypes.byref(key), ctypes.byref(var))
            _method(store, 7, hres)(store)                                        # Commit
        if query(link, ctypes.byref(GUID(IID_IPersistFile)), ctypes.byref(persist)) != 0 or not persist:
            raise OSError("Zástupce nelze uložit.")
        hr = _method(persist, 6, hres, wstr, wintypes.BOOL)(persist, path, True)  # Save
        if hr != 0:
            raise OSError(f"Zástupce nelze uložit (0x{hr & 0xFFFFFFFF:08X}).")
    finally:
        for p in (persist, store, link):
            if p:
                _method(p, 2, ctypes.c_ulong)(p)                                  # Release
    return path


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
