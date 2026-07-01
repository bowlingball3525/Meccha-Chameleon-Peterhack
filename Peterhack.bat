@echo off
setlocal

:: Re-launch this batch file as administrator if we are not already elevated.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
    exit /b
)

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
