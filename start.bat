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

python -c "from curl_cffi import requests; from cloakbrowser import launch_persistent_context" >nul 2>&1
if errorlevel 1 (
    echo Installing missing dependencies: curl_cffi cloakbrowser typing_extensions
    python -m pip install --user curl_cffi cloakbrowser typing_extensions
    if errorlevel 1 (
        echo.
        echo Dependency installation failed.
        echo Check your Internet connection and run this file again.
        echo.
        pause
        exit /b 1
    )
)

python -c "from curl_cffi import requests; from cloakbrowser import launch_persistent_context" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Required Python packages still cannot be imported.
    echo Please use Python 3.10 or newer, then run this file again.
    echo.
    pause
    exit /b 1
)

python fanbox_dl.py
echo.
pause
