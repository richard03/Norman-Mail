r"""Vývojový server: rozhraní Maileru v běžném prohlížeči (ladění, kontrola UI).

Není součástí Mailer.exe – desktopová verze žádný server nespouští. Tady naopak běží skutečný HTTP server
na 127.0.0.1, ke kterému se může připojit kterýkoli program na počítači; pouštějte ho jen při vývoji.

    .venv\Scripts\python tools\dev_server.py            (port 8765, nebo MAILER_PORT)
"""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

if __name__ == "__main__":
    url = f"http://{app.HOST}:{app.PORT}/"
    print(f"Vývojový server Maileru: {url}  (ukončení: Ctrl+C)")
    if not os.environ.get("MAILER_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.start_background()
    app.app.run(host=app.HOST, port=app.PORT, threaded=True)
