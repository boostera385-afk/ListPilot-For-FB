@echo off
title Update Libraries

cd /d "%~dp0"
set VENV_DIR=.venv

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run "Start Automation" first.
    pause
    exit /b
)

call "%VENV_DIR%\Scripts\activate.bat"
echo Checking and updating required libraries...
python -m pip install --upgrade -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Library update failed.
    pause
    exit /b 1
)
python core\utils\dependency_setup.py mark
echo.
echo [SUCCESS] Libraries are up to date!
pause
exit
