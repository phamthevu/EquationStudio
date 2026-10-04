@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
 echo Run setup_windows.bat first.
 pause
 exit /b 1
)
echo Downloading open-source OCR models. Internet is required only for preparation.
.venv\Scripts\python.exe main.py --prepare
if errorlevel 1 (
 echo Preparation failed. Check your network and the error above.
 pause
 exit /b 1
)
echo Models prepared. You can now disconnect Internet and run the app.
pause
