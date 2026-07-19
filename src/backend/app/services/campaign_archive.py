from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.tables import (
    Belief,
    Event,
    Fact,
    ProposedChange,
    RelationshipAssertion,
    Turn,
)
from app.models.proposed_change import ChangeType
from app.services.canon_applier import CanonApplier
from app.services.debugger_service import DebuggerService
from app.services.initial_world_state import InitialWorldStateService


def sqlite_database_path() -> Path:
    if not settings.DATABASE_URL.startswith("sqlite"):
        raise ValueError("Backup and local rebuild currently require SQLite")
    raw = settings.DATABASE_URL.split("///", 1)[-1]
    return Path(raw).resolve()


def backup_database(reason: str = "manual") -> Path:
    source_path = sqlite_database_path()
    if not source_path.exists():
        raise ValueError(f"Database file does not exist: {source_path}")
    backup_dir = Path(settings.DATA_DIR).resolve() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)[:40]
    target = backup_dir / f"campaign-{datetime.utcnow():%Y%m%d-%H%M%S}-{safe_reason}.db"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


class CampaignArchiveService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._initial_state = InitialWorldStateService(session)

    async def export_json(self, campaign_id: UUID) -> tuple[Path, dict]:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=100000)
        snapshot["archive"] = {
            "format": "personal-dm-campaign",
            "version": 2,
            "exported_at": datetime.utcnow().isoformat(),
            "initial_world_state": await self._initial_state.get(campaign_id),
        }
        export_dir = Path(settings.DATA_DIR).resolve() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f"campaign-{campaign_id}-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
        path = export_dir / filename
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path, snapshot

    async def _accepted_proposals(
        self, campaign_id: UUID
    ) -> tuple[list[tuple[ChangeType, dict, UUID]], list[dict]]:
        accepted = (
            await self._session.execute(
                select(ProposedChange, Turn)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .order_by(Turn.created_at, ProposedChange.created_at)
            )
        ).all()
        replayable: list[tuple[ChangeType, dict, UUID]] = []
        skipped: list[dict] = []
        for proposal, turn in accepted:
            try:
                change_type = ChangeType(proposal.change_type)
            except ValueError:
                skipped.append({"proposal_id": proposal.id, "reason": "unknown change type"})
                continue
            payload_raw = proposal.user_edit if proposal.status == "edited" else proposal.payload
            try:
                payload = json.loads(payload_raw or "{}")
            except json.JSONDecodeError:
                skipped.append({"proposal_id": proposal.id, "reason": "invalid JSON payload"})
                continue
            if change_type in {ChangeType.CANON_GAP, ChangeType.SCENE_THESIS}:
                skipped.append(
                    {"proposal_id": proposal.id, "reason": f"{change_type.value} is not rebuilt"}
                )
                continue
            replayable.append((change_type, payload, UUID(turn.id)))
        return replayable, skipped

    async def capture_initial_state(
        self, campaign_id: UUID, *, replace: bool = False
    ) -> dict:
        return await self._initial_state.capture(campaign_id, replace=replace)

    async def rebuild_canon(
        self,
        campaign_id: UUID,
        *,
        apply: bool = False,
        verify: bool = True,
    ) -> dict:
        replayable, skipped = await self._accepted_proposals(campaign_id)
        stateful = [
            item
            for item in replayable
            if item[0] in {ChangeType.MOVEMENT, ChangeType.ITEM_TRANSFER}
        ]
        initial = await self._initial_state.get(campaign_id)
        report = {
            "campaign_id": str(campaign_id),
            "accepted_proposals": len(replayable) + len(skipped),
            "replayable_proposals": len(replayable),
            "stateful_proposals": len(stateful),
            "initial_state_present": initial is not None,
            "initial_state_hash": initial.get("snapshot_hash") if initial else None,
            "skipped": skipped,
            "applied": False,
            "verified": False,
            "matches_previous_state": None,
            "expected_state_hash": None,
            "rebuilt_state_hash": None,
            "backup_path": None,
        }
        if stateful and not initial:
            report["error"] = (
                "Stateful proposals exist, but the campaign has no initial world-state snapshot"
            )
            if apply:
                raise ValueError(report["error"])
            return report
        if not apply:
            return report

        expected_state = await self._initial_state.current_state(campaign_id)
        report["expected_state_hash"] = self._initial_state.fingerprint(expected_state)
        backup = backup_database(f"before-rebuild-{campaign_id}")
        report["backup_path"] = str(backup)

        if initial:
            await self._initial_state.restore(campaign_id)

        extracted_fact_ids = select(Fact.id).where(
            Fact.campaign_id == str(campaign_id), Fact.source_turn_id.is_not(None)
        )
        await self._session.execute(
            update(Fact)
            .where(
                Fact.campaign_id == str(campaign_id),
                Fact.superseded_by.in_(extracted_fact_ids),
            )
            .values(is_current=True, superseded_by=None)
        )
        extracted_belief_ids = (
            select(Belief.id)
            .join(Turn, Turn.id == Belief.source_turn_id)
            .where(Turn.campaign_id == str(campaign_id))
        )
        await self._session.execute(
            update(Belief)
            .where(Belief.superseded_by.in_(extracted_belief_ids))
            .values(is_current=True, superseded_by=None)
        )
        extracted_relationship_ids = select(RelationshipAssertion.id).where(
            RelationshipAssertion.campaign_id == str(campaign_id),
            RelationshipAssertion.provenance == "extracted",
        )
        await self._session.execute(
            update(RelationshipAssertion)
            .where(
                RelationshipAssertion.campaign_id == str(campaign_id),
                RelationshipAssertion.superseded_by.in_(extracted_relationship_ids),
            )
            .values(is_current=True, superseded_by=None)
        )

        await self._session.execute(
            delete(Belief).where(
                Belief.source_turn_id.in_(
                    select(Turn.id).where(Turn.campaign_id == str(campaign_id))
                )
            )
        )
        await self._session.execute(
            delete(RelationshipAssertion).where(
                RelationshipAssertion.campaign_id == str(campaign_id),
                RelationshipAssertion.provenance == "extracted",
            )
        )
        await self._session.execute(
            delete(Fact).where(
                Fact.campaign_id == str(campaign_id), Fact.source_turn_id.is_not(None)
            )
        )
        await self._session.execute(
            delete(Event).where(
                Event.campaign_id == str(campaign_id),
                Event.source_turns.is_not(None),
                Event.event_type.not_in(["scene_outcome", "scenario_pulse"]),
            )
        )
        await self._session.flush()

        applier = CanonApplier(self._session)
        for change_type, payload, source_turn_id in replayable:
            await applier.apply(
                campaign_id=campaign_id,
                change_type=change_type,
                payload=payload,
                source_turn_id=source_turn_id,
            )
        await self._session.flush()

        rebuilt_state = await self._initial_state.current_state(campaign_id)
        report["rebuilt_state_hash"] = self._initial_state.fingerprint(rebuilt_state)
        report["matches_previous_state"] = rebuilt_state == expected_state
        report["verified"] = bool(verify)
        if verify and not report["matches_previous_state"]:
            await self._session.rollback()
            report["applied"] = False
            report["error"] = "Replay result differs from the pre-rebuild state"
            report["expected_state"] = expected_state
            report["rebuilt_state"] = rebuilt_state
            return report

        await self._session.commit()
        report["applied"] = True
        return report
