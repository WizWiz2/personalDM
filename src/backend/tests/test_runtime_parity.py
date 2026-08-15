from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import GameApplication
from app.db.tables import Campaign, Scene
from app.models.campaign import CampaignCreate
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProvider
from app.runtime import install_runtime
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.base_context_compiler import ContextCompiler as BaseContextCompiler
from app.services.base_turn_runner import TurnRunner as BaseTurnRunner
from app.services.campaign_service import CampaignService
from app.services.context_compiler import ContextCompiler
from app.services.turn_runner import TurnRunner
from app.services.turn_saga import TurnSaga

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _cold_manifest(import_target: str) -> dict:
    script = (
        f"import {import_target}\n"
        "import json\n"
        "from app.runtime import runtime_manifest\n"
        "print('RUNTIME_MANIFEST=' + json.dumps(runtime_manifest(), sort_keys=True))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    line = next(
        item
        for item in result.stdout.splitlines()
        if item.startswith("RUNTIME_MANIFEST=")
    )
    return json.loads(line.split("=", 1)[1])


def test_cold_cli_and_fastapi_install_identical_runtime() -> None:
    cli_manifest = _cold_manifest("cli")
    api_manifest = _cold_manifest("app.main")

    assert cli_manifest == api_manifest
    assert cli_manifest["installed"] is True
    assert cli_manifest["guards"] == [
        "memory_scribe",
        "session_zero_finalize",
        "thesis_lifecycle",
    ]
    assert cli_manifest["context_pipeline"] == [
        "authoritative_scene_state",
        "recent_narrative_details",
    ]
    assert cli_manifest["turn_pipeline"] == [
        "compile_context",
        "plan_authority",
        "execute_structured_boundary",
        "build_turn_authority",
        "render_narration",
        "validate_authority",
        "materialize_structured_outcome",
        "commit",
        "enqueue_post_turn",
    ]
    assert cli_manifest["narration_pipeline"] == [
        "generate_draft",
        "validate_authority",
        "repair_once",
        "publish_accepted",
    ]
    assert cli_manifest["turn_stream"].endswith("TurnRunner.run_turn_stream")
    assert cli_manifest["turn_saga"].endswith("TurnSaga.run_turn_stream")
    assert cli_manifest["provider_stream"].endswith("LLMProvider.generate_stream")
    assert cli_manifest["narration_pipeline_impl"].endswith(
        "AuthorityNarrationPipeline.generate"
    )
    assert cli_manifest["authority_planner"].endswith("TurnAuthorityPlanner.plan")
    assert cli_manifest["authority_validator"].endswith(
        "TurnAuthorityValidator.validate"
    )
    assert cli_manifest["context_compiler"].endswith(
        "ContextCompiler.compile_context"
    )
    assert cli_manifest["memory_parser"].endswith("guarded_parse_data")
    assert cli_manifest["thesis_reconcile"].endswith(
        "_reconcile_with_lifecycle"
    )
    assert cli_manifest["post_turn_mode"] == "background"


def test_context_pipeline_is_explicit_and_not_runtime_patched() -> None:
    base_method = BaseContextCompiler.compile_context
    install_runtime()

    assert BaseContextCompiler.compile_context is base_method
    assert ContextCompiler.__mro__[1] is BaseContextCompiler
    assert ContextCompiler.DEFAULT_PROVIDER_NAMES == (
        "authoritative_scene_state",
        "recent_narrative_details",
    )
    assert ContextCompiler.compile_context.__module__ == (
        "app.services.context_compiler"
    )


def test_turn_saga_and_authority_pipeline_are_explicit() -> None:
    raw_provider_method = LLMProvider.generate_stream
    legacy_turn_method = BaseTurnRunner.run_turn_stream
    install_runtime()

    assert LLMProvider.generate_stream is raw_provider_method
    assert BaseTurnRunner.run_turn_stream is legacy_turn_method
    assert TurnRunner.__mro__[1] is TurnSaga
    assert TurnSaga.__mro__[1] is BaseTurnRunner
    assert TurnRunner.run_turn_stream.__module__ == "app.services.turn_runner"
    assert AuthorityNarrationPipeline.generate.__module__ == (
        "app.services.authority_narration_pipeline"
    )


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_cli_is_an_adapter_not_a_parallel_game_engine() -> None:
    modules = _imported_modules(BACKEND_ROOT / "cli.py")
    assert not any(module.startswith("app.db.repositories") for module in modules)
    assert {
        "app.services.turn_runner",
        "app.services.meta_command_router",
        "app.services.turn_undo_service",
        "app.services.post_turn_processor",
        "app.services.scene_lifecycle",
    }.isdisjoint(modules)
    assert "app.application" in modules
    assert "app.runtime" in modules


def test_fastapi_turn_adapter_uses_same_application_boundary() -> None:
    modules = _imported_modules(BACKEND_ROOT / "app" / "api" / "turns.py")
    assert "app.application" in modules
    assert {
        "app.services.turn_runner",
        "app.services.meta_command_router",
        "app.services.turn_undo_service",
        "app.services.session_zero_service",
    }.isdisjoint(modules)


@pytest.mark.asyncio
async def test_meta_command_routes_before_session_zero(
    db_session: AsyncSession,
) -> None:
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Runtime parity meta")
    )
    await db_session.commit()

    route = await GameApplication(db_session).route_input(
        campaign.id,
        TurnCreate(role="user", content="/DM Где сейчас герой?"),
    )

    assert route.channel == "meta"


@pytest.mark.asyncio
async def test_manual_scene_creation_uses_authoritative_lifecycle(
    db_session: AsyncSession,
) -> None:
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Runtime parity lifecycle")
    )
    await db_session.commit()
    application = GameApplication(db_session)

    first = await application.create_and_activate_scene(
        campaign.id,
        location_name="Первая площадь",
        description="Каменная площадь",
        mood="спокойно",
    )
    second = await application.create_and_activate_scene(
        campaign.id,
        location_name="Вторая площадь",
        description="Рынок у ворот",
        mood="оживлённо",
    )

    campaign_row = await db_session.get(Campaign, str(campaign.id))
    first_row = await db_session.get(Scene, str(first.id))
    second_row = await db_session.get(Scene, str(second.id))
    active_count = (
        await db_session.execute(
            select(Scene).where(
                Scene.campaign_id == str(campaign.id),
                Scene.status == "active",
            )
        )
    ).scalars().all()

    assert campaign_row.current_scene_id == str(second.id)
    assert first_row.status == "completed"
    assert second_row.status == "active"
    assert len(active_count) == 1
