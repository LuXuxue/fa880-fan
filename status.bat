@echo off
rem Live fan speed display: the current RPMs, the gear that is in effect
rem and the state of the governor service.  Refreshes once a second.
rem
rem Reads the shared-memory bus, so it needs no elevation and no UAC
rem prompt as long as the service is running.
rem
rem Press Ctrl+C or close the window to exit.
cd /d "%~dp0"

python fanspeed.py
exit /b %errorlevel%
