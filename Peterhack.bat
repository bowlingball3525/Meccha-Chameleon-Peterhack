@echo off
setlocal EnableExtensions

title Peterhack

:: Self-elevate: re-launch as Administrator if we are not already elevated.
net session >nul 2>&1
if %errorlevel%==0 goto run

echo [Peterhack] Requesting Administrator privileges...
echo Approve the UAC prompt to continue.
echo.

set "ELEVATE_VBS=%temp%\peterhack_elevate_%random%.vbs"
(
    echo Set shell = CreateObject^("Shell.Application"^)
    echo shell.ShellExecute "%~f0", "", "%~dp0", "runas", 1
) > "%ELEVATE_VBS%"
cscript //nologo "%ELEVATE_VBS%" >nul 2>&1
del "%ELEVATE_VBS%" >nul 2>&1

:: Non-elevated launcher exits; the elevated copy continues below at :run
exit /b 0

:run
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [Peterhack] Python was not found in PATH. Install Python 3 and try again.
    pause
    exit /b 1
)

:: Quick import check for packages listed in requirements.txt
python -c "import pymem; from PyQt5.QtWidgets import QApplication; import win32api" >nul 2>&1
if errorlevel 1 (
    echo [Peterhack] Missing dependencies — installing from requirements.txt...
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [Peterhack] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo [Peterhack] Dependencies installed.
)

python -m meccha_chameleon_tools
if errorlevel 1 pause
