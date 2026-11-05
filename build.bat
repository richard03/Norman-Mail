@echo off
rem Sestavi desktopovou aplikaci do slozky dist\Mailer (spousti se Mailer.exe).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv || goto :error
.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller || goto :error
.venv\Scripts\pyinstaller --noconfirm --clean --windowed --name Mailer ^
  --icon assets\mailer.ico ^
  --add-data "static;static" ^
  --add-data "assets\mailer.ico;assets" ^
  main.py || goto :error
echo.
echo Hotovo: dist\Mailer\Mailer.exe
goto :eof
:error
echo Sestaveni selhalo.
pause
