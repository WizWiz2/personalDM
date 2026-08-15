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
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.player_intent_contract import (
    language_mismatch,
    unauthorized_player_speech,
    unresolved_player_completion,
)
from app.services.role_model_router import RoleModelRouter, RoleModelSelection


class TurnAuthorityValidator:
    """Small control-model gate that judges prose against one typed authority object."""

    SYSTEM_PROMPT = """[TURN AUTHORITY VALIDATOR]
You are not a game master and you never continue the story. You receive one machine-readable
TURN AUTHORITY object and candidate prose. The authority object is the sole source of truth for
what this turn is allowed to establish.

Return repair_required only for concrete violations of that authority:
- PLAYER AGENCY: the prose adds voluntary protagonist dialogue, gestures, movement, choices,
  decisions, plans, beliefs, consent, promises, attacks or emotions that are not already contained
  in player_input. Merely describing perception or externally caused involuntary effects is allowed.
- CHARACTER PRESENCE: a known_absent_character acts/speaks/appears physically. Characters listed
  in present_characters are present. Characters listed in allowed_new_npcs are explicitly approved
  first-time introductions and MUST NOT be rejected as absent.
- UNPLANNED NPC: a genuinely new character not listed in allowed_new_npcs is introduced as present.
- MOVEMENT/TIME: prose completes a location/time/focus change not authorized by scene_disposition,
  transition_type or action_sequence.
- OUTCOME: prose contradicts approved observable_consequences or a completed structured sequence.
- CURRENT TURN: prose answers or repeats a previous turn instead of the current player_input and
  current observable_consequences.
- LANGUAGE: when player_input is Russian, final narration must be Russian. Established canonical
  names may remain exact, but Chinese/English prose or translated character names are not valid.
- SURFACE: player-facing prose must not expose UUIDs, slugs, route/debug paths, TURN AUTHORITY,
  BLOCKED/SKIPPED/COMPLETED labels, validator diagnostics, or meta commentary about the response,
  narration, engine, player input, or waiting for the player's next message.
- COMPLICATION: prose invents a new threat/interruption/twist when allow_new_complication=false.

Do not reconstruct hidden campaign rules. Do not complain that an approved new NPC was not in the
old participant list. Do not invent corrections that change the approved turn outcome.
For EVERY error, evidence MUST quote the shortest exact offending fragment from candidate prose.
Do not paraphrase evidence: deterministic actor-response publication may remove that exact segment.
All human-readable fields (summary, evidence, correction) MUST be written in Russian even if the
candidate or your internal reasoning uses another language.

Return exactly:
{
  "verdict": "pass|repair_required",
  "summary": "short reason in Russian",
  "violations": [
    {
      "violation_type": "absent_character|absent_object|invalid_movement|invalid_time_advance|player_agency|ungrounded_complication|sequence_violation|canon_conflict|other",
      "severity": "warning|error",
      "evidence": "exact candidate fragment in Russian",
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
        r"(?:ответ\s+заканчива(?:ется|ет)|"
        r"жд[её]т\s+дальнейших\s+(?:слов|действий)\s+игрока|"
        r"игрок\s+(?:должен|может|теперь)|"
        r"следующ(?:ий|ую|ее)\s+(?:ход|реплик|действ).*игрок|"
        r"player\s+(?:must|should|can|input)|"
        r"waits?\s+for\s+(?:the\s+)?player|"
        r"candidate\s+narration|engine\s+state)",
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
            result = self.apply_deterministic_language(
                result,
                authority,
                candidate_text,
            )
            result = self.apply_deterministic_surface_quality(
                result,
                candidate_text,
            )
            return self.apply_deterministic_player_agency(
                result,
                authority,
                candidate_text,
            )
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
        """Never let a control-model hallucination overrule typed presence authority."""
        protected = {
            value.casefold()
            for value in [
                authority.acting_character_name,
                *authority.allowed_new_npc_names,
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
                else "Типизированный TurnAuthority подтверждает этого собеседника или новое появление."
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
        """Weak multilingual models may drift scripts even after the validator says pass."""
        if not language_mismatch(candidate_text, authority.player_input):
            return result
        return cls._append_error(
            result,
            NarrationViolation(
                violation_type="other",
                severity="error",
                evidence="Наррация сменила язык относительно русского ввода игрока.",
                correction=(
                    "Переписать весь внутриигровой ответ на русском языке, сохранив точные "
                    "канонические имена без перевода или переименования."
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
        """Keep debug/control-plane language out of text shown to the player."""
        evidence = None
        if cls.UUID_PATTERN.search(candidate_text):
            evidence = "Наррация содержит внутренний UUID."
        elif cls.TECHNICAL_TOKEN_PATTERN.search(candidate_text):
            evidence = "Наррация содержит технический идентификатор или статус движка."
        elif cls.META_SURFACE_PATTERN.search(candidate_text):
            evidence = "Наррация описывает ответ/игрока как элементы интерфейса вместо мира игры."
        if evidence is None:
            return result
        return cls._append_error(
            result,
            NarrationViolation(
                violation_type="other",
                severity="error",
                evidence=evidence,
                correction=(
                    "Удалить служебный или мета-текст и выразить только подтверждённое событие "
                    "внутриигровой прозой без внутренних идентификаторов."
                ),
            ),
            "Детерминированная проверка обнаружила технический или мета-текст.",
        )

    @classmethod
    def apply_deterministic_player_agency(
        cls,
        result: NarrationValidationResult,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        """Protect unresolved choices and new protagonist speech independently of the LLM judge."""
        if any(
            item.violation_type == "player_agency" and item.severity == "error"
            for item in result.violations
        ):
            return result

        if unauthorized_player_speech(
            candidate_text,
            player_input=authority.player_input,
            player_name=authority.player_character_name,
        ):
            result = cls._append_error(
                result,
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence=(
                        f"Нарратор придумал новую прямую реплику героя "
                        f"{authority.player_character_name or 'игрока'}."
                    ),
                    correction=(
                        "Удалить придуманную реплику протагониста; его слова задаёт только человек."
                    ),
                ),
                "Детерминированная проверка обнаружила придуманную реплику героя.",
            )

        if unresolved_player_completion(
            candidate_text,
            player_input=authority.player_input,
            player_name=authority.player_character_name,
        ):
            result = cls._append_error(
                result,
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence=(
                        "Нарратор завершил одну из альтернатив, которую игрок оставил нерешённой."
                    ),
                    correction=(
                        "Оставить выбор открытым и описать только уже совершённое действие или "
                        "внешнюю реакцию мира."
                    ),
                ),
                "Детерминированная проверка обнаружила самовольное завершение выбора игрока.",
            )

        if authority.scene_disposition != "actor_turn" or not authority.player_character_name:
            return result

        # Actor output gets an additional broad deterministic scrub. Small local models often append
        # a protagonist reaction after an otherwise useful NPC reply and then approve themselves.
        sanitized, diagnostics = NarrationPublicationGuard.publish(
            authority,
            candidate_text,
            None,
        )
        if (
            diagnostics.get("mode") == "sanitized_candidate"
            and " ".join(sanitized.split()) == " ".join(candidate_text.split())
        ):
            return result

        return cls._append_error(
            result,
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence=(
                    f"Ответ за {authority.acting_character_name or 'NPC'} содержит новую реплику "
                    f"или самостоятельное действие героя {authority.player_character_name}."
                ),
                correction=(
                    f"Оставить только ответ и действия {authority.acting_character_name or 'NPC'}; "
                    f"следующую реплику или действие {authority.player_character_name} вводит человек."
                ),
            ),
            "Детерминированная проверка обнаружила управление героем в ответе NPC.",
        )

    @classmethod
    def apply_deterministic_actor_agency(
        cls,
        result: NarrationValidationResult,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        """Backward-compatible entry point for actor-agency regression tests."""
        return cls.apply_deterministic_player_agency(result, authority, candidate_text)

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
            "[REGENERATE NARRATION FROM TURN AUTHORITY]\n"
            "Напиши новый ответ С НУЛЯ. Не редактируй и не продолжай отвергнутый текст: он "
            "намеренно не передан, чтобы не копировать его ошибки. Используй только AUTHORITY ниже. "
            "Дай один законченный внутриигровой ответ на русском языке. Начни с реакции мира, NPC "
            "или наблюдаемого результата текущего действия. Не пересказывай действие игрока и не "
            "продолжай его добровольными действиями, мыслями, решениями, эмоциями или репликами. "
            "Если нужно описать непосредственное восприятие или внешний эффект для героя, обращайся "
            "во втором лице. Не используй имя героя как субъект нового действия. Не добавляй NPC, "
            "поворот, перемещение или время сверх Authority. Не выводи UUID, slugs, маршруты, "
            "BLOCKED/SKIPPED, поля authority/validator или мета-фразы. Верни только естественную "
            "художественную прозу.\n\n"
            "AUTHORITY:\n"
            + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
            + "\n\nОШИБКИ ПРЕДЫДУЩЕЙ ПОПЫТКИ, КОТОРЫЕ НЕЛЬЗЯ ПОВТОРЯТЬ:\n"
            + (violations or result.summary)
            + "\n\n[REJECTED CANDIDATE OMITTED]\n"
            "Исходный отвергнутый текст намеренно не передаётся модели."
        )


__all__ = ["TurnAuthorityValidator"]
