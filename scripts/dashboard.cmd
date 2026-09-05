@echo off
REM The dashboard on http://127.0.0.1:7788 (loopback only).
REM   dashboard.cmd         -> 7788
REM   dashboard.cmd 7799    -> that port instead
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=7788"
echo Dashboard: http://127.0.0.1:%PORT%
python -m bgate_cli.main serve --port %PORT%
