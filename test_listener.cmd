@echo off
cd /d "%~dp0"
echo Starting Feishu listener test for one event...
echo Send a help command mentioning Winston in the configured Feishu group.
echo This window exits after one event or five minutes.
".venv\Scripts\python.exe" -X utf8 bot\scripts\feishu_bot.py listen --timeout 5m --max-events 1
echo.
echo Listener test finished.
pause
