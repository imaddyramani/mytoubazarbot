@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==========================================
echo MyTourBazar Bot V167
 echo Automatic dependency check + auto restart
 echo ==========================================
echo.

REM Keep WeasyPrint's Windows DLL path available when MSYS2 UCRT64 is installed.
if exist "C:\msys64\ucrt64\bin" set "WEASYPRINT_DLL_DIRECTORIES=C:\msys64\ucrt64\bin"

REM Install dependencies only when a required Python package is missing.
python -c "import telegram, dotenv, pypdf, PIL, fitz, reportlab, google.genai, weasyprint" >nul 2>&1
if errorlevel 1 (
    echo Required Python packages are missing. Installing from requirements.txt...
    python -m pip install --upgrade pip
    if errorlevel 1 goto INSTALL_FAIL
    python -m pip install -r requirements.txt
    if errorlevel 1 goto INSTALL_FAIL
    echo Dependencies installed successfully.
    echo.
)

:run
cls
echo ==========================================
echo MyTourBazar Bot V167 - RUNNING
echo ==========================================
echo.
python bot.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Bot process ended with code %EXIT_CODE%.
if "%EXIT_CODE%"=="0" (
    echo Bot stopped normally. Restarting in 3 seconds...
) else (
    echo Bot stopped unexpectedly. Restarting in 5 seconds...
)
timeout /t 5 /nobreak >nul
goto run

:INSTALL_FAIL
echo.
echo ==========================================
echo DEPENDENCY INSTALLATION FAILED
 echo ==========================================
echo Please keep this CMD window open and copy the error above.
echo.
pause
exit /b 1
