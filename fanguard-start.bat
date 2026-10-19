@echo off
rem Start the fan governor in the background with no console window.
rem Pair with fanguard-stop.bat to shut it down cleanly.
rem It keeps running until you stop it or reboot.
rem No log file is kept on disk; the live numbers are published through
rem shared memory for fanspeed to read.
cd /d "%~dp0"

rem The liveness check goes through the shared-memory bus, which is a kernel
rem object -- it vanishes with the last handle, so unlike a lock file on
rem disk it can never survive a crash or a power cut.
set STATUS=%TEMP%\fanguard.status

python fanspeed.py --status > "%STATUS%" 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('type "%STATUS%"') do echo fanguard is already running: %%i
    del /q "%STATUS%"
    exit /b 0
)
del /q "%STATUS%"

start "" /min pythonw fanguard.py

rem Wait for it to come up (UAC prompt plus sensor driver load).
set /a TRIES=0
:wait
set /a TRIES+=1
if %TRIES% GEQ 30 goto :failed
python fanspeed.py --status > "%STATUS%" 2>nul
if not errorlevel 1 goto :started
ping -n 2 127.0.0.1 >nul
goto :wait

:started
echo fanguard started in the background.
for /f "delims=" %%i in ('type "%STATUS%"') do echo   %%i
echo fanspeed / status.bat will now read it without asking for elevation.
echo To stop it cleanly:  fanguard-stop.bat
goto :eof

:failed
del /q "%STATUS%" 2>nul
echo ERROR: fanguard did not start within 30 seconds.
exit /b 1