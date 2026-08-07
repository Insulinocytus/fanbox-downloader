@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Please install Python 3 first.
    echo https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -c "import curl_cffi" >nul 2>&1
if errorlevel 1 (
    echo Installing missing dependency: curl_cffi
    python -m pip install --user curl_cffi
    if errorlevel 1 (
        echo.
        echo curl_cffi installation failed.
        echo Check your Internet connection and run this file again.
        echo.
        pause
        exit /b 1
    )
)

python fanbox_dl.py
echo.
pause
