from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


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


class SessionZeroInterviewModelDecision(BaseModel):
    """Compact transport returned by the LLM for one interview turn."""

    assistant_message: str = Field(min_length=1)
    ready_to_finalize: bool = False
    patch: SessionZeroInterviewPatch = Field(
        default_factory=SessionZeroInterviewPatch
    )
    question_topics: list[str] = Field(default_factory=list)
    summary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, value: Any) -> Any:
        """Accept useful provider variants without advertising them in the schema."""
        if not isinstance(value, dict):
            return value

        converted = dict(value)
        if not converted.get("patch") and isinstance(converted.get("draft"), dict):
            converted["patch"] = converted.pop("draft")

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

        if not converted.get("assistant_message"):
            converted["assistant_message"] = (
                "Основа кампании собрана. Проверь итоговую сводку перед стартом."
                if converted.get("ready_to_finalize")
                else "Продолжим нулевую сессию."
            )
        return converted


class SessionZeroInterviewDecision(BaseModel):
    """Public decision returned by the service with the complete accumulated draft."""

    assistant_message: str = Field(min_length=1)
    ready_to_finalize: bool = False
    draft: SessionZeroInterviewDraft = Field(
        default_factory=SessionZeroInterviewDraft
    )
    missing_topics: list[str] = Field(default_factory=list)
    question_topics: list[str] = Field(default_factory=list)
    summary: str | None = None


class SessionZeroInterviewState(BaseModel):
    version: int = 5
    response_language: str = "ru"
    messages: list[dict[str, str]] = Field(default_factory=list)
    draft: SessionZeroInterviewDraft = Field(
        default_factory=SessionZeroInterviewDraft
    )
    pending_user_message: str | None = None
    last_summary: str | None = None
    last_question_topics: list[str] = Field(default_factory=list)
    delegated_fields: list[str] = Field(default_factory=list)
