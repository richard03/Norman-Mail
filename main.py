"""Mailer – desktopová aplikace v nativním okně (Edge WebView2).

Neběží žádný síťový server: stránka se načítá ze složky static přes virtuální doménu WebView2
(https://mailer-app.example/) a rozhraní volá Python přes most pywebview (Api.request). Jiný program
ani jiný uživatel počítače se tak k aplikaci nepřipojí. Jen při přihlášení přes Google krátce běží
server pro návrat z prohlížeče (app.OAuthListener)."""

import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse


def unblock_bundled_dlls():
    """Zip stažený z internetu (nebo přenesený z jiného počítače) nese značku Mark of the Web, kterou po
    rozbalení zdědí i přibalené knihovny. .NET Framework pak odmítne načíst Python.Runtime.dll (pythonnet)
    a aplikace spadne s „Failed to resolve Python.Runtime.Loader.Initialize“. Značku (alternativní datový
    proud Zone.Identifier) proto u knihoven odstraníme dřív, než se .NET načte. Kde do složky zapisovat
    nejde, pomůže Mailer.exe.config (loadFromRemoteSources)."""
    if not getattr(sys, "frozen", False):
        return
    for dll in Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).rglob("*.dll"):
        try:
            os.remove(f"{dll}:Zone.Identifier")
        except OSError:
            pass


unblock_bundled_dlls()

import webview  # noqa: E402  (až po odblokování knihoven)

import app as server_app
import mail
import taskbar
from store import DATA_DIR

SAFE_SCHEMES = {"http", "https", "mailto"}
APP_HOST = "mailer-app.example"  # virtuální doména (.example nikdy neexistuje na internetu)
APP_URL = f"https://{APP_HOST}/index.html"


def open_external(url) -> bool:
    """Otevře odkaz v systémovém prohlížeči – jen bezpečná schémata (ne file:, ms-settings: apod.)."""
    url = str(url)
    if urlparse(url).scheme.lower() not in SAFE_SCHEMES:
        return False
    return webbrowser.open(url)


class _SafeBrowser:
    open = staticmethod(open_external)


def harden_link_handling():
    """pywebview otevírá nová okna přes webbrowser.open(); podstrčíme verzi s kontrolou schématu."""
    try:
        from webview.platforms import edgechromium
        edgechromium.webbrowser = _SafeBrowser
    except Exception:
        pass


def disable_tracking_prevention():
    """WebView2 má ve výchozím stavu zapnutou ochranu před sledováním (úroveň „Vyvážená“), která blokuje
    obrázky ze serverů rozesílacích služeb – v newsletterech se pak po „Zobrazit obrázky“ ukáže jen část
    obrázků. Soukromí chrání aplikace sama: vzdálený obsah je zablokovaný (CSP rámce zprávy), dokud ho
    uživatel výslovně nepovolí."""
    try:
        from webview.platforms import edgechromium
        from Microsoft.Web.WebView2.Core import CoreWebView2TrackingPreventionLevel

        original = edgechromium.EdgeChrome.on_webview_ready

        def on_webview_ready(self, sender, args):
            original(self, sender, args)
            try:
                if args.IsSuccess:
                    sender.CoreWebView2.Profile.PreferredTrackingPreventionLevel = \
                        CoreWebView2TrackingPreventionLevel.None_ if hasattr(CoreWebView2TrackingPreventionLevel, "None_") \
                        else getattr(CoreWebView2TrackingPreventionLevel, "None")
            except Exception as e:
                print("Ochranu před sledováním nelze vypnout:", e)

        edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    except Exception as e:
        print("Ochranu před sledováním nelze vypnout:", e)


def map_static_folder(folder):
    """Virtuální doména APP_HOST → složka static. WebView2 soubory čte přímo z disku (žádný port).
    Musí se nastavit dřív, než pywebview načte první stránku."""
    from webview.platforms import edgechromium
    from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind

    original = edgechromium.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        if args.IsSuccess:
            sender.CoreWebView2.SetVirtualHostNameToFolderMapping(
                APP_HOST, folder, CoreWebView2HostResourceAccessKind.Deny)
        original(self, sender, args)

    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready


