@echo off
cd /d "%~dp0"

echo Checking Python...
python --version

echo Upgrading pip...
python -m pip install --upgrade pip

echo Checking for conflicting fitz package...
python -m pip show fitz >nul 2>&1
if %errorlevel%==0 (
    echo Conflicting 'fitz' package found. Uninstalling...
    python -m pip uninstall -y fitz
    echo Installing required packages...
    python -m pip install --upgrade --force-reinstall pymupdf
    python -m pip install --upgrade psutil requests watchdog
)
else (
    echo Installing required packages...
    python -m pip install --upgrade ollama pymupdf psutil requests watchdog
)

echo Dependencies installed.
