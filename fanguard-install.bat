@echo off
rem Register fanguard as a Windows service and start it.
rem
rem The service comes up with the machine (automatic start) and runs as
rem LocalSystem, so it needs no user to be logged in.  Control it with
rem "net start fa880-fan" / "net stop fa880-fan".
rem
rem Administrator rights are needed; the script asks for them itself.
rem Extra options are passed on to the service, e.g.
rem     fanguard-install.bat --cpu-max-at 65 --mem-max-at 70
cd /d "%~dp0"

python fanguard.py --install-service %*
if errorlevel 1 (
    echo.
    echo ERROR: the service could not be installed.
    exit /b 1
)
echo.
echo The fans are now governed by the service.
echo   net stop fa880-fan     stop it (fans go back to AUTO)
echo   net start fa880-fan    start it again
echo   fanspeed.py --status   show what it is doing
exit /b 0
