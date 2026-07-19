from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import DateTime, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.tables import (
    Belief,
    Campaign,
    Character,
    CharacterGoal,
    Creature,
    Entity,
    Event,
    EventParticipant,
    Fact,
    Faction,
    GenerationRun,
    Item,
    Location,
    MediaAsset,
    PostTurnJob,
    ProposedChange,
    ProviderConfig,
    RelationshipAssertion,
    Scene,
    SceneParticipant,
    SceneThesis,
    Turn,
    WorldStateSnapshot,
)
from app.models.proposed_change import ChangeType
from app.services.canon_applier import CanonApplier
from app.services.world_state_snapshot import WorldStateSnapshotService


ARCHIVE_VERSION = 2
STATEFUL_CHANGE_TYPES = {ChangeType.MOVEMENT, ChangeType.ITEM_TRANSFER}
ARCHIVE_MODELS = (
    Campaign,
    ProviderConfig,
    Entity,
    Character,
    Location,
    Item,
    Faction,
    Creature,
    Scene,
    SceneParticipant,
    Turn,
    GenerationRun,
    PostTurnJob,
    CharacterGoal,
    Fact,
    Belief,
    Event,
    EventParticipant,
    RelationshipAssertion,
    ProposedChange,
    SceneThesis,
    MediaAsset,
    WorldStateSnapshot,
)
ARCHIVE_TABLES = {model.__tablename__: model.__table__ for model in ARCHIVE_MODELS}
IMPORT_ORDER = tuple(model.__tablename__ for model in ARCHIVE_MODELS)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _archive_digest(
    *,
    campaign_id: str,
    state_digest: str,
    tables: dict[str, list[dict]],
) -> str:
    return _digest(
        {
            "archive_version": ARCHIVE_VERSION,
            "campaign_id": campaign_id,
            "state_digest": state_digest,
            "tables": tables,
        }
    )


def _event_sources(row: Event) -> list[str]:
    try:
        return [str(value) for value in json.loads(row.source_turns or "[]")]
    except (TypeError, json.JSONDecodeError):
        return []


def _sorted_records(records: list[dict]) -> list[dict]:
    return sorted(records, key=_canonical_json)


