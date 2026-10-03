from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.base import BaseRepository
from app.db.tables import LLMUsageEvent

if TYPE_CHECKING:
    from app.services.llm_usage_tracker import LLMUsageContext


class LLMUsageRepository(BaseRepository):
    async def record(self, context: "LLMUsageContext", event: dict[str, Any]) -> None:
        row = LLMUsageEvent(
            campaign_id=str(context.campaign_id),
            user_turn_id=str(context.user_turn_id),
            assistant_turn_id=(
                str(context.assistant_turn_id)
                if context.assistant_turn_id is not None
                else None
            ),
            generation_run_id=(
                str(context.generation_run_id)
                if context.generation_run_id is not None
                else None
            ),
            model_role=event.get("model_role"),
            model_name=str(event.get("model_name") or "unknown"),
            provider_kind=str(event.get("provider_kind") or "unknown"),
            transport=event.get("transport"),
            status=str(event.get("status") or "unknown"),
            request_count=int(event.get("request_count") or 1),
            input_tokens=int(event.get("input_tokens") or 0),
            cached_input_tokens=int(event.get("cached_input_tokens") or 0),
            cache_write_tokens=int(event.get("cache_write_tokens") or 0),
            output_tokens=int(event.get("output_tokens") or 0),
            reasoning_tokens=int(event.get("reasoning_tokens") or 0),
            total_tokens=int(event.get("total_tokens") or 0),
            duration_ms=event.get("duration_ms"),
            estimated_cost_usd=event.get("estimated_cost_usd"),
            pricing_basis=event.get("pricing_basis"),
        )
        self._session.add(row)
        await self._session.flush()

    async def list_for_turn(
        self,
        campaign_id: UUID,
        user_turn_id: UUID,
    ) -> list[LLMUsageEvent]:
        result = await self._session.execute(
            select(LLMUsageEvent)
            .where(
                LLMUsageEvent.campaign_id == str(campaign_id),
                LLMUsageEvent.user_turn_id == str(user_turn_id),
            )
            .order_by(LLMUsageEvent.created_at.asc(), LLMUsageEvent.id.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    def _bucket() -> dict[str, Any]:
        return {
            "calls": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "priced_events": 0,
            "events": 0,
        }

    @classmethod
    def _add_row(cls, bucket: dict[str, Any], row: LLMUsageEvent) -> None:
        bucket["calls"] += int(row.request_count or 1)
        bucket["input_tokens"] += int(row.input_tokens or 0)
        bucket["cached_input_tokens"] += int(row.cached_input_tokens or 0)
        bucket["cache_write_tokens"] += int(row.cache_write_tokens or 0)
        bucket["output_tokens"] += int(row.output_tokens or 0)
        bucket["reasoning_tokens"] += int(row.reasoning_tokens or 0)
        bucket["total_tokens"] += int(row.total_tokens or 0)
        bucket["events"] += 1
        if row.estimated_cost_usd is not None:
            bucket["estimated_cost_usd"] += float(row.estimated_cost_usd)
            bucket["priced_events"] += 1

    @classmethod
    def _public_bucket(cls, bucket: dict[str, Any]) -> dict[str, Any]:
        events = int(bucket["events"])
        priced_events = int(bucket["priced_events"])
        return {
            "calls": int(bucket["calls"]),
            "input_tokens": int(bucket["input_tokens"]),
            "cached_input_tokens": int(bucket["cached_input_tokens"]),
            "cache_write_tokens": int(bucket["cache_write_tokens"]),
            "output_tokens": int(bucket["output_tokens"]),
            "reasoning_tokens": int(bucket["reasoning_tokens"]),
            "total_tokens": int(bucket["total_tokens"]),
            "estimated_cost_usd": (
                round(float(bucket["estimated_cost_usd"]), 6)
                if priced_events
                else None
            ),
            "cost_complete": bool(events and priced_events == events),
        }

    async def summary_for_turn(
        self,
        campaign_id: UUID,
        user_turn_id: UUID,
    ) -> dict[str, Any]:
        rows = await self.list_for_turn(campaign_id, user_turn_id)
        total = self._bucket()
        by_role: dict[str, dict[str, Any]] = defaultdict(self._bucket)
        by_model: dict[str, dict[str, Any]] = defaultdict(self._bucket)

        for row in rows:
            self._add_row(total, row)
            self._add_row(by_role[row.model_role or "unassigned"], row)
            self._add_row(by_model[row.model_name], row)

        return {
            **self._public_bucket(total),
            "event_count": len(rows),
            "by_role": [
                {"role": role, **self._public_bucket(bucket)}
                for role, bucket in sorted(by_role.items())
            ],
            "by_model": [
                {"model": model, **self._public_bucket(bucket)}
                for model, bucket in sorted(by_model.items())
            ],
        }
