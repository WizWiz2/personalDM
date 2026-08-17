from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SessionZeroStarterNPC(BaseModel):
    """NPC explicitly established by Session Zero as physically present at game start.

    This is a presence contract, not a generic cast list. Characters who merely exist in the
    premise, must be found later, or are only mentioned as background must not be included.
    """

    role: str = Field(min_length=1, max_length=160)
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=600)
    reason: str | None = Field(default=None, max_length=400)
    present_at_start: bool = True

    @staticmethod
    def _explicit_name_from_text(role: str, *values: str | None) -> str | None:
        """Recover only an explicitly written proper name from structured starter text.

        Local models sometimes preserve `Ирина` in description/reason but omit the dedicated name
        field. Bootstrap previously promoted the role (`Судебный фотограф`) to canonical identity,
        which later let TurnAuthority create a second `Ирина`. This parser is intentionally narrow:
        it accepts explicit name cues, `role + Name`, or a proper name before a dash/colon. It never
        invents a name from a profession or free-form semantic similarity.
        """
        role_pattern = re.escape(" ".join(role.split())).replace(r"\ ", r"\s+")
        patterns = [
            re.compile(
                r"(?:по\s+имени|е[её]\s+зовут|его\s+зовут|зовут|имя\s*[:—-]?)\s+"
                r"([А-ЯЁ][а-яё-]{2,}(?:\s+[А-ЯЁ][а-яё-]{2,}){0,2})\b"
            ),
            re.compile(
                rf"\b{role_pattern}\s+"
                r"([А-ЯЁ][а-яё-]{2,}(?:\s+[А-ЯЁ][а-яё-]{2,}){0,2})\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"^\s*([А-ЯЁ][а-яё-]{2,}(?:\s+[А-ЯЁ][а-яё-]{2,}){0,1})\s*(?:—|-|:)"
            ),
        ]
        role_folded = " ".join(role.casefold().split())
        for value in values:
            text = " ".join(str(value or "").split()).strip()
            if not text:
                continue
            for pattern in patterns:
                match = pattern.search(text)
                if not match:
                    continue
                candidate = " ".join(match.group(1).split()).strip(" ,.;:—-")
                if candidate and candidate.casefold() != role_folded:
                    return candidate
        return None

    @model_validator(mode="after")
    def preserve_explicit_name_from_structured_text(self) -> SessionZeroStarterNPC:
        if self.name or not self.present_at_start:
            return self
        explicit = self._explicit_name_from_text(
            self.role,
            self.description,
            self.reason,
        )
        if explicit:
            self.name = explicit
        return self


class SessionZeroWorldDraft(BaseModel):
    setting_name: str | None = None
    genre: str | None = None
    premise: str | None = None
    tone: str | None = None
    themes: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    boundaries_confirmed: bool = False
    rules_system: str | None = None
    world_summary: str | None = None
    play_style: str | None = None
    narrative_style: str | None = None
    content_rating: str | None = None
    starting_location_name: str | None = None
    starting_situation: str | None = None
    starting_scene_title: str | None = None
    starter_npcs: list[SessionZeroStarterNPC] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Complete list of NPCs physically present in the opening scene. Exclude missing, future, "
            "background-only or merely mentioned people."
        ),
    )
    starter_presence_confirmed: bool = False


class SessionZeroCharacterDraft(BaseModel):
    name: str | None = None
    description: str | None = None
    appearance: str | None = None
    personality: str | None = None
    values: list[str] = Field(default_factory=list)
    fears: list[str] = Field(default_factory=list)
    desires: list[str] = Field(default_factory=list)
    voice: str | None = None
    speech_patterns: str | None = None
    biography: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    first_goal: str | None = None


class SessionZeroInterviewDraft(BaseModel):
    world: SessionZeroWorldDraft = Field(default_factory=SessionZeroWorldDraft)
    character: SessionZeroCharacterDraft = Field(
        default_factory=SessionZeroCharacterDraft
    )


class SessionZeroWorldPatch(BaseModel):
    setting_name: str | None = None
    genre: str | None = None
    premise: str | None = None
    tone: str | None = None
    themes: list[str] | None = None
    boundaries: list[str] | None = None
    boundaries_confirmed: bool | None = None
    rules_system: str | None = None
    world_summary: str | None = None
    play_style: str | None = None
    narrative_style: str | None = None
    content_rating: str | None = None
    starting_location_name: str | None = None
    starting_situation: str | None = None
    starting_scene_title: str | None = None
    starter_npcs: list[SessionZeroStarterNPC] | None = None
    starter_presence_confirmed: bool | None = None


class SessionZeroCharacterPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    appearance: str | None = None
    personality: str | None = None
    values: list[str] | None = None
    fears: list[str] | None = None
    desires: list[str] | None = None
    voice: str | None = None
    speech_patterns: str | None = None
    biography: str | None = None
    capabilities: list[str] | None = None
    limitations: list[str] | None = None
    first_goal: str | None = None


class SessionZeroInterviewPatch(BaseModel):
    world: SessionZeroWorldPatch = Field(default_factory=SessionZeroWorldPatch)
    character: SessionZeroCharacterPatch = Field(
        default_factory=SessionZeroCharacterPatch
    )


