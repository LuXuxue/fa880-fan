@echo off
rem Temperature-driven fan control, with a live display.
rem Press Ctrl+C or close this window to stop -- the fans go back to AUTO
rem automatically either way.
rem Pass a log file to also keep one on disk: fanguard.bat fanguard.log
cd /d "%~dp0"
python fanguard.py %*