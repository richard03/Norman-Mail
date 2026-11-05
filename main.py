"""Mailer – desktopová aplikace. Spustí lokální server a zobrazí ho v nativním okně (Edge WebView2)."""

import os
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import webview
from werkzeug.serving import make_server

import app as server_app
import mail
from store import DATA_DIR

SAFE_SCHEMES = {"http", "https", "mailto"}


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


class Api:
    """Funkce volatelné z JavaScriptu přes window.pywebview.api.*"""

    def __init__(self):
        self._window = None  # podtržítko: pywebview neexportuje do JS

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


def start_server():
    port = int(os.environ.get("MAILER_PORT") or 0)  # 0 = libovolný volný port
    srv = make_server(server_app.HOST, port, server_app.app, threaded=True)
    server_app.PORT = srv.server_port  # potřebné pro OAuth redirect URI
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://{server_app.HOST}:{srv.server_port}/"


def resource(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / name)


def main():
    server_app.start_background()
    url = start_server()
    harden_link_handling()
    webview.settings["ALLOW_DOWNLOADS"] = True
    api = Api()
    window = webview.create_window("Mailer", url, js_api=api, width=1600, height=950,
                                   min_size=(1200, 600), text_select=True)
    api._window = window
    icon = resource("assets/mailer.ico")
    webview.start(icon=icon if os.path.exists(icon) else None, private_mode=False,
                  storage_path=str(DATA_DIR / "webview"))


if __name__ == "__main__":
    main()
