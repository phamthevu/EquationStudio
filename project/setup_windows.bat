@echo off
setlocal
cd /d "%~dp0"
rem Supports Python 3.10.x and 3.11.x. Prefer the installed 3.10 requested by this app.
set "PY_CMD="
py -3.10 -c "import struct; assert struct.calcsize('P') == 8, 'Python x64 required'" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3.10"
if not defined PY_CMD (
  py -3.11 -c "import struct; assert struct.calcsize('P') == 8, 'Python x64 required'" >nul 2>&1
  if not errorlevel 1 set "PY_CMD=py -3.11"
)
if not defined PY_CMD (
  python -c "import sys,struct; assert sys.version_info[:2] in [(3,10),(3,11)] and struct.calcsize('P') == 8" >nul 2>&1
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD goto fail
if not exist .venv\Scripts\python.exe %PY_CMD% -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto fail
rem Install CPU-only PyTorch to avoid downloading CUDA runtimes.
.venv\Scripts\python.exe -m pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip check
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip freeze > installed_versions.txt
echo Setup complete using %PY_CMD%. Run prepare_models.bat with Internet once, then run_app.bat.
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.10 or 3.11 x64 first, then run this file again.
pause
exit /b 1
