@echo off
title J.A.R.V.I.S.
cd /d "%~dp0"
python jarvis.py
if %errorlevel% neq 0 (
    echo.
    echo JARVIS exited with an error. Trying text mode...
    python jarvis.py --text
)
pause
