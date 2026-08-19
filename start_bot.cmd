@echo off
cd /d "%~dp0"
start "FeishuBot" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0bot\scripts\run_listener.ps1"
exit /b 0