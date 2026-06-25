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
python -m meccha_chameleon_tools
if errorlevel 1 pause
