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

rem Existing venvs may predate launcher/backend dependencies.
python -c "import questionary, httpx" >nul 2>&1
if %errorlevel% neq 0 (
    echo [Setup] Updating launcher dependencies...
    pip install -e src\backend[dev]
    if errorlevel 1 goto :err_deps
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
goto :image_setup

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
goto :image_setup

:image_setup
rem 6. Provision the local image backend in an isolated environment.
echo.
echo [Setup] Checking local pixel-art image generation...
set "BACKEND_VIRTUAL_ENV=%VIRTUAL_ENV%"
set "COMFY_WORKSPACE=%CD%\tools\comfy"
set "COMFY_ENV=%CD%\tools\comfy-runtime"
set "COMFY_EXE=%COMFY_ENV%\Scripts\comfy.exe"

if not exist "%COMFY_ENV%\Scripts\python.exe" (
    echo [Setup] Creating isolated ComfyUI environment...
    if not exist "%CD%\tools" mkdir "%CD%\tools"
    python -m venv "%COMFY_ENV%"
    if errorlevel 1 goto :warn_images
)

if not exist "%COMFY_EXE%" (
    echo [Setup] Installing comfy-cli...
    "%COMFY_ENV%\Scripts\python.exe" -m pip install --upgrade pip comfy-cli
    if errorlevel 1 goto :warn_images
)

if not exist "%COMFY_WORKSPACE%\ComfyUI\main.py" (
    echo [Setup] Installing ComfyUI for NVIDIA GPU...
    set "VIRTUAL_ENV=%COMFY_ENV%"
    "%COMFY_EXE%" --skip-prompt --workspace="%COMFY_WORKSPACE%" install --skip-manager --nvidia --fast-deps
    if errorlevel 1 (
        set "VIRTUAL_ENV=%BACKEND_VIRTUAL_ENV%"
        goto :warn_images
    )
    set "VIRTUAL_ENV=%BACKEND_VIRTUAL_ENV%"
)

set "COMFY_MODELS=%COMFY_WORKSPACE%\ComfyUI\models"
if not exist "%COMFY_MODELS%\diffusion_models" mkdir "%COMFY_MODELS%\diffusion_models"
if not exist "%COMFY_MODELS%\text_encoders" mkdir "%COMFY_MODELS%\text_encoders"
if not exist "%COMFY_MODELS%\vae" mkdir "%COMFY_MODELS%\vae"
if not exist "%COMFY_MODELS%\loras" mkdir "%COMFY_MODELS%\loras"

echo [Setup] Ensuring FLUX.2 Klein 4B FP8 is downloaded...
call :download_if_missing "%COMFY_MODELS%\diffusion_models\flux-2-klein-4b-fp8.safetensors" "https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/resolve/main/flux-2-klein-4b-fp8.safetensors"
if %errorlevel% neq 0 goto :warn_images

echo [Setup] Ensuring compact Qwen3 4B FP4 image text encoder is downloaded...
call :download_if_missing "%COMFY_MODELS%\text_encoders\qwen_3_4b_fp4_flux2.safetensors" "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors"
if %errorlevel% neq 0 goto :warn_images

echo [Setup] Ensuring FLUX.2 VAE is downloaded...
call :download_if_missing "%COMFY_MODELS%\vae\flux2-vae.safetensors" "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/split_files/vae/flux2-vae.safetensors"
if %errorlevel% neq 0 goto :warn_images

echo [Setup] Ensuring pixel-art LoRA is downloaded...
call :download_if_missing "%COMFY_MODELS%\loras\pixel-art-lora.safetensors" "https://huggingface.co/Limbicnation/pixel-art-lora/resolve/main/pytorch_lora_weights.comfyui.safetensors"
if %errorlevel% neq 0 goto :warn_images

rem Start ComfyUI only if another instance is not already listening on the expected port.
curl -sf http://127.0.0.1:8188/system_stats >nul 2>&1
if %errorlevel% EQU 0 goto :images_ready

echo [Setup] Starting ComfyUI in low-VRAM background mode...
set "VIRTUAL_ENV=%COMFY_ENV%"
"%COMFY_EXE%" --workspace="%COMFY_WORKSPACE%" launch --background -- --lowvram --disable-auto-launch --port 8188
if errorlevel 1 (
    set "VIRTUAL_ENV=%BACKEND_VIRTUAL_ENV%"
    goto :warn_images
)
set "VIRTUAL_ENV=%BACKEND_VIRTUAL_ENV%"

for /l %%I in (1,1,45) do (
    curl -sf http://127.0.0.1:8188/system_stats >nul 2>&1
    if not errorlevel 1 goto :images_ready
    timeout /t 1 /nobreak >nul
)
goto :warn_images

:images_ready
set "VIRTUAL_ENV=%BACKEND_VIRTUAL_ENV%"
call :set_image_enabled true
echo [Setup] Pixel-art image backend is ready at http://127.0.0.1:8188
goto :launch_menu

:warn_images
set "VIRTUAL_ENV=%BACKEND_VIRTUAL_ENV%"
call :set_image_enabled false
echo [WARNING] Local image generation could not be prepared.
echo [WARNING] PersonalDM will still start normally; generated art will use the existing fallback visuals.
goto :launch_menu

:download_if_missing
set "DOWNLOAD_TARGET=%~1"
set "DOWNLOAD_URL=%~2"
if exist "%DOWNLOAD_TARGET%" exit /b 0
if exist "%DOWNLOAD_TARGET%.part" (
    echo [Setup] Resuming %~nx1 ...
    curl.exe -fL --retry 3 --retry-delay 2 -C - -o "%DOWNLOAD_TARGET%.part" "%DOWNLOAD_URL%"
) else (
    echo [Setup] Downloading %~nx1 ...
    curl.exe -fL --retry 3 --retry-delay 2 -o "%DOWNLOAD_TARGET%.part" "%DOWNLOAD_URL%"
)
if %errorlevel% neq 0 exit /b 1
move /Y "%DOWNLOAD_TARGET%.part" "%DOWNLOAD_TARGET%" >nul
if %errorlevel% neq 0 exit /b 1
exit /b 0

:set_image_enabled
if not exist "%ENV_FILE%" type nul > "%ENV_FILE%"
python -c "from pathlib import Path; p=Path(r'%ENV_FILE%'); lines=p.read_text(encoding='utf-8-sig').splitlines() if p.exists() else []; lines=[line for line in lines if not line.startswith('PDM_IMAGE_ENABLED=')]; lines.append('PDM_IMAGE_ENABLED=%~1'); p.write_text(chr(10).join(lines)+chr(10), encoding='utf-8')"
exit /b 0

:launch_menu
rem 7. Unified GUI/CLI launcher.
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
if errorlevel 1 (
    echo [ERROR] Failed to download Ollama installer. Please check your internet connection.
    pause
    exit /b
)
echo [Setup] Installing Ollama silently (please wait)...
start /wait "" "%TEMP%\OllamaSetup.exe" /silent
if errorlevel 1 (
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
