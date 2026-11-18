@echo off
rem Sestavi Norman Mail pro Windows bez Pythonu:
rem   dist\NormanMail\NormanMail.exe          - aplikace (slozka obsahuje vse potrebne vcetne Pythonu)
rem   dist\NormanMail-VERZE-portable.zip      - portable verze (data ve slozce data vedle NormanMail.exe)
rem   dist\NormanMail-Setup-VERZE.exe         - instalator (jen pokud je nainstalovany Inno Setup 6 nebo 7)
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv || goto :error
.venv\Scripts\python -m pip install -q --require-hashes -r requirements.txt -r requirements-build.txt || goto :error
for /f %%v in ('.venv\Scripts\python -c "import version; print(version.VERSION)"') do set VER=%%v

.venv\Scripts\pyinstaller --noconfirm --clean --windowed --name NormanMail ^
  --icon assets\mailer.ico ^
  --add-data "static;static" ^
  --add-data "assets\mailer.ico;assets" ^
  main.py || goto :error

rem --- portable: stejna aplikace + portable.txt (prepne ukladani dat vedle exe)
if exist dist\portable rmdir /s /q dist\portable
xcopy /e /i /q dist\NormanMail dist\portable\NormanMail >nul || goto :error
copy /y assets\portable.txt dist\portable\NormanMail\portable.txt >nul || goto :error
if exist dist\NormanMail-%VER%-portable.zip del dist\NormanMail-%VER%-portable.zip
tar -a -c -f dist\NormanMail-%VER%-portable.zip -C dist\portable NormanMail || goto :error
rmdir /s /q dist\portable

rem --- instalator (Inno Setup 6 nebo 7: systemova nebo uzivatelska instalace)
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
for %%d in ("%ProgramFiles(x86)%" "%ProgramFiles%" "%LOCALAPPDATA%\Programs") do if exist "%%~d\Inno Setup 7\ISCC.exe" set ISCC="%%~d\Inno Setup 7\ISCC.exe"
if defined ISCC (
  %ISCC% /Q /DAppVersion=%VER% installer.iss || goto :error
) else (
  echo Inno Setup neni nainstalovany - instalator se nesestavi ^(winget install JRSoftware.InnoSetup^).
)

echo.
echo Hotovo:
echo   dist\NormanMail\NormanMail.exe
echo   dist\NormanMail-%VER%-portable.zip
if defined ISCC echo   dist\NormanMail-Setup-%VER%.exe
goto :eof
:error
echo Sestaveni selhalo.
pause
exit /b 1
