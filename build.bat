@echo off
title Build Job Analyser EXE
color 0B

echo.
echo  ==========================================
echo    Building JobAnalyser.exe
echo  ==========================================
echo.

:: Need Python on this build machine (end users will NOT need it).
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install Python from python.org ^(tick "Add to PATH"^), then re-run.
    pause & exit /b 1
)

echo  Installing build + runtime dependencies (one-time)...
python -m pip install --upgrade pip -q --disable-pip-version-check
python -m pip install pyinstaller flask flask-cors requests beautifulsoup4 -q --disable-pip-version-check
echo.

echo  Compiling into a single .exe ...
echo.
python -m PyInstaller --onefile --console --name JobAnalyser ^
  --add-data "job-analyser.html;." ^
  --hidden-import flask_cors ^
  server.py

if errorlevel 1 (
    echo.
    echo  BUILD FAILED. Scroll up for the error.
    pause & exit /b 1
)

:: Save a timestamped copy so every build is preserved automatically (no manual
:: version counting). JobAnalyser.exe stays the predictable "latest"; the stamped
:: copy is the dated archive. yyyy-MM-dd_HHmm sorts chronologically.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
copy /Y dist\JobAnalyser.exe "dist\JobAnalyser_%STAMP%.exe" >nul

echo.
echo  ==========================================
echo    Done!  Your app is here:
echo      dist\JobAnalyser.exe              (latest - hand this out)
echo      dist\JobAnalyser_%STAMP%.exe   (dated archive of this build)
echo  ==========================================
echo.
echo  Recipients just double-click the .exe - nothing to install.
echo  Tip: to ship a UI tweak later WITHOUT rebuilding, drop an updated
echo       job-analyser.html next to the .exe - the app prefers that copy.
echo.
pause
