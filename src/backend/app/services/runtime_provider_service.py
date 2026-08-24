from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import httpx

from app.config import settings


class RuntimeProviderError(RuntimeError):
    pass


class RuntimeProviderService:
    """Single source of truth for text/image runtime selection, checks and local setup.

    This service is intentionally usable from FastAPI, CLI and launcher.py. The durable
    choice lives in backend/.env; campaign text configs may still override the global
    defaults, but first-run and local infrastructure are managed here.
    """

    BACKEND_DIR = Path(__file__).resolve().parents[2]
    ROOT_DIR = Path(__file__).resolve().parents[4]
    ENV_FILE = BACKEND_DIR / ".env"
    TOOLS_DIR = ROOT_DIR / "tools"
    COMFY_ROOT = TOOLS_DIR / "comfy"
    COMFY_DIR = COMFY_ROOT / "ComfyUI"
    COMFY_ENV = TOOLS_DIR / "comfy-runtime"
    COMFY_READY = COMFY_ENV / ".personaldm-ready"

    TEXT_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
    TEXT_LOCAL_MODEL = "gemma4:e4b"
    IMAGE_LOCAL_BASE_URL = "http://127.0.0.1:8188"
    IMAGE_CLOUD_BASE_URL = "https://api.openai.com/v1"
    IMAGE_CLOUD_MODEL = "gpt-image-2"

    IMAGE_MODEL_FILES = (
        (
            "diffusion_models/flux-2-klein-4b-fp8.safetensors",
            "https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/resolve/main/flux-2-klein-4b-fp8.safetensors",
        ),
        (
            "text_encoders/qwen_3_4b_fp4_flux2.safetensors",
            "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors",
        ),
        (
            "vae/flux2-vae.safetensors",
            "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/split_files/vae/flux2-vae.safetensors",
        ),
        (
            "loras/pixel-art-lora.safetensors",
            "https://huggingface.co/Limbicnation/pixel-art-lora/resolve/main/pytorch_lora_weights.comfyui.safetensors",
        ),
    )

    def read_env(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self.ENV_FILE.exists():
            return result
        for raw in self.ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _write_env(self, updates: dict[str, str | None]) -> None:
        self.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = self.ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if self.ENV_FILE.exists() else []
        keys = set(updates)
        kept = [line for line in existing if not any(line.startswith(f"{key}=") for key in keys)]
        for key, value in updates.items():
            if value is not None:
                kept.append(f"{key}={value}")
        temporary = self.ENV_FILE.with_suffix(".env.tmp")
        temporary.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        temporary.replace(self.ENV_FILE)
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._apply_runtime_settings(updates)

    @staticmethod
    def _apply_runtime_settings(updates: dict[str, str | None]) -> None:
        mapping = {
            "PDM_TEXT_PROVIDER": "TEXT_PROVIDER",
            "PDM_LLM_BASE_URL": "LLM_BASE_URL",
            "PDM_LLM_MODEL": "LLM_MODEL",
            "PDM_LLM_API_KEY": "LLM_API_KEY",
            "PDM_LLM_CONTEXT_WINDOW": "LLM_CONTEXT_WINDOW",
            "PDM_IMAGE_PROVIDER": "IMAGE_PROVIDER",
            "PDM_IMAGE_ENABLED": "IMAGE_ENABLED",
            "PDM_IMAGE_BASE_URL": "IMAGE_BASE_URL",
            "PDM_IMAGE_CLOUD_BASE_URL": "IMAGE_CLOUD_BASE_URL",
            "PDM_IMAGE_CLOUD_MODEL": "IMAGE_CLOUD_MODEL",
            "PDM_IMAGE_API_KEY": "IMAGE_API_KEY",
        }
        for env_key, attr in mapping.items():
            if env_key not in updates:
                continue
            value = updates[env_key]
            if attr.endswith("_ENABLED"):
                parsed = str(value).casefold() in {"1", "true", "yes", "on"}
            elif attr.endswith("CONTEXT_WINDOW"):
                parsed = int(value or 4096)
            else:
                parsed = value
            setattr(settings, attr, parsed)

    def text_mode(self) -> str:
        env = self.read_env()
        explicit = env.get("PDM_TEXT_PROVIDER")
        if explicit in {"local", "cloud"}:
            return explicit
        return "cloud" if env.get("PDM_LLM_API_KEY") else "local"

    def image_mode(self) -> str:
        env = self.read_env()
        explicit = env.get("PDM_IMAGE_PROVIDER")
        if explicit in {"local", "cloud", "off"}:
            return explicit
        if env.get("PDM_IMAGE_ENABLED", "").casefold() in {"1", "true", "yes", "on"}:
            return "local"
        return "off"

    def configure_text(
        self,
        mode: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        context_window: int | None = None,
    ) -> dict:
        if mode not in {"local", "cloud"}:
            raise ValueError("Text provider must be local or cloud")
        if mode == "local":
            base_url = self.TEXT_LOCAL_BASE_URL
            model = model or self.TEXT_LOCAL_MODEL
            api_key = ""
            context_window = context_window or 4096
        else:
            base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
            model = model or "gpt-5.6-luna"
            if not api_key and not self.read_env().get("PDM_LLM_API_KEY"):
                raise ValueError("Cloud text provider requires an API key")
            api_key = api_key if api_key is not None else self.read_env().get("PDM_LLM_API_KEY", "")
            context_window = context_window or 128000
        self._write_env(
            {
                "PDM_TEXT_PROVIDER": mode,
                "PDM_LLM_BASE_URL": base_url,
                "PDM_LLM_MODEL": model,
                "PDM_LLM_API_KEY": api_key,
                "PDM_LLM_CONTEXT_WINDOW": str(context_window),
                "PDM_CONTROL_LLM_MODEL": model,
            }
        )
        return self.profile()["text"]

    def configure_image(
        self,
        mode: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        if mode not in {"local", "cloud", "off"}:
            raise ValueError("Image provider must be local, cloud or off")
        updates: dict[str, str | None] = {
            "PDM_IMAGE_PROVIDER": mode,
            "PDM_IMAGE_ENABLED": "false" if mode == "off" else "true",
        }
        if mode == "local":
            updates["PDM_IMAGE_BASE_URL"] = self.IMAGE_LOCAL_BASE_URL
        elif mode == "cloud":
            env = self.read_env()
            key = api_key if api_key is not None else env.get("PDM_IMAGE_API_KEY", "")
            if not key:
                raise ValueError("Cloud image provider requires an API key")
            updates.update(
                {
                    "PDM_IMAGE_CLOUD_BASE_URL": (base_url or self.IMAGE_CLOUD_BASE_URL).rstrip("/"),
                    "PDM_IMAGE_CLOUD_MODEL": model or self.IMAGE_CLOUD_MODEL,
                    "PDM_IMAGE_API_KEY": key,
                }
            )
        self._write_env(updates)
        return self.profile()["image"]

    def profile(self) -> dict:
        env = self.read_env()
        text_mode = self.text_mode()
        image_mode = self.image_mode()
        return {
            "text": {
                "mode": text_mode,
                "base_url": env.get("PDM_LLM_BASE_URL", self.TEXT_LOCAL_BASE_URL),
                "model": env.get("PDM_LLM_MODEL", self.TEXT_LOCAL_MODEL),
                "context_window": int(env.get("PDM_LLM_CONTEXT_WINDOW", "4096") or 4096),
                "has_api_key": bool(env.get("PDM_LLM_API_KEY")),
                "status": self.check_text(),
            },
            "image": {
                "mode": image_mode,
                "base_url": (
                    env.get("PDM_IMAGE_CLOUD_BASE_URL", self.IMAGE_CLOUD_BASE_URL)
                    if image_mode == "cloud"
                    else env.get("PDM_IMAGE_BASE_URL", self.IMAGE_LOCAL_BASE_URL)
                ),
                "model": (
                    env.get("PDM_IMAGE_CLOUD_MODEL", self.IMAGE_CLOUD_MODEL)
                    if image_mode == "cloud"
                    else "FLUX.2 Klein 4B FP8"
                ),
                "has_api_key": bool(env.get("PDM_IMAGE_API_KEY")),
                "status": self.check_image(),
            },
        }

    def check_text(self) -> dict:
        mode = self.text_mode()
        env = self.read_env()
        if mode == "cloud":
            base_url = env.get("PDM_LLM_BASE_URL", "").rstrip("/")
            key = env.get("PDM_LLM_API_KEY", "")
            if not base_url or not key:
                return self._status(False, "configuration_missing", "Нужны Base URL и API key")
            return self._cloud_health(base_url, key)

        ollama = self.find_ollama()
        if not ollama:
            return self._status(False, "not_installed", "Ollama не установлен", installable=True)
        try:
            response = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
            if not response.is_success:
                return self._status(False, "service_offline", "Ollama установлен, но сервис не отвечает", installable=True)
            names = {str(item.get("name")) for item in response.json().get("models", [])}
            model = env.get("PDM_LLM_MODEL", self.TEXT_LOCAL_MODEL)
            if model not in names and not any(name.startswith(model + ":") for name in names):
                return self._status(False, "model_missing", f"Модель {model} не скачана", installable=True)
            return self._status(True, "ready", f"Ollama готов: {model}")
        except (httpx.HTTPError, ValueError, TypeError):
            return self._status(False, "service_offline", "Ollama установлен, но сервис не отвечает", installable=True)

    def check_image(self) -> dict:
        mode = self.image_mode()
        env = self.read_env()
        if mode == "off":
            return self._status(True, "disabled", "Генерация изображений выключена")
        if mode == "cloud":
            base_url = env.get("PDM_IMAGE_CLOUD_BASE_URL", self.IMAGE_CLOUD_BASE_URL).rstrip("/")
            key = env.get("PDM_IMAGE_API_KEY", "")
            if not key:
                return self._status(False, "configuration_missing", "Нужен API key", installable=False)
            return self._cloud_health(base_url, key)

        if not (self.COMFY_DIR / "main.py").is_file():
            return self._status(False, "not_installed", "ComfyUI не установлен", installable=True)
        missing = [rel for rel, _ in self.IMAGE_MODEL_FILES if not (self.COMFY_DIR / "models" / rel).is_file()]
        if missing:
            return self._status(False, "model_missing", f"Не хватает файлов моделей: {len(missing)}", installable=True)
        try:
            response = httpx.get(f"{self.IMAGE_LOCAL_BASE_URL}/system_stats", timeout=2.0)
            if response.is_success:
                return self._status(True, "ready", "ComfyUI готов")
        except httpx.HTTPError:
            pass
        return self._status(False, "service_offline", "ComfyUI установлен, но не запущен", installable=True)

    @staticmethod
    def _status(ready: bool, code: str, message: str, installable: bool = False) -> dict:
        return {"ready": ready, "code": code, "message": message, "installable": installable}

    @staticmethod
    def _cloud_health(base_url: str, api_key: str) -> dict:
        try:
            response = httpx.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
            )
            if response.is_success:
                return RuntimeProviderService._status(True, "ready", "Облачный API доступен")
            return RuntimeProviderService._status(False, "auth_or_endpoint_error", f"Cloud API ответил HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            return RuntimeProviderService._status(False, "service_offline", f"Cloud API недоступен: {exc}")

    def find_ollama(self) -> str | None:
        found = shutil.which("ollama")
        if found:
            return found
        candidates = []
        local = os.environ.get("LOCALAPPDATA")
        program = os.environ.get("PROGRAMFILES")
        if local:
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        if program:
            candidates.append(Path(program) / "Ollama" / "ollama.exe")
        return next((str(path) for path in candidates if path.is_file()), None)

    def ensure_local_text(self) -> dict:
        ollama = self.find_ollama()
        if not ollama:
            if os.name != "nt" or not shutil.which("winget"):
                raise RuntimeProviderError("Ollama не установлен; автоматическая установка сейчас поддерживается через winget на Windows")
            self._run(
                [
                    "winget", "install", "--id", "Ollama.Ollama", "-e", "--silent",
                    "--accept-package-agreements", "--accept-source-agreements",
                ],
                "Не удалось установить Ollama через winget",
            )
            ollama = self.find_ollama()
            if not ollama:
                raise RuntimeProviderError("Ollama установщик завершился, но ollama.exe не найден")

        if not self._url_ok("http://127.0.0.1:11434/api/tags"):
            kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen([ollama, "serve"], **kwargs)
            if not self._wait_url("http://127.0.0.1:11434/api/tags", 30):
                raise RuntimeProviderError("Ollama установлен, но сервис не запустился")

        model = self.read_env().get("PDM_LLM_MODEL", self.TEXT_LOCAL_MODEL)
        self._run([ollama, "pull", model], f"Не удалось скачать модель {model}")
        return self.check_text()

    def ensure_local_image(self) -> dict:
        self.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        self.COMFY_ROOT.mkdir(parents=True, exist_ok=True)
        if not (self.COMFY_DIR / "main.py").is_file():
            self._install_comfy_source()

        python_exe = self.COMFY_ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python_exe.is_file():
            self._run([sys.executable, "-m", "venv", str(self.COMFY_ENV)], "Не удалось создать ComfyUI venv")
        if not self.COMFY_READY.is_file():
            self._run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], "Не удалось обновить pip для ComfyUI")
            torch_index = "https://download.pytorch.org/whl/cu130" if os.name == "nt" else "https://download.pytorch.org/whl/cu128"
            self._run(
                [str(python_exe), "-m", "pip", "install", "--upgrade", "torch", "torchvision", "torchaudio", "--index-url", torch_index],
                "Не удалось установить PyTorch для ComfyUI",
            )
            self._run(
                [str(python_exe), "-m", "pip", "install", "-r", str(self.COMFY_DIR / "requirements.txt")],
                "Не удалось установить зависимости ComfyUI",
            )
            self._run(
                [str(python_exe), "-c", "import torch; import server; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))"],
                "ComfyUI runtime установлен, но CUDA недоступна",
                cwd=self.COMFY_DIR,
            )
            self.COMFY_READY.write_text("ready\n", encoding="utf-8")

        models = self.COMFY_DIR / "models"
        for relative, url in self.IMAGE_MODEL_FILES:
            target = models / relative
            if not target.is_file():
                self._download(url, target)

        if not self._url_ok(f"{self.IMAGE_LOCAL_BASE_URL}/system_stats"):
            kwargs: dict = {"cwd": self.COMFY_DIR, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                [str(python_exe), "main.py", "--lowvram", "--disable-auto-launch", "--port", "8188"],
                **kwargs,
            )
            if not self._wait_url(f"{self.IMAGE_LOCAL_BASE_URL}/system_stats", 90):
                raise RuntimeProviderError("ComfyUI установлен, но не поднялся на :8188")
        return self.check_image()

    def ensure_selected_local_providers(self) -> dict:
        result: dict[str, dict] = {}
        if self.text_mode() == "local":
            result["text"] = self.ensure_local_text()
        else:
            result["text"] = self.check_text()
        if self.image_mode() == "local":
            result["image"] = self.ensure_local_image()
        else:
            result["image"] = self.check_image()
        return result

    def _install_comfy_source(self) -> None:
        if self.COMFY_DIR.exists():
            shutil.rmtree(self.COMFY_DIR, ignore_errors=True)
        git = shutil.which("git")
        if git:
            try:
                subprocess.run(
                    [git, "clone", "--depth", "1", "https://github.com/Comfy-Org/ComfyUI.git", str(self.COMFY_DIR)],
                    check=True,
                )
            except subprocess.CalledProcessError:
                shutil.rmtree(self.COMFY_DIR, ignore_errors=True)
        if (self.COMFY_DIR / "main.py").is_file():
            return

        archive = self.TOOLS_DIR / "comfyui-master.zip"
        unpack = self.TOOLS_DIR / ".comfyui-unpack"
        self._download("https://github.com/Comfy-Org/ComfyUI/archive/refs/heads/master.zip", archive)
        shutil.rmtree(unpack, ignore_errors=True)
        unpack.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zip_file:
            zip_file.extractall(unpack)
        source = unpack / "ComfyUI-master"
        if not (source / "main.py").is_file():
            raise RuntimeProviderError("Архив ComfyUI скачан, но main.py не найден")
        shutil.move(str(source), str(self.COMFY_DIR))
        archive.unlink(missing_ok=True)
        shutil.rmtree(unpack, ignore_errors=True)

    @staticmethod
    def _run(command: list[str], error: str, cwd: Path | None = None) -> None:
        try:
            subprocess.run(command, cwd=cwd, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeProviderError(f"{error}: {exc}") from exc

    @staticmethod
    def _download(url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=None) as response:
                response.raise_for_status()
                with part.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
            part.replace(target)
        except (httpx.HTTPError, OSError) as exc:
            raise RuntimeProviderError(f"Не удалось скачать {target.name}: {exc}") from exc

    @staticmethod
    def _url_ok(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            return False

    @classmethod
    def _wait_url(cls, url: str, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls._url_ok(url):
                return True
            time.sleep(1)
        return False


__all__ = ["RuntimeProviderError", "RuntimeProviderService"]
