@echo off
rem Hand both fans back to the firmware AUTO curve.
rem
rem Stop the service first (net stop fa880-fan), otherwise the governor
rem overwrites this within a few seconds.
cd /d "%~dp0"

python fanctl.py --cpu auto --mem auto
exit /b %errorlevel%
