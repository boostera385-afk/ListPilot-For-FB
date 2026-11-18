@echo off
setlocal enabledelayedexpansion
TITLE ListPilot Launcher
cd /d "%~dp0"
echo ==========================================
echo ListPilot - Post, track and verify every Marketplace listing
echo ==========================================

:: 1. Check if Python is installed
set PYTHON_CMD=python
%PYTHON_CMD% --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [INFO] Python is not detected on this system.
    echo [INFO] Auto-downloading Python 3.11 for you. Please wait...
    
    :: Use curl to download the Python installer silently (built into Windows 10/11)
    curl -L -o python_installer.exe https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe
    
    IF NOT EXIST "python_installer.exe" (
        echo [ERROR] Failed to download Python. Please check your internet connection.
        pause
        exit /b
    )

    echo [INFO] Installing Python silently. This may take 1-2 minutes...
    :: /quiet runs silently. PrependPath=1 adds it to PATH.
    start /wait python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1
    
    echo [INFO] Python installed successfully! Cleaning up installer...
    del python_installer.exe

    :: Since the installer just ran, the PATH variable in this specific batch window is not updated yet.
    :: We will point directly to the default installation path for Python 3.11.
    set PYTHON_CMD="%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe"
    
    if not exist !PYTHON_CMD! (
        echo [ERROR] Could not locate the newly installed Python. 
        echo Please close this window, restart your computer, and double-click Start_ListPilot again.
        pause
        exit /b
    )
)

:: 2. Create the virtual environment only once
IF NOT EXIST ".venv\Scripts\python.exe" (
    echo [INFO] Creating an isolated virtual environment...
    !PYTHON_CMD! -m venv .venv
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Could not create the Python environment.
        pause
        exit /b 1
    )
)

:: 3. Install libraries only on first setup or when requirements.txt changes
set VENV_PY=.venv\Scripts\python.exe
set DEPS_MARKER=.venv\.listpilot_dependencies_v1.ready
IF EXIST "!DEPS_MARKER!" GOTO :launch

!VENV_PY! core\utils\dependency_setup.py check >nul 2>&1
IF !ERRORLEVEL! NEQ 0 (
    echo [INFO] Installing required libraries. This is needed only once...
    !VENV_PY! -m pip install --upgrade pip
    if !ERRORLEVEL! NEQ 0 goto :dependency_error
    !VENV_PY! -m pip install -r requirements.txt
    if !ERRORLEVEL! NEQ 0 goto :dependency_error
    !VENV_PY! core\utils\dependency_setup.py mark
    if !ERRORLEVEL! NEQ 0 goto :dependency_error
) ELSE (
    echo [OK] Libraries are already installed. Skipping downloads.
)

:: 4. Launch the app
:launch
:: Keep desktop shortcuts pointing here (old Kitkat name or a renamed folder). Never blocks startup.
powershell -NoProfile -ExecutionPolicy Bypass -File "core\utils\fix_shortcuts.ps1" -Root "%~dp0." 2>nul
echo ==========================================
echo Launching ListPilot...
echo ==========================================
!VENV_PY! main.py

echo.
echo ListPilot has closed. Press any key to exit.
pause >nul
exit /b 0

:dependency_error
echo.
echo [ERROR] Required libraries could not be installed.
echo Check your internet connection, then run Start_ListPilot again.
pause
exit /b 1
