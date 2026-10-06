@echo off
title Job Analyser Server
color 0A

echo.
echo  ==========================================
echo    Job Analyser -- LinkedIn Server
echo  ==========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Please install Python from python.org
    pause & exit
)

:: Skip install if dependencies are already present (fast restart)
python -c "import flask, flask_cors, bs4, requests" >nul 2>&1
if not errorlevel 1 goto :start

echo  First run — installing dependencies...
echo.

set PIP=python -m pip
%PIP% install --upgrade pip -q --disable-pip-version-check
%PIP% install flask flask-cors requests beautifulsoup4 -q --disable-pip-version-check

echo.
echo  Dependencies installed.
echo.

:start
:: Register protocol handler only if not already present
REG QUERY "HKCU\Software\Classes\jobanalyser" >nul 2>&1
if errorlevel 1 (
    REG ADD "HKCU\Software\Classes\jobanalyser" /ve /d "URL:Job Analyser" /f >nul 2>&1
    REG ADD "HKCU\Software\Classes\jobanalyser" /v "URL Protocol" /d "" /f >nul 2>&1
    REG ADD "HKCU\Software\Classes\jobanalyser\shell\open\command" /ve /d "cmd /c start \"Job Analyser Server\" \"%~f0\"" /f >nul 2>&1
)

:: Register startup entry only if not already present
REG QUERY "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "JobAnalyser" >nul 2>&1
if errorlevel 1 (
    REG ADD "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "JobAnalyser" /d "\"%~f0\"" /f >nul 2>&1
)

:: Open the app in the default browser
start http://localhost:5000/

echo  Server running at http://localhost:5000
echo  Opens automatically on Windows login.
echo  Press Ctrl+C to stop.
echo.
python "%~dp0server.py"
pause
