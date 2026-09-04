from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Turn
from app.db.truth_engine_table import TruthEventRecord
from app.models.truth_engine import (
    EntityMentionObservation,
    EntityResolutionDecision,
    FluentObservation,
    RelationObservation,
    SemanticTypeResolutionDecision,
)
from app.models.truth_engine_residual import SemanticResidualEnvelope
from app.services.truth_engine_residual import (
    JointResidualEntityResolver,
    SemanticResidualExtractor,
)
from app.services.truth_engine_semantics import (
    ConstrainedSemanticResolver,
    SemanticObservationCompiler,
    SemanticResolutionError,
)
from app.services.truth_engine_turn_context import (
    SemanticTurnContext,
    SemanticTurnContextReader,
)


class SemanticSourceInactive(RuntimeError):
    """The source turn pair stopped being active before a guarded semantic write."""


class _PreparedDecisionResolver:
    """No-LLM adapter used only inside an already guarded write transaction."""

    def __init__(
        self,
        *,
        entity_decision: EntityResolutionDecision | None = None,
        semantic_decision: SemanticTypeResolutionDecision | None = None,
    ):
        self._entity_decision = entity_decision
        self._semantic_decision = semantic_decision

    async def resolve_entity(self, *args, **kwargs) -> EntityResolutionDecision:
        if self._entity_decision is None:
            raise AssertionError("prepared resolver has no entity decision")
        return self._entity_decision

    async def resolve_semantic_type(self, *args, **kwargs):
        if self._semantic_decision is None:
            raise AssertionError("prepared resolver has no semantic-type decision")
        return self._semantic_decision, []


@dataclass(frozen=True)
class SemanticWriterResult:
    entity_ids: dict[str, UUID]
    fluent_event_ids: tuple[UUID, ...]
    relation_event_ids: tuple[UUID, ...]