def _serialize_value(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _deserialize_row(table, row: dict) -> dict:
    result: dict = {}
    for column in table.c:
        value = row.get(column.name)
        if value is not None and isinstance(column.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        result[column.name] = value
    return result


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
        self._world = WorldStateSnapshotService(session)

    async def _raw_rows(self, table, where_clause) -> list[dict]:
        rows = (
            (
                await self._session.execute(
                    select(*table.c).where(where_clause).order_by(*table.primary_key.columns)
                )
            )
            .mappings()
            .all()
        )
        return [{key: _serialize_value(value) for key, value in dict(row).items()} for row in rows]

    async def _export_tables(self, campaign_id: UUID) -> dict[str, list[dict]]:
        campaign_key = str(campaign_id)
        entity_ids = list(
            (
                await self._session.execute(
                    select(Entity.id).where(Entity.campaign_id == campaign_key)
                )
            ).scalars()
        )
        scene_ids = list(
            (
                await self._session.execute(
                    select(Scene.id).where(Scene.campaign_id == campaign_key)
                )
            ).scalars()
        )
        turn_ids = list(
            (
                await self._session.execute(select(Turn.id).where(Turn.campaign_id == campaign_key))
            ).scalars()
        )
        event_ids = list(
            (
                await self._session.execute(
                    select(Event.id).where(Event.campaign_id == campaign_key)
                )
            ).scalars()
        )

        conditions = {
            "campaigns": Campaign.id == campaign_key,
            "provider_configs": ProviderConfig.campaign_id == campaign_key,
            "entities": Entity.campaign_id == campaign_key,
            "characters": Character.entity_id.in_(entity_ids),
            "locations": Location.entity_id.in_(entity_ids),
            "items": Item.entity_id.in_(entity_ids),
            "factions": Faction.entity_id.in_(entity_ids),
            "creatures": Creature.entity_id.in_(entity_ids),
            "scenes": Scene.campaign_id == campaign_key,
            "scene_participants": SceneParticipant.scene_id.in_(scene_ids),
            "turns": Turn.campaign_id == campaign_key,
            "generation_runs": GenerationRun.campaign_id == campaign_key,
            "post_turn_jobs": PostTurnJob.campaign_id == campaign_key,
            "character_goals": CharacterGoal.character_id.in_(entity_ids),
            "facts": Fact.campaign_id == campaign_key,
            "beliefs": Belief.character_id.in_(entity_ids),
            "events": Event.campaign_id == campaign_key,
            "event_participants": EventParticipant.event_id.in_(event_ids),
            "relationship_assertions": RelationshipAssertion.campaign_id == campaign_key,
            "proposed_changes": ProposedChange.turn_id.in_(turn_ids),
            "scene_theses": SceneThesis.scene_id.in_(scene_ids),
            "media_assets": MediaAsset.campaign_id == campaign_key,
            "world_state_snapshots": WorldStateSnapshot.campaign_id == campaign_key,
        }
        tables: dict[str, list[dict]] = {}
        for table_name in IMPORT_ORDER:
            table = ARCHIVE_TABLES[table_name]
            rows = await self._raw_rows(table, conditions[table_name])
            if table_name == "provider_configs":
                for row in rows:
                    row["api_key_encrypted"] = None
            tables[table_name] = rows
        return tables

    async def canonical_state(self, campaign_id: UUID) -> dict:
        campaign_key = str(campaign_id)
        entity_ids = select(Entity.id).where(Entity.campaign_id == campaign_key)

        facts = (
            (
                await self._session.execute(
                    select(Fact).where(Fact.campaign_id == campaign_key, Fact.is_current.is_(True))
                )
            )
            .scalars()
            .all()
        )
        beliefs = (
            (
                await self._session.execute(
                    select(Belief).where(
                        Belief.character_id.in_(entity_ids), Belief.is_current.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        relationships = (
            (
                await self._session.execute(
                    select(RelationshipAssertion).where(
                        RelationshipAssertion.campaign_id == campaign_key,
                        RelationshipAssertion.is_current.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        fact_semantics = {
            row.id: {
                "subject": row.subject,
                "predicate": row.predicate,
                "object_value": row.object_value,
                "truth_status": row.truth_status,
            }
            for row in (
                await self._session.execute(select(Fact).where(Fact.campaign_id == campaign_key))
            )
            .scalars()
            .all()
        }
        events = [
            row
            for row in (
                await self._session.execute(
                    select(Event).where(
                        Event.campaign_id == campaign_key,
                        Event.event_type.not_in(["scene_outcome", "scenario_pulse"]),
                    )
                )
            )
            .scalars()
            .all()
            if _event_sources(row)
        ]
        world = await self._world.current_state(campaign_id)
        return {
            "facts": _sorted_records(
                [
                    {
                        "subject": row.subject,
                        "predicate": row.predicate,
                        "object_value": row.object_value,
                        "truth_status": row.truth_status,
                        "visibility": row.visibility,
                        "confidence": round(row.confidence, 6),
                    }
                    for row in facts
                ]
            ),
            "beliefs": _sorted_records(
                [
                    {
                        "character_id": row.character_id,
                        "fact": fact_semantics.get(row.fact_id),
                        "proposition": row.proposition,
                        "status": row.status,
                        "confidence": round(row.confidence, 6),
                        "source_character_id": row.source_character_id,
                        "visibility": row.visibility,
                    }
                    for row in beliefs
                ]
            ),
            "relationships": _sorted_records(
                [
                    {
                        "subject_id": row.subject_id,
                        "object_id": row.object_id,
                        "relation_type": row.relation_type,
                        "description": row.description,
                        "reason": row.reason,
                        "intensity": row.intensity,
                        "visibility": row.visibility,
                    }
                    for row in relationships
                ]
            ),
            "events": _sorted_records(
                [
                    {
                        "event_type": row.event_type,
                        "description": row.description,
                        "world_time": row.world_time,
                        "location_id": row.location_id,
                        "importance": row.importance,
                        "source_turns": sorted(_event_sources(row)),
                    }
                    for row in events
                ]
            ),
            "world": world,
        }

    async def build_archive(self, campaign_id: UUID) -> dict:
        campaign = await self._session.get(Campaign, str(campaign_id))
        if campaign is None:
            raise ValueError("Campaign not found")
        tables = await self._export_tables(campaign_id)
        state = await self.canonical_state(campaign_id)
        state_hash = _digest(state)
        archive = {
            "archive_version": ARCHIVE_VERSION,
            "campaign_id": str(campaign_id),
            "exported_at": datetime.utcnow().isoformat(),
            "tables": tables,
            "state_digest": state_hash,
        }
        archive["archive_digest"] = _archive_digest(
            campaign_id=str(campaign_id),
            state_digest=state_hash,
            tables=tables,
        )
        return archive

    async def export_json(self, campaign_id: UUID) -> tuple[Path, dict]:
        archive = await self.build_archive(campaign_id)
        export_dir = Path(settings.DATA_DIR).resolve() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f"campaign-{campaign_id}-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
        path = export_dir / filename
        path.write_text(
            json.dumps(archive, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path, archive

    def _validate_archive(self, archive: dict) -> tuple[str, dict[str, list[dict]]]:
        if archive.get("archive_version") != ARCHIVE_VERSION:
            raise ValueError("Unsupported campaign archive version")
        campaign_id = str(archive.get("campaign_id") or "")
        tables = archive.get("tables")
        if not campaign_id or not isinstance(tables, dict):
            raise ValueError("Campaign archive is missing campaign_id or tables")
        unknown = set(tables) - set(ARCHIVE_TABLES)
        if unknown:
            raise ValueError(f"Campaign archive contains unknown tables: {sorted(unknown)}")
        for table_name in IMPORT_ORDER:
            if not isinstance(tables.get(table_name), list):
                raise ValueError(f"Campaign archive is missing table {table_name}")
        campaign_rows = tables["campaigns"]
        if len(campaign_rows) != 1 or str(campaign_rows[0].get("id")) != campaign_id:
            raise ValueError("Campaign archive must contain exactly one matching campaign")
        for row in tables["provider_configs"]:
            if row.get("api_key_encrypted"):
                raise ValueError("Portable campaign archive must not contain provider secrets")
        expected_archive_digest = _archive_digest(
            campaign_id=campaign_id,
            state_digest=str(archive.get("state_digest") or ""),
            tables=tables,
        )
        if archive.get("archive_digest") != expected_archive_digest:
            raise ValueError("Campaign archive digest does not match its payload")
        return campaign_id, tables

    async def _purge_campaign_rows(self, campaign_key: str) -> None:
        entity_ids = select(Entity.id).where(Entity.campaign_id == campaign_key)
        scene_ids = select(Scene.id).where(Scene.campaign_id == campaign_key)
        turn_ids = select(Turn.id).where(Turn.campaign_id == campaign_key)
        event_ids = select(Event.id).where(Event.campaign_id == campaign_key)
        deletions = (
            delete(WorldStateSnapshot).where(WorldStateSnapshot.campaign_id == campaign_key),
            delete(MediaAsset).where(MediaAsset.campaign_id == campaign_key),
            delete(SceneThesis).where(SceneThesis.scene_id.in_(scene_ids)),
            delete(ProposedChange).where(ProposedChange.turn_id.in_(turn_ids)),
            delete(RelationshipAssertion).where(RelationshipAssertion.campaign_id == campaign_key),
            delete(EventParticipant).where(EventParticipant.event_id.in_(event_ids)),
            delete(Event).where(Event.campaign_id == campaign_key),
            delete(Belief).where(Belief.character_id.in_(entity_ids)),
            delete(Fact).where(Fact.campaign_id == campaign_key),
            delete(CharacterGoal).where(CharacterGoal.character_id.in_(entity_ids)),
            delete(PostTurnJob).where(PostTurnJob.campaign_id == campaign_key),
            delete(GenerationRun).where(GenerationRun.campaign_id == campaign_key),
            delete(Turn).where(Turn.campaign_id == campaign_key),
            delete(SceneParticipant).where(SceneParticipant.scene_id.in_(scene_ids)),
            delete(Scene).where(Scene.campaign_id == campaign_key),
            delete(Creature).where(Creature.entity_id.in_(entity_ids)),
            delete(Faction).where(Faction.entity_id.in_(entity_ids)),
            delete(Item).where(Item.entity_id.in_(entity_ids)),
            delete(Location).where(Location.entity_id.in_(entity_ids)),
            delete(Character).where(Character.entity_id.in_(entity_ids)),
            delete(ProviderConfig).where(ProviderConfig.campaign_id == campaign_key),
            delete(Entity).where(Entity.campaign_id == campaign_key),
            delete(Campaign).where(Campaign.id == campaign_key),
        )
        for statement in deletions:
            await self._session.execute(statement)
        await self._session.flush()

    async def import_archive(self, archive: dict, *, replace: bool = False) -> dict:
        campaign_key, tables = self._validate_archive(archive)
        existing = await self._session.get(Campaign, campaign_key)
        backup_path = None
        if existing is not None and not replace:
            raise ValueError("Campaign already exists; use replace=true for explicit replacement")
        if replace and existing is not None:
            backup_path = str(backup_database(f"before-import-{campaign_key}"))

        try:
            if replace:
                await self._purge_campaign_rows(campaign_key)
            await self._session.execute(text("PRAGMA defer_foreign_keys=ON"))
            inserted: dict[str, int] = {}
            for table_name in IMPORT_ORDER:
                table = ARCHIVE_TABLES[table_name]
                rows = [_deserialize_row(table, row) for row in tables[table_name]]
                if rows:
                    await self._session.execute(table.insert(), rows)
                inserted[table_name] = len(rows)
            await self._session.flush()

            campaign_id = UUID(campaign_key)
            imported_state = await self.canonical_state(campaign_id)
            imported_digest = _digest(imported_state)
            expected_digest = str(archive.get("state_digest") or "")
            if not expected_digest or imported_digest != expected_digest:
                raise ValueError("Imported campaign state does not match the archive state digest")
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        return {
            "campaign_id": campaign_key,
            "inserted": inserted,
            "backup_path": backup_path,
            "archive_digest": archive["archive_digest"],
            "state_digest": imported_digest,
            "state_matches_export": True,
        }

    async def _accepted_proposals(
        self, campaign_id: UUID
    ) -> tuple[list[tuple[ChangeType, dict, UUID]], list[dict], int]:
        accepted = (
            await self._session.execute(
                select(ProposedChange, Turn)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .order_by(Turn.created_at, ProposedChange.created_at, ProposedChange.id)
            )
        ).all()
        ordered: list[tuple[int, int, int, ChangeType, dict, UUID]] = []
        skipped: list[dict] = []
        turn_order: dict[str, int] = {}
        for proposal_index, (proposal, turn) in enumerate(accepted):
            turn_order.setdefault(turn.id, len(turn_order))
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
            # Within one turn, establish objective facts before knowledge links to them.
            type_order = 1 if change_type == ChangeType.KNOWLEDGE else 0
            ordered.append(
                (
                    turn_order[turn.id],
                    type_order,
                    proposal_index,
                    change_type,
                    payload,
                    UUID(turn.id),
                )
            )
        ordered.sort(key=lambda item: item[:3])
        replayable = [
            (change_type, payload, source_turn_id)
            for _, _, _, change_type, payload, source_turn_id in ordered
        ]
        return replayable, skipped, len(accepted)

    async def _remap_fact_reference(
        self,
        campaign_id: UUID,
        payload: dict,
        old_fact_refs: dict[str, dict],
    ) -> dict:
        old_fact_id = str(payload.get("fact_id") or "")
        reference = old_fact_refs.get(old_fact_id)
        if not reference:
            return payload
        clauses = [
            Fact.campaign_id == str(campaign_id),
            Fact.source_turn_id == reference["source_turn_id"],
            Fact.subject == reference["subject"],
            Fact.predicate == reference["predicate"],
            Fact.truth_status == reference["truth_status"],
        ]
        if reference["object_value"] is None:
            clauses.append(Fact.object_value.is_(None))
        else:
            clauses.append(Fact.object_value == reference["object_value"])
        rebuilt = await self._session.scalar(
            select(Fact).where(*clauses).order_by(Fact.created_at.desc()).limit(1)
        )
        if rebuilt is None:
            if payload.get("proposition"):
                adjusted = dict(payload)
                adjusted["fact_id"] = None
                return adjusted
            raise ValueError("Knowledge proposal references an extracted fact that was not rebuilt")
        adjusted = dict(payload)
        adjusted["fact_id"] = rebuilt.id
        return adjusted

    async def rebuild_canon(self, campaign_id: UUID, *, apply: bool = False) -> dict:
        replayable, skipped, accepted_count = await self._accepted_proposals(campaign_id)
        stateful_count = sum(
            1 for change_type, _, _ in replayable if change_type in STATEFUL_CHANGE_TYPES
        )
        initial_snapshot = await self._world.get(campaign_id)
        if stateful_count and initial_snapshot is None:
            raise ValueError(
                "Initial world snapshot is missing; movement and item transfer cannot be replayed safely"
            )

        before_state = await self.canonical_state(campaign_id)
        extracted_facts = (
            (
                await self._session.execute(
                    select(Fact).where(
                        Fact.campaign_id == str(campaign_id),
                        Fact.source_turn_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        old_fact_refs = {
            row.id: {
                "source_turn_id": row.source_turn_id,
                "subject": row.subject,
                "predicate": row.predicate,
                "object_value": row.object_value,
                "truth_status": row.truth_status,
            }
            for row in extracted_facts
        }
        report = {
            "campaign_id": str(campaign_id),
            "accepted_proposals": accepted_count,
            "replayable_proposals": len(replayable),
            "stateful_proposals": stateful_count,
            "has_initial_world_snapshot": initial_snapshot is not None,
            "skipped": skipped,
            "applied": False,
            "backup_path": None,
            "before_digest": _digest(before_state),
            "after_digest": None,
            "semantic_match_before": None,
        }
        if not apply:
            return report

        backup = backup_database(f"before-rebuild-{campaign_id}")
        report["backup_path"] = str(backup)
        if stateful_count:
            await self._world.restore(campaign_id)

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
        replay_event_ids = [
            row.id
            for row in (
                await self._session.execute(
                    select(Event).where(
                        Event.campaign_id == str(campaign_id),
                        Event.event_type.not_in(["scene_outcome", "scenario_pulse"]),
                    )
                )
            )
            .scalars()
            .all()
            if _event_sources(row)
        ]
        if replay_event_ids:
            await self._session.execute(
                delete(EventParticipant).where(EventParticipant.event_id.in_(replay_event_ids))
            )
            await self._session.execute(delete(Event).where(Event.id.in_(replay_event_ids)))
        await self._session.flush()

        applier = CanonApplier(self._session)
        for change_type, payload, source_turn_id in replayable:
            if change_type == ChangeType.KNOWLEDGE:
                payload = await self._remap_fact_reference(campaign_id, payload, old_fact_refs)
            await applier.apply(
                campaign_id=campaign_id,
                change_type=change_type,
                payload=payload,
                source_turn_id=source_turn_id,
            )
        await self._session.commit()
        after_state = await self.canonical_state(campaign_id)
        report["applied"] = True
        report["after_digest"] = _digest(after_state)
        report["semantic_match_before"] = report["after_digest"] == report["before_digest"]
        return report
