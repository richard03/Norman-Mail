"""Běh jediné instance aplikace a úklid po sestavené verzi (jediný NormanMail.exe).

Dvě instance nad stejnými daty by si přepisovaly databázi a nastavení, a po automatické aktualizaci se
nová verze spouští, zatímco stará končí. Zámek je proto pojmenovaný mutex odvozený od složky s daty:
nainstalovaná a portable verze (jiná data) můžou běžet současně, stejná data jen jednou.
"""

import ctypes
import hashlib
import os
import shutil
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
_mutex = None  # drží se po celou dobu běhu procesu


def acquire(data_dir) -> bool:
    """True = jsme jediná instance pro tato data. False = už běží jiná."""
    global _mutex
    if sys.platform != "win32":
        return True
    key = hashlib.sha256(str(Path(data_dir).resolve()).lower().encode()).hexdigest()[:16]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, f"Local\\NormanMail-{key}")
    if not handle:
        return True  # zámek nejde vytvořit – raději spustit, než aplikaci zablokovat
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return False
    _mutex = handle
    return True


def wait_and_acquire(data_dir, timeout=20.0) -> bool:
    """Jako acquire(), ale chvíli počká, až předchozí instance skončí (restart po aktualizaci)."""
    end = time.monotonic() + timeout
    while True:
        if acquire(data_dir):
            return True
        if time.monotonic() > end:
            return False
        time.sleep(0.25)


def activate_existing(title) -> bool:
    """Přenese okno již běžící instance (jiný proces, okno s daným názvem) do popředí."""
    if sys.platform != "win32":
        return False
    user32, me, found = ctypes.windll.user32, os.getpid(), []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != me and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
                return False
        return True
    user32.EnumWindows(visit, 0)
    if not found:
        return False
    if user32.IsIconic(found[0]):
        user32.ShowWindow(found[0], SW_RESTORE)
    user32.SetForegroundWindow(found[0])
    return True


def _is_our_bundle(d: Path) -> bool:
    return (d / "static" / "index.html").is_file() and (d / "assets" / "mailer.ico").is_file()


def cleanup_stale_bundles(min_age=600):
    """Smaže rozbalené složky _MEI… po dřívějších bězích, které nedoběhly (pád, ukončení ve Správci úloh).

    Mažou se jen naše složky (podle obsahu), starší než min_age sekund. Složku napřed přejmenujeme:
    Windows nedovolí přejmenovat složku s otevřenými soubory, takže složku jiné běžící instance nepoškodíme."""
    current = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not current:
        return
    current = Path(current).resolve()
    now = time.time()
    try:
        candidates = list(current.parent.glob("_MEI*"))
    except OSError:
        return
    for d in candidates:
        try:
            if d.resolve() == current or not d.is_dir() or now - d.stat().st_mtime < min_age:
                continue
            if not (_is_our_bundle(d) or d.name.endswith(".del")):
                continue
            target = d if d.name.endswith(".del") else d.with_name(d.name + ".del")
            if target != d:
                os.rename(d, target)  # selže, pokud složku používá běžící proces
            shutil.rmtree(target, ignore_errors=True)
        except OSError:
            continue


def cleanup_in_background():
    threading.Thread(target=cleanup_stale_bundles, name="cleanup-bundles", daemon=True).start()
