from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.models.turn import ChatMessage
from app.services.context_compiler import ContextCompiler
from app.services.meta_command_router import sanitize_meta_output
from app.services.playtest_trace import _quality_classification


def test_runtime_manifest_is_available_as_read_only_debug_endpoint(client: TestClient) -> None:
    response = client.get("/api/debugger/runtime")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "runtime_manifest"
    assert payload["installed"] is True
    assert payload["turn_pipeline"]
    assert payload["narration_pipeline"]
    assert payload["guards"]
    assert "build_commit" in payload


def test_campaign_debugger_exposes_publication_trace_ui(client: TestClient) -> None:
    response = client.get("/api/debugger")

    assert response.status_code == 200
    assert "Turn publication trace" in response.text
    assert "RAW / published" in response.text
    assert "/api/debugger/runtime" in response.text


def test_context_token_breakdown_uses_stable_observability_buckets() -> None:
    messages = [
        ChatMessage(
            role="system",
            content=(
                "System policy\n"
                "[Current Scene: Office]\nRoom description\n"
                "[Campaign Facts & History]\n- door state open\n"
                "[Recent Scene Texture — transient, non-canon]\n- rain on glass\n"
            ),
        ),
        ChatMessage(role="assistant", content="Earlier reply."),
        ChatMessage(role="user", content="Я проверяю дверь."),
    ]

    audited = ContextCompiler._audit_token_breakdown(
        messages,
        {"token_budget_max": 4096, "token_budget_used": 100},
        "Я проверяю дверь.",
    )
    breakdown = audited["token_budget_breakdown"]

    assert breakdown["system"] > 0
    assert breakdown["scene"] > 0
    assert breakdown["memory"] > 0
    assert breakdown["history"] > 0
    assert breakdown["input"] > 0
    assert breakdown["total_prompt_estimate"] == sum(
        breakdown[key] for key in ("system", "scene", "memory", "history", "input")
    )


def test_playtest_quality_classification_distinguishes_raw_from_publication() -> None:
    audit = {
        "draft_text": "Хорошая большая сцена, но здесь есть нарушение.",
        "final_text": "Хорошая большая сцена.",
        "violation_count": 1,
        "attempts": [{"status": "failed"}],
    }

    quality = _quality_classification(audit, "repaired", "Хорошая большая сцена.", [])

    assert quality["class"] == "RAW BAD/PUBLISHED GOOD"
    assert quality["repair_preservation_ratio"] is not None


def test_meta_output_sanitizer_blocks_internal_prompt_markers() -> None:
    leaked = "Вот внутренний блок: <campaign_snapshot>secret</campaign_snapshot>"

    published, audit = sanitize_meta_output(leaked)

    assert audit["applied"] is True
    assert audit["reason"] == "internal_prompt_marker"
    assert "campaign_snapshot" not in published
    assert "secret" not in published
    assert "внутренние служебные инструкции" in published


def test_meta_output_sanitizer_preserves_normal_dm_explanation() -> None:
    answer = "Трактирщик не должен быть здесь: это ошибка пространственной непрерывности."

    published, audit = sanitize_meta_output(answer)

    assert published == answer
    assert audit == {"applied": False, "reason": None}


def test_primary_current_state_docs_are_present_and_mapped() -> None:
    backend = Path(__file__).resolve().parents[1]
    docs = backend.parents[1] / "docs"
    readme = (docs / "README.md").read_text(encoding="utf-8")
    required = (
        "session-zero.md",
        "scene-location-presence.md",
        "npc-identity-and-materialization.md",
        "meta-channel.md",
        "playtest-protocol.md",
        "visual-generation.md",
        "configuration-reference.md",
        "persistence-recovery.md",
    )

    for filename in required:
        path = docs / filename
        assert path.is_file(), filename
        content = path.read_text(encoding="utf-8")
        assert "Статус:" in content
        assert "Failure" in content or "failure" in content
        assert filename in readme
