"""Rozvržení okna (window.json): poloha a velikost okna, šířky sloupců.

Patří k počítači, ne k poště, proto není v databázi. Zapisují ho dvě místa – main.py při zavírání okna
(poloha a velikost) a app.py za běhu (šířky sloupců) –, takže každý zápis jen doplní své klíče a zbytek
souboru zachová. Zapisuje se přes dočasný soubor, aby přerušený zápis nenechal rozbitý JSON."""

import json
import os
import threading

from store import DATA_DIR

WINDOW_FILE = DATA_DIR / "window.json"
_lock = threading.Lock()


def load() -> dict:
    try:
        data = json.loads(WINDOW_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(**changes) -> dict:
    """Doplní / přepíše dané klíče a soubor uloží; ostatní klíče zůstanou."""
    with _lock:
        data = {**load(), **changes}
        WINDOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WINDOW_FILE.with_name(WINDOW_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, WINDOW_FILE)
        return data
