@echo off
rem Stop the background fan governor started by fanguard-start.bat.
rem It hands the fans back to AUTO on its way out -- unlike taskkill, which
rem would leave them at the last duty.
rem The governor picks the file up within one interval and removes it again.
rem
rem The request goes to the per-user state directory, not next to the
rem scripts, so this works from a read-only install too.
cd /d "%~dp0"
set STOPFILE=%LOCALAPPDATA%\fa880-fan\fanguard.stop
if not exist "%LOCALAPPDATA%\fa880-fan" mkdir "%LOCALAPPDATA%\fa880-fan"
rem Remove any leftover request first, so that "it exists" afterwards means
rem "we just wrote it" rather than "one was already there".
del /q "%STOPFILE%" 2>nul
echo stop> "%STOPFILE%"
if not exist "%STOPFILE%" (
    echo ERROR: could not write "%STOPFILE%" -- the governor was not asked
    echo to stop.  Check that %%LOCALAPPDATA%% is writable.
    exit /b 1
)
echo Stop requested. The governor will exit within a few seconds.