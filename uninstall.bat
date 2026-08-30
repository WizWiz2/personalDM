@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title Personal DM - Uninstall
if not exist "src\backend\venv\Scripts\python.exe" goto :err_venv
call "src\backend\venv\Scripts\activate.bat"
python src\backend\launcher.py --uninstall
pause
exit /b %ERRORLEVEL%
:err_venv
echo [ERROR] PersonalDM environment not found. Run play.bat once first.
pause
exit /b 1
