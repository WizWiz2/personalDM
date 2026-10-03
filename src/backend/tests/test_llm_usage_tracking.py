from __future__ import annotations

from uuid import uuid4

import pytest

from app.db.repositories.llm_usage_repo import LLMUsageRepository
from app.db.tables import Campaign, Turn
from app.services.llm_pricing import estimate_openai_text_cost_usd
from app.services.llm_usage_tracker import LLMUsageContext, _aggregate_usage


def test_openai_api_equivalent_pricing_handles_cache_and_long_context():
    short_cost, short_basis = estimate_openai_text_cost_usd(
        model_name="gpt-5.6-luna",
        input_tokens=100_000,
        cached_input_tokens=10_000,
        cache_write_tokens=0,
        output_tokens=5_000,
    )
    assert short_cost == pytest.approx(0.0242)
    assert short_basis and short_basis.endswith(":gpt-5.6-luna:short")

    long_cost, long_basis = estimate_openai_text_cost_usd(
        model_name="gpt-5.6-luna",
        input_tokens=300_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=10_000,
    )
    assert long_cost == pytest.approx(0.138)
    assert long_basis and long_basis.endswith(":gpt-5.6-luna:long")


def test_usage_aggregation_preserves_cached_and_reasoning_tokens():
    usage, calls = _aggregate_usage(
        {
            "attempts": [
                {
                    "usage": {
                        "input_tokens": 12_000,
                        "input_tokens_details": {
                            "cached_tokens": 7_000,
                            "cache_write_tokens": 500,
                        },
                        "output_tokens": 1_500,
                        "output_tokens_details": {"reasoning_tokens": 900},
                        "total_tokens": 13_500,
                    }
                },
                {
                    "usage": {
                        "input_tokens": 8_000,
                        "input_tokens_details": {"cached_tokens": 2_000},
                        "output_tokens": 600,
                        "output_tokens_details": {"reasoning_tokens": 100},
                        "total_tokens": 8_600,
                    }
                },
            ]
        }
    )

    assert calls == 2
    assert usage == {
        "input_tokens": 20_000,
        "cached_input_tokens": 9_000,
        "cache_write_tokens": 500,
        "output_tokens": 2_100,
        "reasoning_tokens": 1_000,
        "total_tokens": 22_100,
    }


@pytest.mark.asyncio
async def test_usage_repository_summarizes_entire_turn(db_session):
    campaign_id = uuid4()
    user_turn_id = uuid4()
    db_session.add(
        Campaign(
            id=str(campaign_id),
            name="Usage test",
        )
    )
    db_session.add(
        Turn(
            id=str(user_turn_id),
            campaign_id=str(campaign_id),
            role="user",
            content="Иду дальше.",
        )
    )
    await db_session.flush()

    repo = LLMUsageRepository(db_session)
    context = LLMUsageContext(
        campaign_id=campaign_id,
        user_turn_id=user_turn_id,
    )
    await repo.record(
        context,
        {
            "model_role": "planner",
            "model_name": "gpt-5.6-luna",
            "provider_kind": "chatgpt",
            "transport": "chatgpt_responses",
            "status": "completed",
            "request_count": 1,
            "input_tokens": 10_000,
            "cached_input_tokens": 4_000,
            "cache_write_tokens": 0,
            "output_tokens": 1_000,
            "reasoning_tokens": 600,
            "total_tokens": 11_000,
            "duration_ms": 450,
            "estimated_cost_usd": 0.0024,
            "pricing_basis": "test",
        },
    )
    await repo.record(
        context,
        {
            "model_role": "narrator",
            "model_name": "gpt-5.6-luna",
            "provider_kind": "chatgpt",
            "transport": "chatgpt_responses",
            "status": "completed",
            "request_count": 2,
            "input_tokens": 20_000,
            "cached_input_tokens": 8_000,
            "cache_write_tokens": 0,
            "output_tokens": 2_000,
            "reasoning_tokens": 0,
            "total_tokens": 22_000,
            "duration_ms": 900,
            "estimated_cost_usd": 0.0048,
            "pricing_basis": "test",
        },
    )
    await db_session.commit()

    summary = await repo.summary_for_turn(campaign_id, user_turn_id)

    assert summary["calls"] == 3
    assert summary["input_tokens"] == 30_000
    assert summary["cached_input_tokens"] == 12_000
    assert summary["output_tokens"] == 3_000
    assert summary["reasoning_tokens"] == 600
    assert summary["total_tokens"] == 33_000
    assert summary["estimated_cost_usd"] == pytest.approx(0.0072)
    assert summary["cost_complete"] is True
    assert {row["role"] for row in summary["by_role"]} == {"planner", "narrator"}
