@echo off
rem Run both fans at full speed.
rem
rem Stop the service first (net stop fa880-fan), otherwise the governor
rem overwrites this within a few seconds.
cd /d "%~dp0"

python fanctl.py --cpu max --mem max
exit /b %errorlevel%
