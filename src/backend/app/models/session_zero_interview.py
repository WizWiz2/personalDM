from __future__ import annotations

from pydantic import BaseModel, Field


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


class SessionZeroInterviewDecision(BaseModel):
    assistant_message: str = Field(min_length=1)
    ready_to_finalize: bool = False
    draft: SessionZeroInterviewDraft = Field(
        default_factory=SessionZeroInterviewDraft
    )
    missing_topics: list[str] = Field(default_factory=list)
    summary: str | None = None


class SessionZeroInterviewState(BaseModel):
    version: int = 3
    messages: list[dict[str, str]] = Field(default_factory=list)
    draft: SessionZeroInterviewDraft = Field(
        default_factory=SessionZeroInterviewDraft
    )
    pending_user_message: str | None = None
    last_summary: str | None = None
