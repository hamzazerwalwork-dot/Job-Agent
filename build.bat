@echo off
cd /d "%~dp0"
echo Installing dependencies...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail
echo Building JobAgent.exe ...
python -m PyInstaller --noconfirm --onefile --windowed --name JobAgent --collect-all playwright app.py
if errorlevel 1 goto fail
if not exist "dist\JobAgent.exe" goto fail
echo.
echo SUCCESS. Your app is here: %~dp0dist\JobAgent.exe
echo Opening the folder for you...
explorer /select,"%~dp0dist\JobAgent.exe"
pause
exit /b 0
:fail
echo.
echo BUILD FAILED. Scroll up, copy the last 15 lines of red/error text and send them to Claude.
pause
exit /b 1
