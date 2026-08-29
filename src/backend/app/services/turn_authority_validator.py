from __future__ import annotations

import json
import re

from app.config import settings
from app.models.narration_validation import (
    NarrationValidationResult,
    NarrationViolation,
)
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.narration_validator import NarrationValidationError
from app.services.player_intent_contract import language_mismatch
from app.services.role_model_router import RoleModelRouter, RoleModelSelection


class TurnAuthorityValidator:
    """Semantic control-model gate over one typed TurnAuthority object.

    Deterministic code here is intentionally limited to machine-provable state/surface invariants.
    Meaning such as player agency, perception vs emotion, NPC ownership and movement paraphrase is
    judged by the model from the complete authority and candidate prose.
    """

    SYSTEM_PROMPT = """[TURN AUTHORITY VALIDATOR]
You are not a game master and you never continue the story. You receive one machine-readable
TURN AUTHORITY object and candidate prose. The authority object is the sole source of truth for
what this turn is allowed to establish.

Judge SEMANTICALLY from the whole sentence, grammatical subject and scene context. Never decide from
a word/stem whitelist or blacklist.

Return repair_required only for concrete violations:
- PLAYER AGENCY: prose assigns the human protagonist new voluntary dialogue, choices, decisions,
  plans, beliefs, consent, promises, attacks, thoughts, emotions, intentions or next actions beyond
  player_input. Physical realization of an action already completed by authority is allowed.
- PERCEPTION IS NOT INTERNAL AGENCY: immediate seeing, hearing, smell, taste, touch, temperature,
  pain, pressure, balance and other bodily/sensory perception may be narrated when grounded by the
  scene. Decide from meaning in context. Do not classify a phrase merely because it uses a verb such
  as "чувствовать".
- NPC OWNERSHIP: thoughts, emotions, facial expressions, gestures, posture, speech and local
  conversational behavior of a present/authorized NPC belong to that NPC, not to the protagonist.
- PRESENT NPC DIALOGUE: a present response actor may answer naturally from their perspective. Their
  sentences do not need to be prewritten in observable_consequences. Personal memories,
  observations, opinions, uncertainty, claims and lies are epistemic character claims, not objective
  canon merely because they contain new information. Never turn a legal answer into silence.
- CHARACTER PRESENCE: a known_absent_character physically acts/speaks/appears. Characters in
  present_characters, allowed_new_npcs and allowed_existing_npc_arrivals are authorized physically.
- UNPLANNED NPC: a genuinely new physical person appears without typed NPC authority.
- SCENE TEXTURE: neutral local sensory/furnishing detail is allowed when it does not create a new
  character, route, threat, clue, mechanically/causally significant object or action outcome.
- MOVEMENT/TIME: prose completes a physical location/time/focus change not authorized by structured
  authority. Distinguish a natural paraphrase/local body movement from a true scene transition by
  meaning, not vocabulary.
- OUTCOME: prose contradicts observable_consequences or completed structured execution.
- CURRENT TURN: prose answers/repeats a previous turn instead of current player_input/current result.
- COMPLICATION: prose invents a new threat/interruption/twist when allow_new_complication=false.
- LANGUAGE/SURFACE: final player-facing text must use the player's language and must not expose UUIDs,
  slugs, route/debug paths, TURN AUTHORITY, engine statuses or validator/meta commentary.

Do not reconstruct hidden campaign rules. Do not complain that an approved/present NPC was missing
from an older participant list. Do not change the approved outcome while repairing prose.
For EVERY error, evidence MUST quote the shortest exact offending fragment from candidate prose.
Evidence for player_agency MUST actually have the protagonist as semantic owner; never cite an
NPC-owned fragment as player agency. One bad span does not invalidate unrelated legal prose.
All human-readable fields must be Russian.

Return exactly:
{
  "verdict": "pass|repair_required",
  "summary": "short reason in Russian",
  "violations": [
    {
      "violation_type": "absent_character|absent_object|invalid_movement|invalid_time_advance|player_agency|ungrounded_complication|sequence_violation|canon_conflict|other",
      "severity": "warning|error",
      "evidence": "shortest exact candidate fragment",
      "correction": "specific prose-only correction in Russian"
    }
  ]
}
"""

    UUID_PATTERN = re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
    )
    TECHNICAL_TOKEN_PATTERN = re.compile(
        r"(?:\bturn[_ ]authority\b|\bsource_scene_id\b|\btarget_scene_id\b|"
        r"\bsource_location\b|\btarget_location\b|\broute_discovery\b|"
        r"\bplayer destination is not authorized\b|\bvalidator(?:_status)?\b|"
        r"\bnarration_validation\b|\bBLOCKED\b|\bSKIPPED\b|\bCOMPLETED\b|"
        r"\[[A-Z][A-Z _-]{5,}\]|\b[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}\b)",
        flags=re.IGNORECASE,
    )
    META_SURFACE_PATTERN = re.compile(
        r"(?:candidate\s+narration|engine\s+state|turn\s+authority|"
        r"validator\s+(?:status|result)|narration\s+validation)",
        flags=re.IGNORECASE,
    )

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()

    @property
    def telemetry(self) -> dict:
        return dict(self._provider.last_telemetry or {})

    async def validate(
        self,
        selection: RoleModelSelection,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        if not candidate_text.strip():
            return NarrationValidationResult.model_validate(
                {
                    "verdict": "repair_required",
                    "summary": "Нарратор вернул пустой текст.",
                    "violations": [
                        {
                            "violation_type": "other",
                            "severity": "error",
                            "evidence": "пустой ответ",
                            "correction": "Описать утверждённый исход хода художественным текстом.",
                        }
                    ],
                }
            )

        messages = [
            ChatMessage(role="system", content=self.SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    "[TURN AUTHORITY]\n"
                    + json.dumps(
                        authority.validator_payload(),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n\n[CANDIDATE NARRATION]\n"
                    + candidate_text
                ),
            ),
        ]
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                messages,
                max_tokens=min(settings.NARRATION_VALIDATOR_MAX_TOKENS, 700),
                temperature=0.0,
                response_model=NarrationValidationResult,
            )
            result = NarrationValidationResult.model_validate(data)
            result = self.apply_deterministic_authority(result, authority)
            result = self.apply_deterministic_language(result, authority, candidate_text)
            return self.apply_deterministic_surface_quality(result, candidate_text)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise NarrationValidationError(str(exc)) from exc

    @staticmethod
    def _append_error(
        result: NarrationValidationResult,
        violation: NarrationViolation,
        summary: str,
    ) -> NarrationValidationResult:
        if any(
            item.violation_type == violation.violation_type
            and item.severity == "error"
            and item.evidence == violation.evidence
            for item in result.violations
        ):
            return result
        return NarrationValidationResult(
            verdict="repair_required",
            summary=summary,
            violations=[*result.violations, violation],
        )

    @staticmethod
    def apply_deterministic_authority(
        result: NarrationValidationResult,
        authority: TurnAuthority,
    ) -> NarrationValidationResult:
        """Typed presence wins over a control-model absent-character hallucination."""
        protected = {
            value.casefold()
            for value in [
                authority.acting_character_name,
                *authority.present_character_names,
                *authority.allowed_new_npc_names,
                *authority.allowed_existing_npc_arrival_names,
            ]
            if value
        }
        if not protected:
            return result

        filtered = []
        removed = False
        for violation in result.violations:
            if violation.violation_type != "absent_character":
                filtered.append(violation)
                continue
            text = f"{violation.evidence} {violation.correction}".casefold()
            if any(name in text for name in protected):
                removed = True
                continue
            filtered.append(violation)

        if not removed:
            return result
        errors = [item for item in filtered if item.severity == "error"]
        return NarrationValidationResult(
            verdict="repair_required" if errors else "pass",
            summary=(
                result.summary
                if errors
                else "Типизированный TurnAuthority подтверждает присутствие этого персонажа."
            ),
            violations=filtered,
        )

    @classmethod
    def apply_deterministic_language(
        cls,
        result: NarrationValidationResult,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        if not language_mismatch(candidate_text, authority.player_input):
            return result
        return cls._append_error(
            result,
            NarrationViolation(
                violation_type="other",
                severity="error",
                evidence="Наррация сменила язык относительно русского ввода игрока.",
                correction=(
                    "Переписать внутриигровой ответ на русском языке, сохранив точные "
                    "канонические имена."
                ),
            ),
            "Детерминированная проверка обнаружила смену языка наррации.",
        )

    @classmethod
    def apply_deterministic_surface_quality(
        cls,
        result: NarrationValidationResult,
        candidate_text: str,
    ) -> NarrationValidationResult:
        """Keep unambiguously technical control-plane syntax out of player-facing prose."""
        evidence = None
        if cls.UUID_PATTERN.search(candidate_text):
            evidence = "Наррация содержит внутренний UUID."
        elif cls.TECHNICAL_TOKEN_PATTERN.search(candidate_text):
            evidence = "Наррация содержит технический идентификатор или статус движка."
        elif cls.META_SURFACE_PATTERN.search(candidate_text):
            evidence = "Наррация содержит явный служебный комментарий о движке/валидаторе."
        if evidence is None:
            return result
        return cls._append_error(
            result,
            NarrationViolation(
                violation_type="other",
                severity="error",
                evidence=evidence,
                correction="Удалить служебный текст и оставить только внутриигровую прозу.",
            ),
            "Детерминированная проверка обнаружила технический текст.",
        )

    # Backward-compatible entry points intentionally contain no semantic inference. They remain so
    # older callers/tests cannot silently reactivate word-list authority.
    @classmethod
    def apply_deterministic_movement_surface(
        cls,
        result: NarrationValidationResult,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        del cls, authority, candidate_text
        return result

    @classmethod
    def apply_deterministic_player_agency(
        cls,
        result: NarrationValidationResult,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        del cls, authority, candidate_text
        return result

    @classmethod
    def apply_deterministic_actor_agency(
        cls,
        result: NarrationValidationResult,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        del cls, authority, candidate_text
        return result

    @staticmethod
    def repair_prompt(
        authority: TurnAuthority,
        candidate: str,
        result: NarrationValidationResult,
    ) -> str:
        violations = "\n".join(
            f"- {item.violation_type}: {item.evidence} -> {item.correction}"
            for item in result.violations
            if item.severity == "error"
        )
        return (
            "[REPAIR REJECTED NARRATION]\n"
            "[MINIMAL EDIT AGAINST TURN AUTHORITY]\n"
            "Отредактируй отвергнутый текст МИНИМАЛЬНО. Сохрани все предложения, абзацы, "
            "реплики присутствующих NPC, конкретные детали и утверждённые последствия, которые "
            "не перечислены ниже как нарушения. Не пересочиняй ответ с нуля и не сокращай его до "
            "служебной заглушки. Удали или перепиши только конкретные offending spans.\n\n"
            "Критически важно:\n"
            "- не заменяй легальную реплику NPC на молчание;\n"
            "- естественная формулировка уже выполненного действия допустима, но не добавляй "
            "следующий шаг или новый результат;\n"
            "- не добавляй мысли, эмоции, решения, планы, согласие или новые реплики героя;\n"
            "- непосредственное физическое/сенсорное восприятие не является автоматически "
            "внутренней эмоцией: сохраняй его, если оно не нарушает Authority;\n"
            "- мысли, эмоции, жесты и речь NPC принадлежат NPC, а не герою;\n"
            "- не добавляй новый физический NPC, маршрут, угрозу, clue или причинно значимый объект;\n"
            "- нейтральная сенсорная фактура сцены допустима;\n"
            "- верни только цельную естественную художественную прозу на русском языке.\n\n"
            "AUTHORITY:\n"
            + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
            + "\n\nТОЧНЫЕ НАРУШЕНИЯ, КОТОРЫЕ НУЖНО ИСПРАВИТЬ:\n"
            + (violations or result.summary)
            + "\n\n[REJECTED CANDIDATE — EDIT IN PLACE]\n"
            + candidate
        )


__all__ = ["TurnAuthorityValidator"]
