@echo off
title Job Analyser — One-time Setup
color 0B

echo.
echo  ==========================================
echo    Job Analyser — One-time Setup
echo  ==========================================
echo.
echo  This registers a protocol so the web app
echo  can start the server with one click.
echo.

set "BAT=%~dp0start.bat"

REG ADD "HKCU\Software\Classes\jobanalyser"                        /ve /d "URL:Job Analyser" /f >nul
REG ADD "HKCU\Software\Classes\jobanalyser"                        /v  "URL Protocol" /d "" /f >nul
REG ADD "HKCU\Software\Classes\jobanalyser\shell\open\command"     /ve /d "cmd /c start \"Job Analyser Server\" \"%BAT%\"" /f >nul

echo  Done! You only need to run this once.
echo.
echo  From now on, clicking "Save and Reconnect"
echo  in the app will start the server automatically.
echo.
pause
