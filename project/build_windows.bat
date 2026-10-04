@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
 echo Run setup_windows.bat first.
 pause
 exit /b 1
)
.venv\Scripts\python.exe -m pip install "pyinstaller>=6.11,<7"
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --console --name EquationStudio --collect-all pix2text --collect-all cnocr --collect-all cnstd --collect-all latex2mathml --collect-all mathml2omml --collect-all transformers --collect-all optimum --collect-all onnxruntime --collect-all rapidocr --collect-all doclayout_yolo --hidden-import ocr_worker main.py
if errorlevel 1 goto fail
echo EXE created in dist\EquationStudio\EquationStudio.exe
echo Keep the entire folder. OCR model cache is separate; prepare it on the target computer.
pause
exit /b 0
:fail
echo Build failed. Read the error above.
pause
exit /b 1
