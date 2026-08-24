from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services.runtime_provider_service import RuntimeProviderService
from app.services.visual_generation import VisualGenerationService
from app.services.visual_provider_factory import (
    CloudVisualGenerationService,
    create_visual_generation_service,
)


@pytest.fixture
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    tools = tmp_path / "tools"
    monkeypatch.setattr(RuntimeProviderService, "ENV_FILE", env_file)
    monkeypatch.setattr(RuntimeProviderService, "TOOLS_DIR", tools)
    monkeypatch.setattr(RuntimeProviderService, "COMFY_ROOT", tools / "comfy")
    monkeypatch.setattr(RuntimeProviderService, "COMFY_DIR", tools / "comfy" / "ComfyUI")
    monkeypatch.setattr(RuntimeProviderService, "COMFY_ENV", tools / "comfy-runtime")
    monkeypatch.setattr(RuntimeProviderService, "COMFY_READY", tools / "comfy-runtime" / ".personaldm-ready")
    monkeypatch.setattr(
        RuntimeProviderService,
        "_apply_runtime_settings",
        staticmethod(lambda updates: None),
    )
    return RuntimeProviderService(), env_file


def test_legacy_image_enabled_true_maps_to_local(isolated_runtime):
    service, env_file = isolated_runtime
    env_file.write_text("PDM_IMAGE_ENABLED=true\n", encoding="utf-8")

    assert service.image_mode() == "local"


def test_legacy_image_disabled_maps_to_off(isolated_runtime):
    service, env_file = isolated_runtime
    env_file.write_text("PDM_IMAGE_ENABLED=false\n", encoding="utf-8")

    assert service.image_mode() == "off"


def test_image_off_is_persisted_without_deleting_existing_cloud_settings(isolated_runtime):
    service, env_file = isolated_runtime
    env_file.write_text(
        "PDM_IMAGE_API_KEY=secret-image-key\n"
        "PDM_IMAGE_CLOUD_MODEL=gpt-image-2\n",
        encoding="utf-8",
    )

    profile = service.configure_image("off")
    values = service.read_env()

    assert profile["mode"] == "off"
    assert values["PDM_IMAGE_PROVIDER"] == "off"
    assert values["PDM_IMAGE_ENABLED"] == "false"
    assert values["PDM_IMAGE_API_KEY"] == "secret-image-key"
    assert service.check_image()["code"] == "disabled"


def test_cloud_image_requires_its_own_key(isolated_runtime):
    service, env_file = isolated_runtime
    env_file.write_text("PDM_LLM_API_KEY=text-only-key\n", encoding="utf-8")

    with pytest.raises(ValueError, match="image provider requires an API key"):
        service.configure_image("cloud")


def test_local_text_configuration_uses_ollama_defaults(isolated_runtime):
    service, _ = isolated_runtime

    profile = service.configure_text("local")
    values = service.read_env()

    assert profile["mode"] == "local"
    assert values["PDM_TEXT_PROVIDER"] == "local"
    assert values["PDM_LLM_BASE_URL"] == service.TEXT_LOCAL_BASE_URL
    assert values["PDM_LLM_MODEL"] == service.TEXT_LOCAL_MODEL
    assert values["PDM_LLM_API_KEY"] == ""


def test_explicit_provider_modes_override_legacy_flags(isolated_runtime):
    service, env_file = isolated_runtime
    env_file.write_text(
        "PDM_TEXT_PROVIDER=cloud\n"
        "PDM_LLM_API_KEY=x\n"
        "PDM_IMAGE_PROVIDER=off\n"
        "PDM_IMAGE_ENABLED=true\n",
        encoding="utf-8",
    )

    assert service.text_mode() == "cloud"
    assert service.image_mode() == "off"


def test_visual_factory_switches_between_local_and_cloud(monkeypatch: pytest.MonkeyPatch):
    class DummySession:
        pass

    original_provider = settings.IMAGE_PROVIDER
    try:
        settings.IMAGE_PROVIDER = "local"
        local = create_visual_generation_service(DummySession())
        assert isinstance(local, VisualGenerationService)
        assert not isinstance(local, CloudVisualGenerationService)

        settings.IMAGE_PROVIDER = "cloud"
        cloud = create_visual_generation_service(DummySession())
        assert isinstance(cloud, CloudVisualGenerationService)
    finally:
        settings.IMAGE_PROVIDER = original_provider
