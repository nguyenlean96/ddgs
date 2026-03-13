@echo off
REM DDGS Build Script for Windows

echo Building DDGS executable...
echo.

REM Check if virtual environment exists
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Install dependencies including PyInstaller
echo Installing dependencies...
pip install -e .[dev]

REM Build executable
echo Building executable with PyInstaller...
pyinstaller ddgs.spec --clean

echo.
echo Build complete! Executable is in dist\ddgs.exe
echo.

pause