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

set "PDM_GIT_BRANCH=unknown"
set "PDM_GIT_SHA=unknown"
for /f "delims=" %%i in ('git branch --show-current 2^>nul') do set "PDM_GIT_BRANCH=%%i"
for /f "delims=" %%i in ('git rev-parse --short HEAD 2^>nul') do set "PDM_GIT_SHA=%%i"
echo [Setup] Source checkout: %PDM_GIT_BRANCH% @ %PDM_GIT_SHA%
python -c "from pathlib import Path; runtime=Path(r'src/backend/app/runtime.py').read_text(encoding='utf-8'); pipeline=Path(r'src/backend/app/services/turn_intent_pipeline.py'); assert pipeline.is_file(), 'turn_intent_pipeline.py is missing'; assert 'install_turn_intent_pipeline()' in runtime, 'runtime.py does not install frozen-intent planning'; assert 'frozen_player_intent_v1' in runtime, 'runtime manifest does not declare frozen_player_intent_v1'; print('[Setup] Frozen-intent source preflight: OK')"
if errorlevel 1 goto :err_stale_source

echo [Setup] Checking Ollama runtime and required models...
pushd src\backend
python -m live_model_contracts.bootstrap %*
set "BOOTSTRAP_RC=%ERRORLEVEL%"
if not "%BOOTSTRAP_RC%"=="0" (
    popd
    echo.
    echo [FAIL] Live model runtime bootstrap failed.
    exit /b %BOOTSTRAP_RC%
)

echo.
python -m live_model_contracts.diagnostic_console_runner %*
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

:err_stale_source
echo [ERROR] Live model suite refused to start because this checkout does not contain the frozen-intent production pipeline.
echo [ERROR] Verify the current branch and pull feat/truth-engine-2-foundation before running expensive model contracts.
exit /b 1
