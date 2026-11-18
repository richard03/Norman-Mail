@echo off
rem Sestavi Norman Mail pro Windows bez Pythonu jako jediny soubor:
rem   dist\NormanMail.exe   - cela aplikace vcetne Pythonu (nastaveni sestaveni je v NormanMail.spec)
rem Portable rezim: vedle exe staci polozit soubor portable.txt (assets\portable.txt).
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv || goto :error
.venv\Scripts\python -m pip install -q --require-hashes -r requirements.txt -r requirements-build.txt || goto :error

.venv\Scripts\pyinstaller --noconfirm --clean NormanMail.spec || goto :error

echo.
echo Hotovo: dist\NormanMail.exe
goto :eof
:error
echo Sestaveni selhalo.
pause
exit /b 1
