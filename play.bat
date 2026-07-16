@echo off
cd /d "%~dp0"
title Personal DM - One-Click Launcher
cls
echo =======================================================================
echo              PERSONAL DM - AUTOMATIC ENV SETUP AND LAUNCHER
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
goto :check_ollama

:activate_venv
echo [Setup] Virtual environment found. Activating...
call src\backend\venv\Scripts\activate.bat

:check_ollama
rem 3. Verify Ollama Installation (checking PATH first, then default install paths)
set "OLLAMA_CMD=ollama"
ollama --version >nul 2>&1
if %errorlevel% EQU 0 goto :ollama_ok

if exist "%LocalAppData%\Programs\Ollama\ollama.exe" (
    set "OLLAMA_CMD=%LocalAppData%\Programs\Ollama\ollama.exe"
    goto :ollama_ok
)

if exist "%ProgramFiles%\Ollama\ollama.exe" (
    set "OLLAMA_CMD=%ProgramFiles%\Ollama\ollama.exe"
    goto :ollama_ok
)

goto :err_ollama

:ollama_ok
rem 4. Verify/Start Ollama Service
echo [Setup] Checking if Ollama service is running...
curl -s -I http://localhost:11434/ >nul 2>&1
if %errorlevel% EQU 0 goto :pull_model

echo [Setup] Ollama is installed but not running. Starting Ollama app...
if exist "%LocalAppData%\Programs\Ollama\ollama app.exe" start "" "%LocalAppData%\Programs\Ollama\ollama app.exe" & goto :wait_ollama
if exist "%ProgramFiles%\Ollama\ollama app.exe" start "" "%ProgramFiles%\Ollama\ollama app.exe" & goto :wait_ollama

rem Fallback launch daemon
start /B "" "%OLLAMA_CMD%" serve >nul 2>&1

:wait_ollama
echo Waiting for Ollama service to boot up...
timeout /t 5 /nobreak >nul

rem Double check
curl -s -I http://localhost:11434/ >nul 2>&1
if %errorlevel% EQU 0 goto :pull_model
echo [WARNING] Could not automatically start Ollama.
echo Please make sure Ollama is open and running in your taskbar, then press any key.
pause

:pull_model
rem 5. Pull Gemma
echo [Setup] Ensuring Gemma 4 (4B parameters) is downloaded...
echo Running: %OLLAMA_CMD% pull gemma4:e4b
"%OLLAMA_CMD%" pull gemma4:e4b
if %errorlevel% neq 0 goto :err_gemma
echo [Setup] Gemma 4 model is ready!

rem 6. Run CLI
echo [Launch] Starting Game Console Mirror...
echo.
cd src\backend
python cli.py
cd ..\..
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
