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

    async def export_json(self, campaign_id: UUID) -> tuple[Path, dict]:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=100000)
        export_dir = Path(settings.DATA_DIR).resolve() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f"campaign-{campaign_id}-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
        path = export_dir / filename
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path, snapshot

    async def rebuild_canon(self, campaign_id: UUID, *, apply: bool = False) -> dict:
        accepted = (
            await self._session.execute(
                select(ProposedChange, Turn)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .order_by(ProposedChange.resolved_at, ProposedChange.created_at)
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
            if change_type in {ChangeType.MOVEMENT, ChangeType.ITEM_TRANSFER}:
                skipped.append(
                    {
                        "proposal_id": proposal.id,
                        "reason": "stateful movement/item deltas require an initial-state snapshot",
                    }
                )
                continue
            replayable.append((change_type, payload, UUID(turn.id)))

        report = {
            "campaign_id": str(campaign_id),
            "accepted_proposals": len(accepted),
            "replayable_proposals": len(replayable),
            "skipped": skipped,
            "applied": False,
            "backup_path": None,
        }
        if not apply:
            return report

        backup = backup_database(f"before-rebuild-{campaign_id}")
        report["backup_path"] = str(backup)

        # Restore preserved manual records that were superseded by extracted rows.
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
            delete(Belief).where(Belief.source_turn_id.in_(select(Turn.id).where(
                Turn.campaign_id == str(campaign_id)
            )))
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
        await self._session.commit()
        report["applied"] = True
        return report
