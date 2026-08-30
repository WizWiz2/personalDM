@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title Personal DM
cls

echo =======================================================================
echo                  PERSONAL DM - ONE-CLICK LAUNCHER
echo =======================================================================
echo.

rem 1. Verify Python.
python --version >nul 2>&1
if errorlevel 1 goto :err_python

rem 2. Create/activate backend virtual environment.
if not exist "src\backend\venv\Scripts\python.exe" (
    echo [Setup] Creating backend virtual environment...
    python -m venv src\backend\venv
    if errorlevel 1 goto :err_venv
)

call src\backend\venv\Scripts\activate.bat

rem Existing installations may predate launcher/provider dependencies.
python -c "import questionary, httpx, fastapi" >nul 2>&1
if errorlevel 1 (
    echo [Setup] Installing/updating backend dependencies...
    python -m pip install --upgrade pip
    if errorlevel 1 goto :err_deps
    pip install -e src\backend[dev]
    if errorlevel 1 goto :err_deps
)

rem 3. Apply database migrations on every launch; Alembic is idempotent.
echo [Setup] Checking user data location...
python src\backend\launcher.py --migrate-user-data
if errorlevel 1 goto :err_data
echo [Setup] Checking database schema...
pushd src\backend
alembic upgrade head
set "MIGRATION_RC=%ERRORLEVEL%"
popd
if not "%MIGRATION_RC%"=="0" goto :err_migrations

rem 4. First run asks for text and image providers. Later runs only check the
rem selected providers and repair/start local infrastructure when necessary.
echo [Setup] Checking model providers...
python src\backend\launcher.py --bootstrap-providers
if errorlevel 1 goto :end

rem 5. Unified GUI/CLI menu.
echo.
python src\backend\launcher.py
goto :end

:err_python
echo [ERROR] Python is not installed or is not available in PATH.
echo Install Python 3.11 or newer and run play.bat again.
pause
exit /b 1

:err_venv
echo [ERROR] Failed to create backend virtual environment.
pause
exit /b 1

:err_deps
echo [ERROR] Failed to install backend dependencies.
pause
exit /b 1

:err_migrations
echo [ERROR] Database migration failed.
pause
exit /b 1

:err_data
echo [ERROR] Failed to move the game library to the user data folder.
pause
exit /b 1

:end
echo.
echo =======================================================================
echo                     Session closed. Goodbye!
echo =======================================================================
pause
