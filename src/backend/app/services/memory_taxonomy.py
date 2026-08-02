from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.entity_repo import EntityRepository
from app.models.memory_taxonomy import MemoryKind, NarrativeDetailType
from app.models.proposed_change import ChangeType, ProposedChangeCreate


class MemoryTaxonomyService:
    """Normalize Scribe proposals into explicit persistence classes.

    The LLM may still emit a transient observation as a fact. This service is the
    deterministic boundary that prevents a gaze, pose or ambient sound from becoming
    permanent campaign canon.
    """

    TRANSIENT_PATTERNS = (
        r"\bвзгляд\w*\b",
        r"\bпосмотр\w*\b",
        r"\bглянул\w*\b",
        r"\bотвел\w*\b",
        r"\bотвела\w*\b",
        r"\bулыб\w*\b",
        r"\bкив\w*\b",
        r"\bвздох\w*\b",
        r"\bморг\w*\b",
        r"\bпожал\w* плеч\w*\b",
        r"\bжест\w*\b",
        r"\bпоз[аеуы]\b",
        r"\bвыражени\w* лиц\w*\b",
        r"\bтон голос\w*\b",
        r"\bшум\w* дожд\w*\b",
        r"\bкапл\w*\b",
        r"\bветер\w*\b",
        r"\bсвет\w* дрог\w*\b",
        r"\bglanc\w*\b",
        r"\bgaze\w*\b",
        r"\bsmil\w*\b",
        r"\bnod\w*\b",
        r"\bshrug\w*\b",
        r"\bsigh\w*\b",
        r"\bblink\w*\b",
        r"\bambient\b",
    )
    ENTITY_STATE_PATTERNS = (
        r"\bранен\w*\b",
        r"\bтравм\w*\b",
        r"\bотравлен\w*\b",
        r"\bбез сознани\w*\b",
        r"\bсвязан\w*\b",
        r"\bобездвиж\w*\b",
        r"\bзамаскирован\w*\b",
        r"\bвооружен\w*\b",
        r"\bнесет\w*\b",
        r"\bносит\w*\b",
        r"\bболен\w*\b",
        r"\bwound\w*\b",
        r"\binjur\w*\b",
        r"\bpoison\w*\b",
        r"\bunconscious\b",
        r"\bbound\b",
        r"\bdisguis\w*\b",
        r"\barmed\b",
    )
    WORLD_CANON_PATTERNS = (
        r"\bродил\w*\b",
        r"\bпроисхожд\w*\b",
        r"\bявляется\b",
        r"\bпринадлежит к\b",
        r"\bсоздан\w*\b",
        r"\bоснован\w*\b",
        r"\bзакон\w*\b",
        r"\bправил\w*\b",
        r"\bстолиц\w*\b",
        r"\bистинн\w* имен\w*\b",
        r"\bслабост\w*\b",
        r"\bborn\b",
        r"\borigin\w*\b",
        r"\bis a\b",
        r"\bcreated\b",
        r"\blaw\b",
        r"\brule\b",
        r"\bcapital\b",
        r"\btrue name\b",
    )
    SCENE_STATE_PATTERNS = (
        r"\bдвер\w*\b",
        r"\bокн\w*\b",
        r"\bворот\w*\b",
        r"\bогон\w*\b",
        r"\bплам\w*\b",
        r"\bслед\w*\b",
        r"\bкров\w*\b",
        r"\bзамок\w*\b",
        r"\bкомнат\w*\b",
        r"\bпроход\w*\b",
        r"\bоткрыт\w*\b",
        r"\bзакрыт\w*\b",
        r"\bзаперт\w*\b",
        r"\bразруш\w*\b",
        r"\bгорит\w*\b",
        r"\bdoor\b",
        r"\bwindow\b",
        r"\bgate\b",
        r"\bfire\b",
        r"\btrail\b",
        r"\bblood\b",
        r"\bopen\b",
        r"\bclosed\b",
        r"\blocked\b",
        r"\bbroken\b",
        r"\bburning\b",
    )

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)

    async def classify_batch(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        proposals: list[ProposedChangeCreate],
    ) -> list[ProposedChangeCreate]:
        if not proposals:
            return proposals

        aliases: dict[str, str] = {}
        for entity in await self._entities.list_by_campaign(campaign_id):
            for name in (entity.canonical_name, *entity.aliases):
                normalized = self._normalize(name)
                if normalized:
                    aliases[normalized] = str(entity.id)

        results: list[ProposedChangeCreate] = []
        for proposal in proposals:
            if proposal.change_type == ChangeType.FACT:
                classified = self._classify_fact(
                    proposal,
                    scene_id=scene_id,
                    aliases=aliases,
                )
                if classified:
                    results.append(classified)
                continue
            if proposal.change_type == ChangeType.NARRATIVE_DETAIL:
                normalized = self._normalize_detail(
                    proposal.payload,
                    scene_id=scene_id,
                    aliases=aliases,
                )
                if normalized:
                    results.append(
                        ProposedChangeCreate(
                            change_type=ChangeType.NARRATIVE_DETAIL,
                            payload=normalized,
                        )
                    )
                continue
            results.append(proposal)
        return results

    def _classify_fact(
        self,
        proposal: ProposedChangeCreate,
        *,
        scene_id: UUID | None,
        aliases: dict[str, str],
    ) -> ProposedChangeCreate | None:
        payload = dict(proposal.payload)
        subject_entity_id = self._resolve_subject(payload, aliases)
        text = self._proposal_text(payload)
        explicit = str(payload.get("memory_kind") or "").strip()
        scope = str(payload.get("scope") or "campaign").casefold()

        if explicit == MemoryKind.NARRATIVE_DETAIL.value:
            kind = MemoryKind.NARRATIVE_DETAIL
        elif subject_entity_id and self._matches(self.ENTITY_STATE_PATTERNS, text):
            kind = MemoryKind.ENTITY_STATE
        elif self._matches(self.TRANSIENT_PATTERNS, text):
            kind = MemoryKind.NARRATIVE_DETAIL
        elif explicit in {
            MemoryKind.WORLD_CANON.value,
            MemoryKind.ENTITY_STATE.value,
            MemoryKind.SCENE_STATE.value,
        }:
            kind = MemoryKind(explicit)
        elif scope == "scene":
            kind = MemoryKind.SCENE_STATE
        elif self._matches(self.WORLD_CANON_PATTERNS, text):
            kind = MemoryKind.WORLD_CANON
        elif subject_entity_id:
            kind = MemoryKind.ENTITY_STATE
        else:
            kind = MemoryKind.WORLD_CANON

        if kind == MemoryKind.NARRATIVE_DETAIL:
            normalized = self._detail_from_fact(
                payload,
                scene_id=scene_id,
                subject_entity_id=subject_entity_id,
            )
            if not normalized:
                # Without an authoritative scene, transient prose cannot be retained.
                return None
            return ProposedChangeCreate(
                change_type=ChangeType.NARRATIVE_DETAIL,
                payload=normalized,
            )

        payload["memory_kind"] = kind.value
        if subject_entity_id:
            payload["subject_entity_id"] = subject_entity_id
        if kind == MemoryKind.SCENE_STATE:
            if not scene_id:
                return None
            payload["scope"] = "scene"
            payload["scene_id"] = str(scene_id)
        else:
            payload["scope"] = "campaign"
            payload.pop("scene_id", None)
        if kind == MemoryKind.ENTITY_STATE and not subject_entity_id:
            payload["_validation_error"] = (
                "entity_state requires a stable subject entity reference"
            )
        payload["_memory"] = {
            "kind": kind.value,
            "classifier": "deterministic-v1",
        }
        return ProposedChangeCreate(change_type=ChangeType.FACT, payload=payload)

    def _detail_from_fact(
        self,
        payload: dict,
        *,
        scene_id: UUID | None,
        subject_entity_id: str | None,
    ) -> dict | None:
        if not scene_id:
            return None
        canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
        text = str(canon.get("evidence") or canon.get("description") or "").strip()
        if not text:
            text = " ".join(
                str(value).strip()
                for value in (
                    payload.get("subject"),
                    payload.get("predicate"),
                    payload.get("object_value"),
                )
                if value
            )
        if not text:
            return None
        authority = str(canon.get("authority") or "")
        visibility = payload.get("visibility") or (
            "public" if authority == "public_observation" else "dm"
        )
        result = {
            "scene_id": str(scene_id),
            "text": text[:2000],
            "detail_type": self._detail_type(text).value,
            "visibility": visibility,
            "turn_window": max(
                1,
                min(12, int(settings.NARRATIVE_DETAIL_TURN_WINDOW)),
            ),
            "_memory": {
                "kind": MemoryKind.NARRATIVE_DETAIL.value,
                "classifier": "deterministic-v1",
                "demoted_from": "fact",
            },
        }
        if subject_entity_id:
            result["subject_entity_id"] = subject_entity_id
        if canon:
            result["_canon"] = canon
        return result

    def _normalize_detail(
        self,
        payload: dict,
        *,
        scene_id: UUID | None,
        aliases: dict[str, str],
    ) -> dict | None:
        effective_scene_id = payload.get("scene_id") or (
            str(scene_id) if scene_id else None
        )
        text = str(payload.get("text") or payload.get("description") or "").strip()
        if not effective_scene_id or not text:
            return None
        subject_entity_id = payload.get("subject_entity_id")
        if not subject_entity_id:
            subject_entity_id = self._resolve_subject(payload, aliases)
        result = dict(payload)
        result["scene_id"] = str(effective_scene_id)
        result["text"] = text[:2000]
        detail_type = str(result.get("detail_type") or "")
        if detail_type not in {item.value for item in NarrativeDetailType}:
            detail_type = self._detail_type(text).value
        result["detail_type"] = detail_type
        result["turn_window"] = max(
            1,
            min(
                12,
                int(
                    result.get("turn_window")
                    or settings.NARRATIVE_DETAIL_TURN_WINDOW
                ),
            ),
        )
        result["visibility"] = result.get("visibility") or "public"
        if subject_entity_id:
            result["subject_entity_id"] = str(subject_entity_id)
        result["_memory"] = {
            "kind": MemoryKind.NARRATIVE_DETAIL.value,
            "classifier": "deterministic-v1",
        }
        return result

    @classmethod
    def _detail_type(cls, text: str) -> NarrativeDetailType:
        normalized = cls._normalize(text)
        if re.search(r"\b(взгляд|посмотр|глянул|отвел|отвела|gaze|glanc)\w*\b", normalized):
            return NarrativeDetailType.GAZE
        if re.search(r"\b(улыб|выражени|лиц|smil|expression)\w*\b", normalized):
            return NarrativeDetailType.EXPRESSION
        if re.search(r"\b(кив|жест|пожал|nod|shrug|gesture)\w*\b", normalized):
            return NarrativeDetailType.GESTURE
        if re.search(r"\b(поз[аеуы]|сидит|стоит|pose|posture)\w*\b", normalized):
            return NarrativeDetailType.POSE
        if re.search(r"\b(дожд|ветер|шум|свет|ambient|rain|wind)\w*\b", normalized):
            return NarrativeDetailType.AMBIENT
        if re.search(r"\b(запах|звук|вкус|холод|тепл|smell|sound|cold|warm)\w*\b", normalized):
            return NarrativeDetailType.SENSORY
        if re.search(r"\b(слева|справа|рядом|позади|у двери|left|right|behind|near)\b", normalized):
            return NarrativeDetailType.SPATIAL
        return NarrativeDetailType.OTHER

    @staticmethod
    def _resolve_subject(payload: dict, aliases: dict[str, str]) -> str | None:
        direct = payload.get("subject_entity_id")
        if direct:
            try:
                return str(UUID(str(direct)))
            except (ValueError, TypeError, AttributeError):
                return None
        subject = MemoryTaxonomyService._normalize(payload.get("subject"))
        return aliases.get(subject)

    @staticmethod
    def _proposal_text(payload: dict) -> str:
        canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
        return MemoryTaxonomyService._normalize(
            " ".join(
                str(value)
                for value in (
                    payload.get("subject"),
                    payload.get("predicate"),
                    payload.get("object_value"),
                    canon.get("description"),
                    canon.get("evidence"),
                )
                if value
            )
        )

    @staticmethod
    def _matches(patterns: tuple[str, ...], text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _normalize(value: object) -> str:
        return " ".join(str(value or "").casefold().split())
