from __future__ import annotations

import hashlib
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
    Entity,
    Event,
    EventParticipant,
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
    ARCHIVE_FORMAT = "personal-dm-campaign"
    ARCHIVE_VERSION = 2

    def __init__(self, session: AsyncSession):
        self._session = session
        self._initial_state = InitialWorldStateService(session)

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _digest(cls, value: object) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    @classmethod
    def _sorted_records(cls, records: list[dict]) -> list[dict]:
        return sorted(records, key=cls._canonical_json)

    async def export_json(self, campaign_id: UUID) -> tuple[Path, dict]:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=100000)
        initial_state = await self._initial_state.get_snapshot(campaign_id)
        projection = await self._canon_projection(campaign_id)
        archive = {
            "format": self.ARCHIVE_FORMAT,
            "version": self.ARCHIVE_VERSION,
            "exported_at": datetime.utcnow().isoformat(),
            "campaign_id": str(campaign_id),
            "initial_world_state": initial_state,
            "campaign": snapshot,
            "canon_projection": projection,
        }
        archive["integrity"] = {
            "algorithm": "sha256",
            "payload_hash": self._digest(archive),
            "canon_projection_hash": self._digest(projection),
        }
        export_dir = Path(settings.DATA_DIR).resolve() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f"campaign-{campaign_id}-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
        path = export_dir / filename
        path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
        return path, archive

    async def rebuild_canon(self, campaign_id: UUID, *, apply: bool = False) -> dict:
        accepted = (
            await self._session.execute(
                select(ProposedChange, Turn)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .order_by(ProposedChange.resolved_at, ProposedChange.created_at, ProposedChange.id)
            )
        ).all()
        snapshot = await self._initial_state.get_snapshot(campaign_id)
        if apply and snapshot is None:
            snapshot = await self._initial_state.ensure_snapshot(campaign_id)
            await self._session.commit()
        baseline_ids = set((snapshot or {}).get("baseline_proposal_ids", []))

        replayable: list[tuple[ChangeType, dict, UUID, str]] = []
        skipped: list[dict] = []
        baseline_covered: list[str] = []
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
            if change_type in {ChangeType.MOVEMENT, ChangeType.ITEM_TRANSFER} and proposal.id in baseline_ids:
                baseline_covered.append(proposal.id)
                continue
            replayable.append((change_type, payload, UUID(turn.id), proposal.id))

        expected_projection = await self._canon_projection(campaign_id)
        report = {
            "campaign_id": str(campaign_id),
            "accepted_proposals": len(accepted),
            "replayable_proposals": len(replayable),
            "stateful_replay_proposals": sum(
                1
                for change_type, *_ in replayable
                if change_type in {ChangeType.MOVEMENT, ChangeType.ITEM_TRANSFER}
            ),
            "baseline_covered_stateful_proposals": len(baseline_covered),
            "checkpoint_exists": snapshot is not None,
            "checkpoint_hash": self._initial_state.digest(snapshot) if snapshot else None,
            "skipped": skipped,
            "applied": False,
            "verified": False,
            "verification_differences": [],
            "projection_hash_before": self._digest(expected_projection),
            "projection_hash_after": None,
            "backup_path": None,
        }
        if not apply:
            return report

        backup = backup_database(f"before-rebuild-{campaign_id}")
        report["backup_path"] = str(backup)

        try:
            await self._initial_state.restore(campaign_id)
            await self._remove_extracted_canon(campaign_id)

            applier = CanonApplier(self._session)
            for change_type, payload, source_turn_id, _proposal_id in replayable:
                await applier.apply(
                    campaign_id=campaign_id,
                    change_type=change_type,
                    payload=payload,
                    source_turn_id=source_turn_id,
                )
            await self._session.flush()

            actual_projection = await self._canon_projection(campaign_id)
            differences = self._projection_differences(expected_projection, actual_projection)
            report["projection_hash_after"] = self._digest(actual_projection)
            report["verification_differences"] = differences
            report["verified"] = not differences
            if differences:
                await self._session.rollback()
                return report

            await self._session.commit()
            report["applied"] = True
            return report
        except Exception:
            await self._session.rollback()
            raise

    async def _remove_extracted_canon(self, campaign_id: UUID) -> None:
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

    async def _canon_projection(self, campaign_id: UUID) -> dict:
        state = await self._initial_state.current_projection(campaign_id)
        facts = (
            await self._session.execute(
                select(Fact).where(
                    Fact.campaign_id == str(campaign_id),
                    Fact.is_current == True,
                )
            )
        ).scalars().all()
        beliefs = (
            await self._session.execute(
                select(Belief)
                .join(Entity, Entity.id == Belief.character_id)
                .where(
                    Entity.campaign_id == str(campaign_id),
                    Belief.is_current == True,
                )
            )
        ).scalars().all()
        relationships = (
            await self._session.execute(
                select(RelationshipAssertion).where(
                    RelationshipAssertion.campaign_id == str(campaign_id),
                    RelationshipAssertion.is_current == True,
                )
            )
        ).scalars().all()
        events = (
            await self._session.execute(
                select(Event).where(Event.campaign_id == str(campaign_id))
            )
        ).scalars().all()
        event_ids = [row.id for row in events]
        participants: dict[str, list[dict]] = {event_id: [] for event_id in event_ids}
        if event_ids:
            rows = (
                await self._session.execute(
                    select(EventParticipant).where(EventParticipant.event_id.in_(event_ids))
                )
            ).scalars().all()
            for row in rows:
                participants.setdefault(row.event_id, []).append(
                    {"entity_id": row.entity_id, "role": row.role}
                )

        return {
            "state": state,
            "facts": self._sorted_records(
                [
                    {
                        "subject": row.subject,
                        "predicate": row.predicate,
                        "object_value": row.object_value,
                        "truth_status": row.truth_status,
                        "confidence": row.confidence,
                        "visibility": row.visibility,
                        "source_turn_id": row.source_turn_id,
                    }
                    for row in facts
                ]
            ),
            "beliefs": self._sorted_records(
                [
                    {
                        "character_id": row.character_id,
                        "fact_id": row.fact_id,
                        "proposition": row.proposition,
                        "status": row.status,
                        "confidence": row.confidence,
                        "source_turn_id": row.source_turn_id,
                        "source_character_id": row.source_character_id,
                        "visibility": row.visibility,
                    }
                    for row in beliefs
                ]
            ),
            "relationships": self._sorted_records(
                [
                    {
                        "subject_id": row.subject_id,
                        "object_id": row.object_id,
                        "relation_type": row.relation_type,
                        "description": row.description,
                        "reason": row.reason,
                        "intensity": row.intensity,
                        "source_turn_id": row.source_turn_id,
                        "provenance": row.provenance,
                        "visibility": row.visibility,
                    }
                    for row in relationships
                ]
            ),
            "events": self._sorted_records(
                [
                    {
                        "event_type": row.event_type,
                        "description": row.description,
                        "world_time": row.world_time,
                        "location_id": row.location_id,
                        "importance": row.importance,
                        "source_turns": json.loads(row.source_turns or "[]"),
                        "participants": self._sorted_records(participants.get(row.id, [])),
                    }
                    for row in events
                ]
            ),
        }

    @staticmethod
    def _projection_differences(expected: dict, actual: dict) -> list[dict]:
        differences: list[dict] = []
        for section in ("state", "facts", "beliefs", "relationships", "events"):
            if expected.get(section) != actual.get(section):
                differences.append(
                    {
                        "section": section,
                        "expected_hash": CampaignArchiveService._digest(expected.get(section)),
                        "actual_hash": CampaignArchiveService._digest(actual.get(section)),
                    }
                )
        return differences
