import os
import sys
import time
import subprocess
import urllib.request
import json
from pathlib import Path

import socket

# DNS Hook to prevent getaddrinfo failures for openrouter.ai
original_getaddrinfo = socket.getaddrinfo

def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == "openrouter.ai":
        return original_getaddrinfo("104.18.3.115", port, family, type, proto, flags)
    return original_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = custom_getaddrinfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "src" / "backend"))
sys.path.insert(0, str(REPOSITORY_ROOT / "src" / "backend" / "tests"))

def log_message(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def wait_for_server(url, timeout_secs=180):
    log_message(f"Waiting for server {url} to initialize...")
    start_time = time.time()
    while time.time() - start_time < timeout_secs:
        try:
            req = urllib.request.Request(f"{url}/health")
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    if data.get("status") == "ok":
                        log_message("Server is ready!")
                        return True
        except Exception:
            pass
        time.sleep(2)
    log_message("Timeout waiting for server to become ready!")
    return False

def get_openrouter_key():
    env_path = Path(r"C:\Users\User\AppData\Local\hermes\.env")
    if not env_path.exists():
        return None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    return line.strip().split("=")[1].strip()
    except Exception:
        pass
    return None

def run_simulation(model_name, base_url, data_dir_name, context_window, turns, api_key=None):
    data_dir = REPOSITORY_ROOT / "test" / "backend" / "tests" / "data" / data_dir_name
    data_dir.mkdir(parents=True, exist_ok=True)
    
    log_message(f"=== Starting simulation for model: {model_name} ===")
    log_message(f"Data directory: {data_dir}")
    
    env = os.environ.copy()
    env["PDM_SIM_MODEL"] = model_name
    env["PDM_SIM_BASE_URL"] = base_url
    env["PDM_SIM_DATA_DIR"] = str(data_dir)
    env["PDM_SIM_CONTEXT_WINDOW"] = str(context_window)
    env["PDM_SIM_TURNS"] = str(turns)
    env["PDM_SIM_RESET"] = "1"
    env["PDM_SIM_MODE"] = "smoke"
    env["PDM_SIM_STOP_ON_PROVIDER_FAILURE"] = "0"
    env["PYTHONUTF8"] = "1"
    if api_key:
        env["LLM_API_KEY"] = api_key
        env["PDM_LLM_API_KEY"] = api_key
    
    # Terminate any dangling simulation processes from previous runs to release SQLite locks
    try:
        subprocess.run([
            "powershell", "-Command",
            "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*run_persistent_simulation.py*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        ], capture_output=True)
    except Exception:
        pass
    time.sleep(2) # Cooldown to let OS release file handles

    python_exe = sys.executable
    sim_launcher = REPOSITORY_ROOT / "test" / "backend" / "tests" / "run_persistent_simulation.py"
    
    start_time = time.time()
    try:
        process = subprocess.Popen(
            [python_exe, "-u", str(sim_launcher)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        # Stream output in real-time
        for line in process.stdout:
            # Only print every 5th turn log line or summary lines to avoid cluttering logs
            cleaned = line.strip()
            if "turn" in cleaned.lower() or "completed" in cleaned.lower() or "error" in cleaned.lower() or "saved" in cleaned.lower():
                print(f"  [SIM] {cleaned}", flush=True)
                
        process.wait()
        duration = time.time() - start_time
        
        if process.returncode == 0:
            log_message(f"=== Model {model_name} simulation completed successfully in {duration/60:.1f} mins ===")
            return True
        else:
            log_message(f"=== Model {model_name} simulation failed with exit code {process.returncode} ===")
            return False
    except Exception as e:
        log_message(f"Error running simulation: {e}")
        return False

def main():
    turns = int(os.getenv("PDM_SIM_TURNS", "200"))
    context_window = int(os.getenv("PDM_SIM_CONTEXT_WINDOW", "6144"))
    
    log_message("=" * 60)
    log_message("       STARTING SEQUENTIAL MULTI-MODEL SIMULATION RUN")
    log_message(f"       Total Turns: {turns} | Context Window: {context_window}")
    log_message("=" * 60)
    
    # ----------------------------------------------------
    # Model 1: Qwen 2.5 7B (Ollama) - SKIPPED (already run)
    # ----------------------------------------------------
    log_message("=== Skipping Qwen 2.5 7B (already simulated) ===")
    
    # ----------------------------------------------------
    # Model 2: Gemma 4 8B (Ollama) - SKIPPED (already run)
    # ----------------------------------------------------
    log_message("=== Skipping Gemma 4 8B (already simulated) ===")
    
    # ----------------------------------------------------
    # Model 3: Bonsai 27B 1-bit (llama-server) - SKIPPED
    # ----------------------------------------------------
    log_message("=== Skipping Bonsai-27B due to 1-bit Russian token degradation ===")
    
    # ----------------------------------------------------
    # ----------------------------------------------------
    # Model 4: Llama 3.1 8B (OpenRouter Cloud)
    # ----------------------------------------------------
    openrouter_key = get_openrouter_key()
    if openrouter_key:
        run_simulation(
            model_name="meta-llama/llama-3.1-8b-instruct",
            base_url="https://openrouter.ai/api/v1",
            data_dir_name="openrouter_llama",
            context_window=context_window,
            turns=turns,
            api_key=openrouter_key
        )
        
        # ----------------------------------------------------
        # Model 5: Gemma 4 31B (OpenRouter Cloud Free)
        # ----------------------------------------------------
        run_simulation(
            model_name="google/gemma-4-31b-it:free",
            base_url="https://openrouter.ai/api/v1",
            data_dir_name="openrouter_gemma_31b",
            context_window=context_window,
            turns=turns,
            api_key=openrouter_key
        )
        
        # ----------------------------------------------------
        # Model 6: Tencent Hunyuan 3 (OpenRouter Cloud Free)
        # ----------------------------------------------------
        run_simulation(
            model_name="tencent/hy3:free",
            base_url="https://openrouter.ai/api/v1",
            data_dir_name="openrouter_hy3",
            context_window=context_window,
            turns=turns,
            api_key=openrouter_key
        )
    else:
        log_message("=== OpenRouter key not found, skipping cloud simulation ===")
            
    log_message("=" * 60)
    log_message("       ALL MULTI-MODEL SIMULATIONS COMPLETED!")
    log_message("=" * 60)

if __name__ == "__main__":
    main()
