from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionResponse(BaseModel):
    """One epistemic speech act, not an assertion of objective world truth."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "required": ["question_index", "disposition", "delivery", "words"],
    })

    question_index: int = Field(ge=0, le=7)
    disposition: Literal["answer", "unknown", "refuse", "deflect"]
    delivery: Literal["spoken", "nonverbal"] = Field(default="spoken", description="Spoken words or an observable nonverbal response; never mix both in words.")
    words: str = Field(min_length=2, max_length=1000, description="For spoken: actual utterance only, without speaker labels or stage directions. For nonverbal: observable response action, without invented speech.")


class RouteDirection(BaseModel):
    """A sourced NPC direction; it permits discovery, never proves access or truth."""

    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=2, max_length=255)
    via: list[str] = Field(default_factory=list, max_length=4)
    evidence: str = Field(min_length=2, max_length=1000)
    speech_index: int | None = Field(default=None, ge=0, le=7, description="Index of the approved speech fragment supplying this direction; evidence is bound by the engine.")


class AddressedResponse(BaseModel):
    """Preserve response ownership and question coverage across planning/rendering."""

    model_config = ConfigDict(extra="forbid")

    speaker_name: str | None = Field(default=None, max_length=120)
    speaker_id: UUID | None = None
    direct_response: str | None = Field(default=None, min_length=2, max_length=1000)
    after_action_index: int | None = Field(default=None, ge=0, le=7)
    revealed_name: str | None = Field(default=None, min_length=2, max_length=120)
    name_evidence: str | None = Field(default=None, max_length=500)
    questions: list[str] = Field(default_factory=list, max_length=8)
    answers: list[QuestionResponse] = Field(default_factory=list, max_length=8)
    route_directions: list[RouteDirection] = Field(default_factory=list, max_length=4)
    speaker_aliases: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_question_partition(self):
        indices = [answer.question_index for answer in self.answers]
        if len(indices) != len(set(indices)) or set(indices) != set(range(len(self.questions))):
            raise ValueError("addressed response must cover each question exactly once")
        self.answers.sort(key=lambda answer: answer.question_index)
        fragments = self.speech_fragments()
        for route in self.route_directions:
            if route.speech_index is not None:
                if route.speech_index >= len(fragments):
                    raise ValueError("route direction references nonexistent speech")
                route.evidence = fragments[route.speech_index]
        approved_speech = " ".join(fragments)
        if any(route.evidence not in approved_speech for route in self.route_directions):
            raise ValueError("route directions require exact evidence in approved NPC speech")
        if self.revealed_name and (
            not self.name_evidence
            or self.name_evidence not in " ".join(self.speech_fragments())
            or self.revealed_name not in self.name_evidence
        ):
            raise ValueError("name revelation requires exact self-identification in the approved reply")
        return self

    def speech_fragments(self) -> list[str]:
        # Indexed answers are authoritative; the legacy aggregate is only a compatibility path.
        return [answer.words for answer in self.answers if answer.delivery == "spoken"] if self.answers else (
            [self.direct_response] if self.direct_response else []
        )
