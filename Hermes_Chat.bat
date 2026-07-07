@echo off
rem ── Talk to Hermes ── your all-capable AI agent (runs on free Gemini).
rem Hermes operates your computer, edits files, runs commands, remembers you,
rem searches the web — and when you hand it a long list of tasks it uses its
rem Foreman engine to do them one-by-one and verify each. Just type what you
rem want in plain language.
setlocal
set HERMES=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\hermes.exe

if not exist "%HERMES%" (
    echo [Hermes] Not installed. Install it first ^(PowerShell^):
    echo     iex ^(irm https://hermes-agent.nousresearch.com/install.ps1^)
    pause
    exit /b 1
)

echo.
echo   Hermes — your AI agent (free Gemini). Examples you can type:
echo     换个桌面壁纸 / 把这个文件夹里的图片按日期重命名
echo     帮我把这份需求清单里的功能全做了：（贴清单）
echo.
"%HERMES%" %*
