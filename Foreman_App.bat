@echo off
rem Foreman desktop-app launcher: starts the server (if not already running)
rem and opens the console as a standalone app window (no browser chrome).
cd /d %~dp0

rem Already running? Then just open the window.
netstat -ano | findstr ":8787" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto :open

start "Foreman Server" /min cmd /c "python serve.py --no-browser"

rem Wait for the server to come up (max ~15s).
set /a tries=0
:wait
set /a tries+=1
if %tries% gtr 30 (
    echo [Foreman] Server did not start. Run start_foreman.bat to see the error.
    echo [Foreman] 服务启动失败。请运行 start_foreman.bat 查看具体报错。
    pause
    exit /b 1
)
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr ":8787" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 goto :wait

:open
start "" msedge --app=http://127.0.0.1:8787 2>nul || start "" http://127.0.0.1:8787
exit /b 0