class SemanticResidualWriterService:
    """Publish objective residual semantics with short, undo-safe write transactions.

    All LLM work happens outside SQLite write locks. After each semantic judgement the service rolls
    back the read transaction, acquires a short ``BEGIN IMMEDIATE`` write lock, re-checks that the
    source user/assistant pair is still active, and only then materializes canonical TE2 state. This
    makes the activity check and event write atomic with respect to /undo without holding a database
    lock while a model is thinking.

    Partial progress is intentional and retry-safe. Entity mentions and each fluent/relation event are
    committed at semantic boundaries. A later failure can retry from stable event keys; if /undo wins
    between boundaries, already committed events are reverted by the normal TE2 turn replay and no
    further events are published.
    """

    SNAPSHOT_KEY = "te2_semantic_writer"

    def __init__(
        self,
        session: AsyncSession,
        *,
        extractor: SemanticResidualExtractor | None = None,
        context_reader: SemanticTurnContextReader | None = None,
        entity_resolver: JointResidualEntityResolver | None = None,
        semantic_resolver: ConstrainedSemanticResolver | None = None,
    ):
        self._session = session
        self._extractor = extractor or SemanticResidualExtractor(session)
        self._context_reader = context_reader or SemanticTurnContextReader(session)
        self._entity_resolver = entity_resolver or JointResidualEntityResolver(session)
        self._semantic_resolver = semantic_resolver or ConstrainedSemanticResolver(session)

    async def write(self, assistant_turn_id: UUID) -> bool:
        context = await self._context_reader.load_active(assistant_turn_id)
        if context is None:
            return False
        # Actor-scoped turns are epistemic dialogue. Their claims remain in the belief/knowledge path
        # until that domain is migrated explicitly; objective TE2 writer mode must not reinterpret it.
        if context.acting_character_id is not None:
            return False

        envelope = await self._extractor.extract(
            context.campaign_id,
            user_content=context.user_content,
            assistant_content=context.assistant_content,
            structured_receipts=list(context.structured_receipts),
        )

        try:
            result = await self._compile(context, envelope)
            await self._write_audit(context, envelope, result)
        except SemanticSourceInactive:
            await self._session.rollback()
            return False
        return True

    async def _compile(
        self,
        context: SemanticTurnContext,
        envelope: SemanticResidualEnvelope,
    ) -> SemanticWriterResult:
        source_key = f"assistant:{context.assistant_turn_id}"
        entity_ids: dict[str, UUID] = {}
        unresolved: list[tuple[str, EntityMentionObservation]] = []

        # Reuse already committed entity observations before considering another semantic judgement.
        for entity in envelope.entities:
            observation = EntityMentionObservation(
                observation_key=f"{source_key}:entity:{entity.ref}",
                mention_text=entity.mention_text,
                entity_type=entity.entity_type,
                description=entity.description,
                source_turn_id=context.user_turn_id,
                scene_id=context.scene_id,
                mention_kind=entity.mention_kind,
                evidence=entity.evidence,
                context_entity_ids=[],
            )
            existing_id = await self._existing_entity_id(
                context.campaign_id,
                observation.observation_key,
            )
            if existing_id is not None:
                entity_ids[entity.ref] = existing_id
            else:
                unresolved.append((entity.ref, observation))

        if unresolved:
            local_graph = {
                "fluents": [
                    {
                        "atom_key": atom.atom_key,
                        "subject_ref": atom.subject_ref,
                        "semantic_description": atom.semantic_description,
                    }
                    for atom in sorted(envelope.fluents, key=lambda item: item.atom_key)
                ],
                "relations": [
                    {
                        "atom_key": atom.atom_key,
                        "subject_ref": atom.subject_ref,
                        "object_ref": atom.object_ref,
                        "semantic_description": atom.semantic_description,
                        "present": atom.present,
                    }
                    for atom in sorted(envelope.relations, key=lambda item: item.atom_key)
                ],
            }
            decisions = await self._entity_resolver.resolve(
                context.campaign_id,
                [observation for _, observation in unresolved],
                local_graph=local_graph,
            )
            await self._begin_guarded_write(context)
            try:
                for ref, observation in sorted(
                    unresolved,
                    key=lambda item: item[1].observation_key,
                ):
                    # A stale/retried worker may have completed the same observation while this
                    # model call was in flight. Re-check under the write lock before creating NEW.
                    existing_id = await self._existing_entity_id(
                        context.campaign_id,
                        observation.observation_key,
                    )
                    if existing_id is not None:
                        entity_ids[ref] = existing_id
                        continue
                    decision = decisions.get(observation.observation_key)
                    if decision is None:
                        raise SemanticResolutionError(
                            f"missing prepared entity decision for {observation.observation_key}"
                        )
                    compiler = SemanticObservationCompiler(
                        self._session,
                        resolver=_PreparedDecisionResolver(entity_decision=decision),
                    )
                    entity_ids[ref] = await compiler.compile_entity_reference(
                        context.campaign_id,
                        observation,
                    )
                await self._session.commit()
            except Exception:
                await self._session.rollback()
                raise

        fluent_event_ids: list[UUID] = []
        for atom in envelope.fluents:
            observation_key = f"{source_key}:fluent:{atom.atom_key}"
            event_key = f"semantic_observation:{observation_key}"
            existing = await self._existing_event_id(context.campaign_id, event_key)
            if existing is not None:
                fluent_event_ids.append(existing)
                continue

            subject_id = entity_ids[atom.subject_ref]
            decision, _ = await self._semantic_resolver.resolve_semantic_type(
                context.campaign_id,
                kind="fluent",
                semantic_description=atom.semantic_description,
                subject_entity_id=subject_id,
                observed_value=atom.value,
                cardinality_hint=atom.cardinality_hint,
            )
            await self._begin_guarded_write(context)
            try:
                existing = await self._existing_event_id(context.campaign_id, event_key)
                if existing is not None:
                    event_id = existing
                else:
                    compiler = SemanticObservationCompiler(
                        self._session,
                        resolver=_PreparedDecisionResolver(semantic_decision=decision),
                    )
                    compiled = await compiler.compile_fluent(
                        context.campaign_id,
                        FluentObservation(
                            observation_key=observation_key,
                            subject_entity_id=subject_id,
                            semantic_description=atom.semantic_description,
                            value=atom.value,
                            description=atom.description,
                            source_turn_id=context.user_turn_id,
                            scene_id=context.scene_id,
                            authority="semantic_compiler",
                            evidence=atom.evidence,
                            cardinality_hint=atom.cardinality_hint,
                        ),
                    )
                    event_id = compiled.event_id
                await self._session.commit()
            except Exception:
                await self._session.rollback()
                raise
            fluent_event_ids.append(event_id)

        relation_event_ids: list[UUID] = []
        for atom in envelope.relations:
            observation_key = f"{source_key}:relation:{atom.atom_key}"
            event_key = f"semantic_observation:{observation_key}"
            existing = await self._existing_event_id(context.campaign_id, event_key)
            if existing is not None:
                relation_event_ids.append(existing)
                continue

            subject_id = entity_ids[atom.subject_ref]
            object_id = entity_ids[atom.object_ref]
            decision, _ = await self._semantic_resolver.resolve_semantic_type(
                context.campaign_id,
                kind="relation",
                semantic_description=atom.semantic_description,
                subject_entity_id=subject_id,
                observed_value={"object_entity_id": str(object_id)},
                cardinality_hint=atom.cardinality_hint,
            )
            await self._begin_guarded_write(context)
            try:
                existing = await self._existing_event_id(context.campaign_id, event_key)
                if existing is not None:
                    event_id = existing
                else:
                    compiler = SemanticObservationCompiler(
                        self._session,
                        resolver=_PreparedDecisionResolver(semantic_decision=decision),
                    )
                    compiled = await compiler.compile_relation(
                        context.campaign_id,
                        RelationObservation(
                            observation_key=observation_key,
                            subject_entity_id=subject_id,
                            object_entity_id=object_id,
                            semantic_description=atom.semantic_description,
                            present=atom.present,
                            description=atom.description,
                            source_turn_id=context.user_turn_id,
                            authority="semantic_compiler",
                            evidence=atom.evidence,
                            cardinality_hint=atom.cardinality_hint,
                        ),
                    )
                    event_id = compiled.event_id
                await self._session.commit()
            except Exception:
                await self._session.rollback()
                raise
            relation_event_ids.append(event_id)

        return SemanticWriterResult(
            entity_ids=entity_ids,
            fluent_event_ids=tuple(fluent_event_ids),
            relation_event_ids=tuple(relation_event_ids),
        )

    async def _begin_guarded_write(self, context: SemanticTurnContext) -> None:
        # End any long-running read snapshot left by candidate retrieval/model work, then acquire
        # SQLite's writer lock before checking activity. /undo can happen before or after this tiny
        # transaction, but it cannot commit between the activity check and canonical write.
        await self._session.rollback()
        await self._session.execute(text("BEGIN IMMEDIATE"))
        if not await self._context_reader.pair_is_active(
            context.assistant_turn_id,
            context.user_turn_id,
        ):
            await self._session.rollback()
            raise SemanticSourceInactive("semantic source pair became inactive")

    async def _existing_entity_id(
        self,
        campaign_id: UUID,
        observation_key: str,
    ) -> UUID | None:
        record = (
            await self._session.execute(
                select(TruthEventRecord).where(
                    TruthEventRecord.campaign_id == str(campaign_id),
                    TruthEventRecord.event_key == f"semantic_entity:{observation_key}",
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        try:
            payload = json.loads(record.payload_json or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise SemanticResolutionError("existing entity event has invalid payload") from exc
        raw_entity_id = payload.get("entity_id") if isinstance(payload, dict) else None
        if not raw_entity_id:
            raise SemanticResolutionError("existing entity observation lost its stable entity_id")
        return UUID(str(raw_entity_id))

    async def _existing_event_id(self, campaign_id: UUID, event_key: str) -> UUID | None:
        raw = (
            await self._session.execute(
                select(TruthEventRecord.event_id).where(
                    TruthEventRecord.campaign_id == str(campaign_id),
                    TruthEventRecord.event_key == event_key,
                )
            )
        ).scalar_one_or_none()
        return UUID(raw) if raw else None

    async def _write_audit(
        self,
        context: SemanticTurnContext,
        envelope: SemanticResidualEnvelope,
        result: SemanticWriterResult,
    ) -> None:
        await self._begin_guarded_write(context)
        try:
            assistant = await self._session.get(Turn, str(context.assistant_turn_id))
            if assistant is None:
                raise SemanticSourceInactive("assistant turn disappeared before semantic audit")
            snapshot = self._snapshot_dict(assistant.context_snapshot)
            snapshot[self.SNAPSHOT_KEY] = {
                "version": 1,
                "mode": "writer",
                "source_user_turn_id": str(context.user_turn_id),
                "receipt_count": len(context.structured_receipts),
                "counts": {
                    "entities": len(envelope.entities),
                    "fluents": len(envelope.fluents),
                    "relations": len(envelope.relations),
                },
                "event_ids": {
                    "fluents": [str(value) for value in result.fluent_event_ids],
                    "relations": [str(value) for value in result.relation_event_ids],
                },
            }
            assistant.context_snapshot = json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    @staticmethod
    def _snapshot_dict(raw: str | dict | None) -> dict:
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}


__all__ = [
    "SemanticResidualWriterService",
    "SemanticSourceInactive",
    "SemanticWriterResult",
]
