from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.entity_repo import EntityRepository
from app.models.memory_taxonomy import MemoryKind, NarrativeDetailType
from app.models.proposed_change import ChangeType, ProposedChangeCreate


class MemoryTaxonomyService:
    """Validate semantic Scribe labels; never infer persistence from prose vocabulary.

    Scribe owns meaning/evidence; this boundary owns stable references, scope and retention.
    Legacy proposals retain explicit scope rather than a guessed lexical classification.
    """

    def __init__(self, session: AsyncSession):
        self._entities = EntityRepository(session)

    async def classify_batch(
        self, campaign_id: UUID, scene_id: UUID | None, proposals: list[ProposedChangeCreate]
    ) -> list[ProposedChangeCreate]:
        if not proposals:
            return proposals
        aliases = await self._entity_aliases(campaign_id)
        results = []
        for proposal in proposals:
            if proposal.change_type == ChangeType.FACT:
                classified = self._classify_fact(proposal, scene_id=scene_id, aliases=aliases)
                if classified:
                    results.append(classified)
            elif proposal.change_type == ChangeType.NARRATIVE_DETAIL:
                detail = self._normalize_detail(
                    proposal.payload, scene_id=scene_id, aliases=aliases
                )
                if detail:
                    results.append(
                        ProposedChangeCreate(
                            change_type=ChangeType.NARRATIVE_DETAIL, payload=detail
                        )
                    )
            else:
                results.append(proposal)
        return results

    async def extract_narrative_details(
        self, campaign_id: UUID, scene_id: UUID | None, assistant_content: str
    ) -> list[ProposedChangeCreate]:
        # Raw sentences are not memory operations. Scribe selects useful texture semantically
        # in its existing call, with explicit non-durable provenance; no extra inference pass.
        return []

    async def _entity_aliases(self, campaign_id: UUID) -> dict[str, str]:
        aliases: dict[str, str] = {}
        ambiguous: set[str] = set()
        for entity in await self._entities.list_by_campaign(campaign_id):
            for name in (entity.canonical_name, *entity.aliases):
                key = self._normalize(name)
                if key in aliases and aliases[key] != str(entity.id):
                    ambiguous.add(key)
                elif key:
                    aliases[key] = str(entity.id)
        return {key: value for key, value in aliases.items() if key not in ambiguous}

    def _classify_fact(
        self, proposal: ProposedChangeCreate, *, scene_id: UUID | None, aliases: dict[str, str]
    ) -> ProposedChangeCreate | None:
        payload = dict(proposal.payload)
        canon = payload.get("_canon") or {}
        subject_id = self._resolve_subject(payload, aliases)
        explicit = payload.get("memory_kind")
        if canon.get("durable") is False or canon.get("kind") == "narrative_detail":
            kind = MemoryKind.NARRATIVE_DETAIL
        elif explicit in {item.value for item in MemoryKind}:
            kind = MemoryKind(explicit)
        else:
            kind = (
                MemoryKind.SCENE_STATE
                if payload.get("scope") == "scene"
                else MemoryKind.WORLD_CANON
            )
        if kind == MemoryKind.NARRATIVE_DETAIL:
            text = (
                canon.get("evidence")
                or canon.get("description")
                or " ".join(
                    str(payload.get(key) or "") for key in ("subject", "predicate", "object_value")
                )
            )
            detail = self._normalize_detail(
                {**payload, "text": text, "subject_entity_id": subject_id},
                scene_id=scene_id,
                aliases=aliases,
            )
            if not detail:
                return None
            detail["_memory"]["demoted_from"] = "fact"
            return ProposedChangeCreate(change_type=ChangeType.NARRATIVE_DETAIL, payload=detail)
        payload["memory_kind"] = kind.value
        if subject_id:
            payload["subject_entity_id"] = subject_id
        if kind == MemoryKind.SCENE_STATE:
            if not scene_id:
                return None
            payload.update(scope="scene", scene_id=str(scene_id))
        else:
            payload["scope"] = "campaign"
            payload.pop("scene_id", None)
        if kind == MemoryKind.ENTITY_STATE and not subject_id:
            payload["_validation_error"] = "entity_state requires a stable subject entity reference"
        payload["_memory"] = {"kind": kind.value, "classifier": "typed-semantic-v2"}
        return ProposedChangeCreate(change_type=ChangeType.FACT, payload=payload)

    def _normalize_detail(
        self, payload: dict, *, scene_id: UUID | None, aliases: dict[str, str]
    ) -> dict | None:
        effective_scene = payload.get("scene_id") or scene_id
        text = str(payload.get("text") or payload.get("description") or "").strip()
        if not effective_scene or not text:
            return None
        result = dict(payload)
        result.update(scene_id=str(effective_scene), text=text[:2000])
        detail_type = result.get("detail_type")
        result["detail_type"] = (
            detail_type
            if detail_type in {item.value for item in NarrativeDetailType}
            else NarrativeDetailType.OTHER.value
        )
        result["turn_window"] = max(
            1, min(12, int(result.get("turn_window") or settings.NARRATIVE_DETAIL_TURN_WINDOW))
        )
        result["visibility"] = result.get("visibility") or "public"
        subject_id = self._resolve_subject(payload, aliases)
        if subject_id:
            result["subject_entity_id"] = subject_id
        result["_memory"] = {
            "kind": MemoryKind.NARRATIVE_DETAIL.value,
            "classifier": "typed-semantic-v2",
        }
        return result

    @staticmethod
    def _resolve_subject(payload: dict, aliases: dict[str, str]) -> str | None:
        direct = payload.get("subject_entity_id")
        if direct:
            try:
                resolved = str(UUID(str(direct)))
            except (ValueError, TypeError, AttributeError):
                return None
            return resolved if resolved in aliases.values() else None
        return aliases.get(MemoryTaxonomyService._normalize(payload.get("subject")))

    @staticmethod
    def _normalize(value: object) -> str:
        return " ".join(str(value or "").casefold().split())
