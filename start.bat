@echo off
rem Start the Photo Organizer web interface.
rem
rem Double-click this file, or run it from a console. Any arguments are
rem passed straight through, so this works:
rem
rem     start.bat --port 8090 --no-browser
rem
rem Nothing is written to your photos by starting this. The page previews
rem what would happen and copies only when you explicitly confirm.

setlocal enabledelayedexpansion
cd /d "%~dp0"

rem Find a real Python. A bare "python" on this machine is the Microsoft
rem Store stub, which looks like Python, is on the PATH, and does nothing --
rem so it is checked LAST and only when it is not the stub.
set "PY="

rem 1. The usual per-user install location. %LOCALAPPDATA% keeps this
rem    working for any account, rather than hard-coding one user's folder.
for %%V in (313 312 311) do (
    if not defined PY (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
            set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        )
    )
)

rem 2. The official launcher, if Python was installed system-wide.
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)

rem 3. Whatever is on the PATH, unless it is the Store stub.
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY (
            echo %%P | find /i "WindowsApps" >nul || set "PY=%%P"
        )
    )
)

if not defined PY (
    echo.
    echo   Could not find Python 3.11 or newer.
    echo.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)

echo Using %PY%
echo.
%PY% -m photo_organizer --serve %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    if "%CODE%"=="2" (
        echo   The server could not start. If it says the port is in use,
        echo   something is already on 8080 -- try:  start.bat --port 8090
    ) else (
        echo   Exited with code %CODE%.
    )
    echo.
    pause
)

endlocal
exit /b %CODE%
