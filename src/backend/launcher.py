from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from app.cli_ui import select_menu


BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parents[1]
FRONTEND_DIR = ROOT_DIR / "src" / "frontend"
BACKEND_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://127.0.0.1:5173"


def _url_ready(url: str, timeout: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:
        return False


def _wait_ready(url: str, process: subprocess.Popen | None, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _url_ready(url):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def _wait_stopped(url: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _url_ready(url):
            return True
        time.sleep(0.2)
    return not _url_ready(url)


def _npm_command(*args: str) -> list[str]:
    if os.name == "nt":
        return ["cmd", "/d", "/c", "npm", *args]
    return ["npm", *args]


def _ensure_frontend_dependencies() -> bool:
    if not (shutil.which("npm") or shutil.which("npm.cmd")):
        print("[Ошибка] Для GUI нужен Node.js с npm. Установи Node.js 20+ и повтори запуск.")
        return False
    vite = FRONTEND_DIR / "node_modules" / ".bin" / ("vite.cmd" if os.name == "nt" else "vite")
    if vite.exists():
        return True
    print("[Setup] Frontend dependencies не найдены. Выполняю npm install...")
    try:
        subprocess.run(
            _npm_command("install", "--no-audit", "--no-fund"),
            cwd=FRONTEND_DIR,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        print("[Ошибка] npm install завершился с ошибкой.")
        return False


def _terminate_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()


def _windows_listener_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        output = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            errors="ignore",
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    suffix = f":{port}"
    for raw in output.splitlines():
        parts = raw.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local_address, state, pid_text = parts[1], parts[3], parts[4]
        if not local_address.endswith(suffix) or state.upper() != "LISTENING":
            continue
        try:
            return int(pid_text)
        except ValueError:
            continue
    return None


def _windows_process_command_line(pid: int) -> str:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return ""
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\"; "
        "if ($p) { [Console]::Out.Write($p.CommandLine) }"
    )
    try:
        return subprocess.check_output(
            [powershell, "-NoProfile", "-Command", script],
            text=True,
            errors="ignore",
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _restart_existing_personaldm_backend() -> bool:
    """Stop a stale launcher-owned PersonalDM backend without touching unrelated services."""
    if os.name != "nt" or not _url_ready(f"{BACKEND_URL}/health"):
        return False

    pid = _windows_listener_pid(8000)
    if not pid:
        return False
    command_line = _windows_process_command_line(pid).casefold()
    if "uvicorn" not in command_line or "app.main:app" not in command_line:
        return False

    print(f"[GUI] Перезапускаю предыдущий PersonalDM backend (PID {pid}) для применения текущего кода и .env...")
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0 and _wait_stopped(f"{BACKEND_URL}/health")


def run_gui() -> int:
    if not _ensure_frontend_dependencies():
        return 1

    backend: subprocess.Popen | None = None
    frontend: subprocess.Popen | None = None
    try:
        if _url_ready(f"{BACKEND_URL}/health"):
            restarted = _restart_existing_personaldm_backend()
            if not restarted and _url_ready(f"{BACKEND_URL}/health"):
                print(
                    "[GUI] На :8000 уже работает backend, который launcher не может безопасно перезапустить. "
                    "Использую его как есть."
                )

        if not _url_ready(f"{BACKEND_URL}/health"):
            print("[GUI] Запускаю FastAPI backend...")
            backend = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
                cwd=BACKEND_DIR,
            )
            if not _wait_ready(f"{BACKEND_URL}/health", backend):
                print("[Ошибка] Backend не поднялся на http://127.0.0.1:8000.")
                return 1
        else:
            print("[GUI] Backend уже запущен на :8000 — использую его.")

        if _url_ready(FRONTEND_URL):
            print("[GUI] Vite уже запущен на :5173 — использую его.")
        else:
            print("[GUI] Запускаю React/Vite frontend...")
            frontend = subprocess.Popen(
                _npm_command("run", "dev", "--", "--host", "127.0.0.1"),
                cwd=FRONTEND_DIR,
            )
            if not _wait_ready(FRONTEND_URL, frontend):
                print("[Ошибка] Frontend не поднялся на http://127.0.0.1:5173.")
                return 1

        print(f"\n[GUI] PersonalDM готов: {FRONTEND_URL}")
        print("[GUI] Браузер откроется автоматически. Ctrl+C — остановить GUI и вернуться в меню.\n")
        webbrowser.open(FRONTEND_URL)

        while True:
            if backend is not None and backend.poll() is not None:
                print("[Ошибка] Backend неожиданно завершился.")
                return backend.returncode or 1
            if frontend is not None and frontend.poll() is not None:
                print("[Ошибка] Frontend неожиданно завершился.")
                return frontend.returncode or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[GUI] Останавливаю локальные сервисы...")
        return 0
    finally:
        _terminate_tree(frontend)
        _terminate_tree(backend)


def run_cli() -> int:
    return subprocess.call([sys.executable, "cli_tui.py"], cwd=BACKEND_DIR)


def provider_choice() -> int:
    selected = select_menu(
        "LLM не настроена. Как продолжить?",
        [
            ("Локальный Ollama + Gemma 4", "local"),
            ("Облачный OpenAI-compatible provider", "cloud"),
            ("Выйти", "exit"),
        ],
    )
    return {"local": 10, "cloud": 20, "exit": 30, None: 30}[selected]


def launcher_menu() -> int:
    while True:
        selected = select_menu(
            "Как запустить PersonalDM?",
            [
                ("GUI — открыть веб-интерфейс", "gui"),
                ("CLI — играть в терминале", "cli"),
                ("Выйти", "exit"),
            ],
        )
        if selected in {None, "exit"}:
            return 0
        if selected == "gui":
            run_gui()
        elif selected == "cli":
            run_cli()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--provider-choice", action="store_true")
    args, _ = parser.parse_known_args()
    if args.provider_choice:
        return provider_choice()
    return launcher_menu()


if __name__ == "__main__":
    raise SystemExit(main())
