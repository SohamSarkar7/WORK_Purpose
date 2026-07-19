@echo off
setlocal

echo ============================================
echo  PRC Factsheet Automation - Windows Build
echo ============================================
echo.
echo This will:
echo   1. Create a private Python virtual environment (.venv)
echo   2. Install all dependencies (downloads ~1-2 GB, needs internet)
echo   3. Sanity-check that everything imports correctly
echo   4. Build the .exe with PyInstaller
echo.
echo This can take 10-20 minutes the first time. Please be patient.
echo.
pause

where py >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] The "py" launcher was not found. This usually means Python
    echo was installed without the launcher option checked.
    echo Install 64-bit Python 3.11 from:
    echo   https://www.python.org/downloads/release/python-3119/
    echo During install, check BOTH "Add python.exe to PATH" AND
    echo "Install launcher for all users (recommended)".
    echo Then re-run this script.
    pause
    exit /b 1
)

py -3.11 -V >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python 3.11 ^(64-bit^) was not found.
    echo.
    echo This app's dependencies ^(torch / numpy / easyocr^) only ship
    echo pre-built Windows installers for certain Python versions. If your
    echo main "python" is something very new ^(3.13, 3.14...^), pip will try
    echo to compile numpy/torch from source instead of downloading a ready
    echo binary - and that fails without Visual Studio's C++ compiler
    echo installed, producing exactly the "Unknown compiler(s)" / meson
    echo error you may have already seen.
    echo.
    echo Fix: install 64-bit Python 3.11 ALONGSIDE your existing Python -
    echo it will NOT replace or break your current Python install.
    echo   1. Download: https://www.python.org/downloads/release/python-3119/
    echo      ^(scroll down to "Windows installer (64-bit)"^)
    echo   2. Run the installer. Check BOTH:
    echo        [x] Add python.exe to PATH
    echo        [x] Install launcher for all users
    echo   3. Re-run this script.
    pause
    exit /b 1
)

echo.
echo [1/4] Creating virtual environment (.venv) with Python 3.11 ...
if exist .venv (
    echo Removing old/broken virtual environment first ...
    rmdir /s /q .venv
)
py -3.11 -m venv .venv
if errorlevel 1 (
    echo [ERROR] Failed to create the virtual environment.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

echo.
echo [2/4] Installing dependencies from requirements.txt ...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Scroll up to see which
    echo package failed and why, fix it, then re-run this script.
    pause
    exit /b 1
)

echo.
echo [3/4] Sanity-checking that all dependencies import correctly ...
python -c "import fitz, cv2, numpy, pandas, openpyxl, PIL, easyocr, torch; print('All dependencies import OK')"
if errorlevel 1 (
    echo.
    echo [ERROR] A dependency failed to import - see the error above.
    echo This must be fixed before building, otherwise the .exe will fail
    echo the same way. A common cause is a numpy/torch version mismatch;
    echo try: pip install -U numpy torch
    pause
    exit /b 1
)

echo.
echo [4/4] Building the .exe with PyInstaller (this also takes a few minutes) ...
pyinstaller --noconfirm PRC_App.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See the error above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  BUILD COMPLETE
echo.
echo  Your app:  dist\PRC_Factsheet_Automation\PRC_Factsheet_Automation.exe
echo.
echo  To share/run it elsewhere, copy the ENTIRE
echo  "dist\PRC_Factsheet_Automation" folder (not just the .exe) -
echo  it depends on the DLLs and data files sitting next to it.
echo ============================================
pause
