@echo off
echo ============================================================
echo   Gaze Mouse System -- Windows 11 Installer
echo ============================================================
echo.
echo Step 1: Checking Python...
python --version
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

echo.
echo Step 2: Installing Python dependencies...
pip install -r "%~dp0..\requirements_win.txt"
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo.
echo Step 3: Downloading MediaPipe model...
call "%~dp0download_model.bat"

echo.
echo ============================================================
echo   Installation complete!
echo   Run: scripts\start.bat to launch the system
echo ============================================================
pause
