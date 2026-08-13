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

rem 1. Verify Python
python --version >nul 2>&1
if %errorlevel% neq 0 goto :err_python

rem 2. Check Virtual Environment
if exist "src\backend\venv" goto :activate_venv

echo [Setup] Creating virtual environment (venv)...
python -m venv src\backend\venv
if %errorlevel% neq 0 goto :err_venv

echo [Setup] Activating venv and installing dependencies...
call src\backend\venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e src\backend[dev]
if %errorlevel% neq 0 goto :err_deps

echo [Setup] Initializing database schema...
cd src\backend
alembic upgrade head
cd ..\..
echo [Setup] Database initialized.
goto :check_llm

:activate_venv
echo [Setup] Virtual environment found. Activating...
call src\backend\venv\Scripts\activate.bat

rem Existing venvs may predate the interactive launcher dependency.
python -c "import questionary" >nul 2>&1
if %errorlevel% neq 0 (
    echo [Setup] Updating launcher dependencies...
    pip install -e src\backend[dev]
    if %errorlevel% neq 0 goto :err_deps
)

:check_llm
rem 3. Use either a configured cloud provider or a local Ollama installation.
set "ENV_FILE=src\backend\.env"
set "CLOUD_API_KEY="
if exist "%ENV_FILE%" (
    for /f "tokens=1,* delims==" %%A in ('findstr /b /c:"PDM_LLM_API_KEY=" "%ENV_FILE%"') do set "CLOUD_API_KEY=%%B"
)
if not "%CLOUD_API_KEY%"=="" goto :cloud_ok

call :find_ollama
if defined OLLAMA_CMD goto :ollama_ok

python src\backend\launcher.py --provider-choice
set "PROVIDER_CHOICE=%ERRORLEVEL%"
if %PROVIDER_CHOICE% GEQ 30 goto :end
if %PROVIDER_CHOICE% GEQ 20 goto :configure_cloud
if %PROVIDER_CHOICE% GEQ 10 goto :err_ollama
goto :end

:configure_cloud
echo.
echo Cloud settings are saved only to %ENV_FILE% (this file is ignored by Git).
set /p "CLOUD_BASE_URL=Base URL (for OpenAI: https://api.openai.com/v1): "
if "%CLOUD_BASE_URL%"=="" set "CLOUD_BASE_URL=https://api.openai.com/v1"
set /p "CLOUD_MODEL=Model (for example: gpt-4.1-mini): "
if "%CLOUD_MODEL%"=="" set "CLOUD_MODEL=gpt-4.1-mini"
set /p "CLOUD_API_KEY=API key: "
if "%CLOUD_API_KEY%"=="" (
    echo [ERROR] An API key is required for a cloud provider.
    pause
    goto :check_llm
)
set /p "CLOUD_CONTEXT=Context window (default: 128000): "
if "%CLOUD_CONTEXT%"=="" set "CLOUD_CONTEXT=128000"
> "%ENV_FILE%" (
    echo PDM_LLM_BASE_URL=%CLOUD_BASE_URL%
    echo PDM_LLM_MODEL=%CLOUD_MODEL%
    echo PDM_LLM_API_KEY=%CLOUD_API_KEY%
    echo PDM_LLM_CONTEXT_WINDOW=%CLOUD_CONTEXT%
    echo PDM_CONTROL_LLM_MODEL=%CLOUD_MODEL%
)
echo [Setup] Cloud provider configured.
goto :cloud_ok

:cloud_ok
echo [Setup] Cloud LLM configuration found in %ENV_FILE%.
goto :launch_menu

:check_ollama
call :find_ollama
if defined OLLAMA_CMD goto :ollama_ok
goto :err_ollama

:find_ollama
set "OLLAMA_CMD=ollama"
ollama --version >nul 2>&1
if %errorlevel% EQU 0 exit /b 0

if exist "%LocalAppData%\Programs\Ollama\ollama.exe" (
    set "OLLAMA_CMD=%LocalAppData%\Programs\Ollama\ollama.exe"
    exit /b 0
)

if exist "%ProgramFiles%\Ollama\ollama.exe" (
    set "OLLAMA_CMD=%ProgramFiles%\Ollama\ollama.exe"
    exit /b 0
)

set "OLLAMA_CMD="
exit /b 0

:ollama_ok
rem 4. Verify/Start Ollama Service
echo [Setup] Checking if Ollama service is running...
curl -s -I http://localhost:11434/ >nul 2>&1
if %errorlevel% EQU 0 goto :pull_model

echo [Setup] Ollama is installed but not running. Starting Ollama app...
if exist "%LocalAppData%\Programs\Ollama\ollama app.exe" start "" "%LocalAppData%\Programs\Ollama\ollama app.exe" & goto :wait_ollama
if exist "%ProgramFiles%\Ollama\ollama app.exe" start "" "%ProgramFiles%\Ollama\ollama app.exe" & goto :wait_ollama
start /B "" "%OLLAMA_CMD%" serve >nul 2>&1

:wait_ollama
echo Waiting for Ollama service to boot up...
timeout /t 5 /nobreak >nul
curl -s -I http://localhost:11434/ >nul 2>&1
if %errorlevel% EQU 0 goto :pull_model
echo [WARNING] Could not automatically start Ollama.
echo Please make sure Ollama is open and running in your taskbar, then press any key.
pause

:pull_model
rem 5. Pull Gemma
echo [Setup] Ensuring Gemma 4 (4B parameters) is downloaded...
"%OLLAMA_CMD%" pull gemma4:e4b
if %errorlevel% neq 0 goto :err_gemma
echo [Setup] Gemma 4 model is ready!
goto :launch_menu

:launch_menu
rem 6. Unified GUI/CLI launcher.
echo.
python src\backend\launcher.py
goto :end

:err_python
echo [ERROR] Python is not installed or not added to your system PATH!
echo Please install Python 3.11 or higher.
pause
exit /b

:err_venv
echo [ERROR] Failed to create virtual environment!
pause
exit /b

:err_deps
echo [ERROR] Failed to install dependencies!
pause
exit /b

:err_ollama
echo [Setup] Ollama is not installed!
echo [Setup] Downloading Ollama installer automatically...
curl -L -o "%TEMP%\OllamaSetup.exe" https://ollama.com/download/OllamaSetup.exe
if %errorlevel% neq 0 (
    echo [ERROR] Failed to download Ollama installer. Please check your internet connection.
    pause
    exit /b
)
echo [Setup] Installing Ollama silently (please wait)...
start /wait "" "%TEMP%\OllamaSetup.exe" /silent
if %errorlevel% neq 0 (
    echo [ERROR] Ollama installation failed.
    pause
    exit /b
)
echo [Setup] Ollama installed successfully!
goto :check_ollama

:err_gemma
echo [ERROR] Failed to pull gemma4:e4b model.
echo Please check your internet connection and ensure Ollama is active.
pause
exit /b

:end
echo.
echo =======================================================================
echo                     Session closed. Goodbye!
echo =======================================================================
pause
