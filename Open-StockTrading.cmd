@echo off
setlocal
set "tradingPython=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%tradingPython%" (
    echo The Stock Trading Python environment is missing.
    echo Open this project in VS Code and follow OPERATING_GUIDE.md to install it.
    echo Expected: "%tradingPython%"
    pause
    exit /b 1
)
pushd "%~dp0"
start "" /D "%~dp0" "%tradingPython%" -m trading_bot.desktop
popd
endlocal
