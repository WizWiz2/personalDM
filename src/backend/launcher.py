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
from app.services.runtime_provider_service import RuntimeProviderError, RuntimeProviderService


BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parents[1]
FRONTEND_DIR = ROOT_DIR / "src" / "frontend"
BACKEND_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://127.0.0.1:5173"


def _hidden_process_kwargs() -> dict:
    """Return Windows subprocess options that keep service consoles invisible."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        "startupinfo": startupinfo,
    }


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
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
                cwd=BACKEND_DIR,
                **_hidden_process_kwargs(),
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
                **_hidden_process_kwargs(),
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
    process = subprocess.Popen([sys.executable, "cli_tui.py"], cwd=BACKEND_DIR)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # Ctrl+C is otherwise delivered to the launcher while the child CLI can survive and
        # leave post-turn work orphaned. Reuse the same scoped tree cleanup as the GUI runner.
        _terminate_tree(process)
        return 130


def _ask_text_provider(service: RuntimeProviderService) -> bool:
    selected = select_menu(
        "Как запускать текстовую модель?",
        [
            ("Локально — Ollama + Gemma 4", "local"),
            ("Облачно — OpenAI-compatible API", "cloud"),
            ("Выйти", "exit"),
        ],
    )
    if selected in {None, "exit"}:
        return False
    if selected == "local":
        service.configure_text("local")
        return True

    print("\nОблачные настройки сохраняются только в src/backend/.env (файл игнорируется Git).")
    base_url = input("Base URL [https://api.openai.com/v1]: ").strip() or "https://api.openai.com/v1"
    model = input("Модель [gpt-4.1-mini]: ").strip() or "gpt-4.1-mini"
    api_key = input("API key: ").strip()
    if not api_key:
        print("[Ошибка] Для облачной текстовой модели нужен API key.")
        return _ask_text_provider(service)
    context_raw = input("Контекст [128000]: ").strip()
    context_window = int(context_raw) if context_raw.isdigit() else 128000
    service.configure_text(
        "cloud",
        base_url=base_url,
        model=model,
        api_key=api_key,
        context_window=context_window,
    )
    return True


def _ask_image_provider(service: RuntimeProviderService) -> bool:
    selected = select_menu(
        "Как генерировать иллюстрации?",
        [
            ("Локально — ComfyUI + FLUX.2 Klein", "local"),
            ("Облачно — Images API", "cloud"),
            ("Не использовать генерацию изображений", "off"),
            ("Выйти", "exit"),
        ],
    )
    if selected in {None, "exit"}:
        return False
    if selected in {"local", "off"}:
        service.configure_image(str(selected))
        return True

    print("\nКлюч графического API хранится отдельно от ключа текстовой модели.")
    base_url = input("Images API Base URL [https://api.openai.com/v1]: ").strip() or "https://api.openai.com/v1"
    model = input("Image model [gpt-image-2]: ").strip() or "gpt-image-2"
    api_key = input("Image API key: ").strip()
    if not api_key:
        print("[Ошибка] Для облачной генерации изображений нужен API key.")
        return _ask_image_provider(service)
    service.configure_image("cloud", base_url=base_url, model=model, api_key=api_key)
    return True


def bootstrap_providers() -> int:
    """First-run wizard plus cheap health/repair pass on every play.bat launch."""
    service = RuntimeProviderService()
    env = service.read_env()

    if "PDM_TEXT_PROVIDER" not in env:
        if not _ask_text_provider(service):
            return 1
    if "PDM_IMAGE_PROVIDER" not in service.read_env():
        if not _ask_image_provider(service):
            return 1

    profile = service.profile()
    for kind, item in (("text", profile["text"]), ("image", profile["image"])):
        status = item["status"]
        print(f"[Setup] {kind}: {item['mode']} — {status['message']}")
        if item["mode"] != "local" or status["ready"]:
            continue
        print(f"[Setup] Доустанавливаю/запускаю локальный {kind} provider...")
        try:
            result = service.ensure_local_text() if kind == "text" else service.ensure_local_image()
            print(f"[Setup] {kind}: {result['message']}")
        except RuntimeProviderError as exc:
            print(f"[WARNING] {kind} provider не удалось подготовить: {exc}")
            if kind == "text":
                print("[WARNING] GUI/CLI всё равно запустится — провайдер можно сменить в настройках.")
    return 0


def migrate_user_data() -> int:
    """Move the old repository-local data directory to the per-user game library."""
    service = RuntimeProviderService()
    env = service.read_env()
    configured = env.get("PDM_DATA_DIR", "").replace("/", "\\").rstrip("\\")
    legacy = (BACKEND_DIR / "data").resolve()
    target = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "PersonalDM" / "library"
    if configured.casefold() not in {".\\data", "data", "./data"} or not legacy.is_dir() or legacy.resolve() == target.resolve():
        return 0
    if target.exists() and any(target.iterdir()):
        print(f"[Storage] Новая библиотека уже существует: {target}. Старую папку не трогаю.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    # The backend imports settings before this command and may already have created
    # the empty target directory. Move its contents, never nest `data` inside it.
    for item in legacy.iterdir():
        destination = target / item.name
        if destination.exists():
            raise RuntimeError(f"Не могу перенести {item}: уже существует {destination}")
        shutil.move(str(item), str(destination))
    legacy.rmdir()
    service._write_env({"PDM_DATA_DIR": str(target), "PDM_DATABASE_URL": f"sqlite+aiosqlite:///{target / 'campaign.db'}"})
    print(f"[Storage] Игровая база перенесена в {target}. Сохранения сохранены.")
    return 0


def uninstall_menu() -> int:
    """Run the transparent Russian uninstall menu using the same UI as play.bat."""
    selected = select_menu(
        "Что удалить?",
        [
            ("Инфраструктура — ComfyUI, runtime и зависимости; сохранения остаются", "infrastructure"),
            ("База игр — кэш и картинки; campaign.db и сохранения остаются", "games"),
            ("Всё приложение — приложение и инфраструктура; сохранения остаются", "all"),
            ("Отмена", "exit"),
        ],
    )
    if selected in {None, "exit"}:
        return 0
    print("\nРежим удаления: " + selected)
    print("Для подтверждения введите DELETE. Сохранения не удаляются.\n")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        print("[Ошибка] PowerShell не найден.")
        return 1
    script = ROOT_DIR / "uninstall.ps1"
    return subprocess.call(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Mode", selected],
        cwd=ROOT_DIR,
    )


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
    parser.add_argument("--bootstrap-providers", action="store_true")
    parser.add_argument("--migrate-user-data", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args, _ = parser.parse_known_args()
    if args.bootstrap_providers:
        return bootstrap_providers()
    if args.migrate_user_data:
        return migrate_user_data()
    if args.uninstall:
        return uninstall_menu()
    return launcher_menu()


if __name__ == "__main__":
    raise SystemExit(main())
