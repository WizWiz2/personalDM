@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title Personal DM - Live Model Contracts

python --version >nul 2>&1
if errorlevel 1 goto :err_python

if not exist "src\backend\venv\Scripts\python.exe" (
    echo [Setup] Creating backend virtual environment...
    python -m venv src\backend\venv
    if errorlevel 1 goto :err_venv
)

call src\backend\venv\Scripts\activate.bat
python -c "import fastapi, httpx, sqlalchemy, alembic" >nul 2>&1
if errorlevel 1 (
    echo [Setup] Installing/updating backend test dependencies...
    python -m pip install --upgrade pip
    if errorlevel 1 goto :err_deps
    pip install -e src\backend[dev]
    if errorlevel 1 goto :err_deps
)

echo =======================================================================
echo              PERSONAL DM - REAL LOCAL MODEL CONTRACTS
echo =======================================================================
echo.
echo This suite uses the actual Ollama models and an isolated temporary game DB.
echo It does NOT use pytest Planner/Validator/Scribe mocks and does not touch
 echo your normal campaign library or provider .env.
echo.

pushd src\backend
python -m live_model_contracts.runner %*
set "RC=%ERRORLEVEL%"
popd

echo.
if "%RC%"=="0" (
    echo [PASS] Live model contracts passed.
) else (
    echo [FAIL] One or more live model contracts failed. See the report path above.
)
exit /b %RC%

:err_python
echo [ERROR] Python is not installed or is not available in PATH.
exit /b 1

:err_venv
echo [ERROR] Failed to create backend virtual environment.
exit /b 1

:err_deps
echo [ERROR] Failed to install backend dependencies.
exit /b 1