class Api:
    """Funkce volatelné z JavaScriptu přes window.pywebview.api.*"""

    def __init__(self):
        self._window = None  # podtržítko: pywebview neexportuje do JS

    def request(self, method, path, body=None):
        """Volání endpointu /api/* uvnitř procesu (Flask test_client – bez sítě). Vrací {status, data}."""
        if not isinstance(path, str) or not path.startswith("/api/") or method not in ("GET", "POST", "PUT", "DELETE"):
            return {"status": 400, "data": {"error": "Neplatný požadavek."}}
        with server_app.app.test_client() as client:
            r = client.open(path, method=method, json=body, headers={"X-Mailer": "1"})
        data = r.get_json(silent=True)
        return {"status": r.status_code, "data": data}

    def save_attachment(self, acc_id, folder, uid, index):
        try:
            acc = server_app.store.get(acc_id)
            if acc is None:
                return {"error": "Účet neexistuje."}
            with server_app.pool.get(acc) as M:
                (filename, _ctype, data), = mail.get_attachments(M, folder, int(uid), [int(index)])
            path = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename)
            if not path:
                return {"ok": False}
            path = path if isinstance(path, str) else path[0]
            Path(path).write_bytes(data)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"error": mail.friendly_error(e)}

    def open_external(self, url):
        return open_external(url)


def resource(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / name)


def pin_identity(window, icon):
    """Po zobrazení okna: identita pro hlavní panel (připnutá ikona pak spouští Mailer, ne Python)."""
    try:
        hwnd = 0
        try:
            hwnd = int(window.native.Handle.ToInt64())
        except Exception:
            hwnd = taskbar.find_window("Mailer")
        if not taskbar.apply_to_window(hwnd, icon):
            print("Identitu okna pro hlavní panel nelze nastavit.")
    except Exception as e:
        print("Identitu okna pro hlavní panel nelze nastavit:", e)


WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"


def webview2_installed() -> bool:
    """Je nainstalovaný Edge WebView2 Runtime? (systémově, nebo jen pro uživatele)"""
    import winreg
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
                      (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT),
                      (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT)):
        try:
            with winreg.OpenKey(root, key) as k:
                version = winreg.QueryValueEx(k, "pv")[0]
            if version and version != "0.0.0.0":
                return True
        except OSError:
            continue
    return False


def require_webview2() -> bool:
    """Bez WebView2 se okno nezobrazí – nabídne stažení (Windows 11 ho má vždy, Windows 10 skoro vždy)."""
    if webview2_installed():
        return True
    import ctypes
    answer = ctypes.windll.user32.MessageBoxW(
        None, "Mailer potřebuje Microsoft Edge WebView2 Runtime, který na tomto počítači chybí.\n\n"
              "Chcete otevřít stránku Microsoftu, odkud ho stáhnete (zvolte „Evergreen Bootstrapper“)? "
              "Po instalaci Mailer spusťte znovu.", "Mailer", 0x04 | 0x30)  # Ano/Ne, ikona upozornění
    if answer == 6:  # Ano
        webbrowser.open(WEBVIEW2_DOWNLOAD)
    return False


def main():
    if not require_webview2():
        return
    taskbar.set_process_app_id()
    server_app.EMBEDDED = True
    server_app.start_background()
    harden_link_handling()
    disable_tracking_prevention()
    map_static_folder(resource("static"))
    webview.settings["ALLOW_DOWNLOADS"] = True
    api = Api()
    window = webview.create_window("Mailer", APP_URL, js_api=api, width=1600, height=950,
                                   min_size=(960, 600), text_select=True)
    api._window = window
    icon = resource("assets/mailer.ico")
    window.events.shown += lambda: pin_identity(window, icon)
    webview.start(icon=icon if os.path.exists(icon) else None, private_mode=False,
                  storage_path=str(DATA_DIR / "webview"))


if __name__ == "__main__":
    main()
