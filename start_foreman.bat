@echo off
cd /d %~dp0

if not exist ".env" (
    echo.
    echo [Foreman] Missing .env file in this folder.
    echo [Foreman] Please create a .env file with your DASHSCOPE_API_KEY, e.g.:
    echo.
    echo     DASHSCOPE_API_KEY=sk-your-key-here
    echo.
    echo [Foreman] 缺少 .env 文件。请在本文件夹下创建 .env 文件，并写入你的 DASHSCOPE_API_KEY，例如：
    echo.
    echo     DASHSCOPE_API_KEY=sk-your-key-here
    echo.
    pause
    exit /b 1
)

python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo [Foreman] pip install failed. Check your Python install / network and try again.
    echo [Foreman] 依赖安装失败，请检查 Python 环境或网络后重试。
    pause
    exit /b 1
)

python serve.py
if errorlevel 1 (
    echo.
    echo [Foreman] serve.py exited with an error. See the messages above.
    echo [Foreman] serve.py 运行出错，请查看上方输出信息。
    pause
)
