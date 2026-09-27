@echo off
REM ============================================
REM  J.A.R.V.I.S. installer for Windows
REM ============================================
echo.
echo  ==========================================
echo   J.A.R.V.I.S.  -  Windows Setup
echo  ==========================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Get it from https://www.python.org/downloads/
    echo         IMPORTANT: tick "Add Python to PATH" during install!
    pause
    exit /b 1
)

echo [1/3] Python found:
python --version

echo.
echo [2/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r "%~dp0requirements.txt"

if %errorlevel% neq 0 (
    echo.
    echo [WARN] Some packages failed - trying PyAudio via pipwin...
    python -m pip install pipwin
    python -m pipwin install pyaudio
)

echo.
echo [3/3] Setup complete! JARVIS works even if some voice packages failed
echo       (it falls back to text mode). 
echo.
echo  To start JARVIS :  start_jarvis.bat   (or: python jarvis.py)
echo  Text-only mode  :  python jarvis.py --text
echo.
pause
