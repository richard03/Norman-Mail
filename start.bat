@echo off
rem Spusti Mailer ze zdrojovych kodu (bez sestaveni .exe).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Vytvarim virtualni prostredi a instaluji zavislosti...
  python -m venv .venv || goto :error
  .venv\Scripts\python -m pip install -q --require-hashes -r requirements.txt || goto :error
)
start "" .venv\Scripts\pythonw.exe main.py
goto :eof
:error
echo Instalace selhala. Je nainstalovany Python 3.10+?
pause
