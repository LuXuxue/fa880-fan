@echo off
rem Stop and remove the fanguard Windows service.
rem
rem The service is stopped first, so it hands the fans back to the
rem firmware AUTO curve before it goes away.
rem
rem Administrator rights are needed; the script asks for them itself.
cd /d "%~dp0"

python fanguard.py --uninstall-service
if errorlevel 1 (
    echo.
    echo ERROR: the service could not be removed.
    exit /b 1
)
echo.
echo The service is gone.  Use fanguard-install.bat to put it back.
exit /b 0
