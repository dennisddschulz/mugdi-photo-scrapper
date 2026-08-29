@echo off
rem Kill any Photo Organizer server holding the port, then start a fresh one.
rem
rem     restart.bat              restart on 8080
rem     restart.bat --port 8090  restart on another port
rem     restart.bat --stop       just stop, do not start again
rem
rem Only ever stops a process that is listening on the port -- it does not go
rem hunting for python processes, so nothing else you are running is touched.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8080"
set "STOPONLY="
set "PASSTHRU="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--port" (
    set "PORT=%~2"
    set "PASSTHRU=!PASSTHRU! --port %~2"
    shift
    shift
    goto parse
)
if /i "%~1"=="--stop" (
    set "STOPONLY=1"
    shift
    goto parse
)
set "PASSTHRU=!PASSTHRU! %~1"
shift
goto parse
:parsed

echo Stopping anything listening on port %PORT% ...
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:"LISTENING" ^| findstr /r /c:":%PORT% "') do (
    echo   stopping PID %%P
    taskkill /PID %%P /F >nul 2>&1
)

rem Give Windows a moment to release the socket before rebinding it.
ping -n 2 127.0.0.1 >nul 2>&1

netstat -ano -p tcp | findstr /r /c:"LISTENING" | findstr /r /c:":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   Port %PORT% is STILL in use by something this script cannot stop.
    echo   Find it with:  netstat -ano ^| findstr :%PORT%
    echo   Or use another port:  restart.bat --port 8090
    echo.
    pause
    exit /b 1
)
echo   port %PORT% is free.

if defined STOPONLY (
    echo Stopped. Not restarting ^(--stop^).
    exit /b 0
)

echo.
call "%~dp0start.bat" %PASSTHRU%
exit /b %ERRORLEVEL%