class SessionZeroAgentToolCall(BaseModel):
    """Provider-neutral tool call emitted by the Session Zero agent."""

    name: Literal["update_session_zero", "finalize_session_zero"]
    patch: SessionZeroInterviewPatch | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_tool_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        converted = dict(value)
        name = str(converted.get("name") or converted.get("tool") or "")
        aliases = {
            "update": "update_session_zero",
            "update_session": "update_session_zero",
            "save_session_zero": "update_session_zero",
            "finalize": "finalize_session_zero",
            "complete_session_zero": "finalize_session_zero",
        }
        converted["name"] = aliases.get(name, name)
        arguments = converted.get("arguments")
        if converted.get("patch") is None and isinstance(arguments, dict):
            nested = arguments.get("patch")
            converted["patch"] = nested if isinstance(nested, dict) else arguments
        return converted

    @model_validator(mode="after")
    def ensure_update_patch(self) -> SessionZeroAgentToolCall:
        if self.name == "update_session_zero" and self.patch is None:
            self.patch = SessionZeroInterviewPatch()
        return self


class SessionZeroInterviewModelDecision(BaseModel):
    """One natural agent reply plus hidden state-management tool calls."""

    assistant_message: str = Field(min_length=1)
    tool_calls: list[SessionZeroAgentToolCall] = Field(default_factory=list)
    question_topics: list[str] = Field(default_factory=list)
    summary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value: Any) -> Any:
        """Accept older patch snapshots and common provider aliases."""
        if not isinstance(value, dict):
            return value

        converted = dict(value)
        assistant_message = converted.get("assistant_message")
        if not isinstance(assistant_message, str) or not assistant_message.strip():
            for alias in (
                "question",
                "next_question",
                "message",
                "reply",
                "assistant_response",
                "response",
            ):
                candidate = converted.get(alias)
                if isinstance(candidate, str) and candidate.strip():
                    converted["assistant_message"] = candidate.strip()
                    break
        elif assistant_message.strip() != assistant_message:
            converted["assistant_message"] = assistant_message.strip()

        calls = list(converted.get("tool_calls") or converted.get("actions") or [])
        legacy_patch = converted.get("patch")
        if not isinstance(legacy_patch, dict) and isinstance(converted.get("draft"), dict):
            legacy_patch = converted.get("draft")
        if isinstance(legacy_patch, dict):
            calls.append({"name": "update_session_zero", "patch": legacy_patch})
        if converted.get("ready_to_finalize"):
            calls.append({"name": "finalize_session_zero"})
        converted["tool_calls"] = calls

        normalized_message = converted.get("assistant_message")
        if not isinstance(normalized_message, str) or not normalized_message.strip():
            converted["assistant_message"] = (
                "Основа кампании собрана. Проверь итоговую сводку перед стартом."
                if any(
                    isinstance(item, dict)
                    and (item.get("name") or item.get("tool"))
                    in {"finalize_session_zero", "finalize"}
                    for item in calls
                )
                else (
                    "Я сохранил твой ответ. Что тебе хочется определить дальше — "
                    "героя, мир или стартовую ситуацию?"
                )
            )
        return converted

    @model_validator(mode="after")
    def keep_conversation_open_until_finalize(self) -> SessionZeroInterviewModelDecision:
        """A Session Zero DM must keep leading until it explicitly starts the game."""
        if self.ready_to_finalize:
            return self
        message = self.assistant_message.strip()
        if "?" not in message:
            separator = " " if message else ""
            self.assistant_message = (
                f"{message}{separator}Что тебе хочется добавить или уточнить дальше — "
                "про героя, мир или начало приключения?"
            )
        return self

    @property
    def patch(self) -> SessionZeroInterviewPatch:
        """Compatibility view for older callers and fixtures."""
        for call in self.tool_calls:
            if call.name == "update_session_zero" and call.patch is not None:
                return call.patch
        return SessionZeroInterviewPatch()

    @property
    def ready_to_finalize(self) -> bool:
        """Compatibility view for older callers and fixtures."""
        return any(call.name == "finalize_session_zero" for call in self.tool_calls)


class SessionZeroInterviewDecision(BaseModel):
    """Public decision returned by the service with the accumulated full card."""

    assistant_message: str = Field(min_length=1)
    ready_to_finalize: bool = False
    draft: SessionZeroInterviewDraft = Field(
        default_factory=SessionZeroInterviewDraft
    )
    missing_topics: list[str] = Field(default_factory=list)
    question_topics: list[str] = Field(default_factory=list)
    summary: str | None = None


class SessionZeroInterviewState(BaseModel):
    version: int = 9
    response_language: str = "ru"
    messages: list[dict[str, str]] = Field(default_factory=list)
    draft: SessionZeroInterviewDraft = Field(
        default_factory=SessionZeroInterviewDraft
    )
    pending_user_message: str | None = None
    last_summary: str | None = None
    last_question_topics: list[str] = Field(default_factory=list)
    delegated_fields: list[str] = Field(default_factory=list)
