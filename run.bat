@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    )
    if not exist ".venv\Scripts\python.exe" (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3.10 or newer was not found. Install Python, then run this file again.
            pause
            exit /b 1
        )
        python -m venv .venv
    )
    if not exist ".venv\Scripts\python.exe" exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10 or newer is required.'"
if errorlevel 1 (
    echo Python 3.10 or newer is required. Update Python and run this file again.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed. Check your internet connection and retry.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "from document_ingestor.visual import ocr_status; status=ocr_status(); print('OCR ready: Tesseract ' + status.get('version', '') if status.get('available') else 'OCR setup needed: ' + status.get('message', 'Install Tesseract OCR.'))"
echo.
echo Starting DocIntake at http://127.0.0.1:8000/
echo Keep this window open while you use the workspace.
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
