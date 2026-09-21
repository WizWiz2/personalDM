"""Selectable Game Master personas and closed director-move catalog."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DirectorMove = Literal[
    "quiet",
    "advance_conflict",
    "introduce_contact",
    "npc_initiative",
    "harden_consequence",
    "intrigue_reveal",
    "soften_blow",
    "escalate_chaos",
]

DIRECTOR_MOVES: tuple[DirectorMove, ...] = (
    "quiet",
    "advance_conflict",
    "introduce_contact",
    "npc_initiative",
    "harden_consequence",
    "intrigue_reveal",
    "soften_blow",
    "escalate_chaos",
)

DIRECTOR_MOVE_LABELS_RU: dict[DirectorMove, str] = {
    "quiet": "Тихий атмосферный такт",
    "advance_conflict": "Продвинуть конфликт",
    "introduce_contact": "Ввести контакт",
    "npc_initiative": "Инициатива NPC",
    "harden_consequence": "Ужесточить цену",
    "intrigue_reveal": "Раскрытие интриги",
    "soften_blow": "Смягчить удар",
    "escalate_chaos": "Эскалация хаоса",
}


class MovePolicy(BaseModel):
    """Relative weights over the closed director-move catalog."""

    quiet: float = 1.0
    advance_conflict: float = 1.0
    introduce_contact: float = 1.0
    npc_initiative: float = 1.0
    harden_consequence: float = 1.0
    intrigue_reveal: float = 1.0
    soften_blow: float = 1.0
    escalate_chaos: float = 1.0

    @field_validator("*", mode="before")
    @classmethod
    def _non_negative(cls, value: object) -> float:
        number = float(value)  # type: ignore[arg-type]
        if number < 0:
            raise ValueError("move weights must be non-negative")
        return number

    def as_mapping(self) -> dict[DirectorMove, float]:
        return {move: float(getattr(self, move)) for move in DIRECTOR_MOVES}


class MasterRhythmState(BaseModel):
    """Lightweight deterministic counters — no LLM interpreter."""

    turn_index: int = 0
    turns_since_pressure: int = 0
    turns_since_quiet: int = 0


class GameMasterPersona(BaseModel):
    """Shared persona fields for presets and custom masters."""

    id: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=2, max_length=120)
    blurb: str = Field(min_length=8, max_length=400)
    brief: str = Field(min_length=40, max_length=4000)
    voice_style: str = Field(min_length=8, max_length=800)
    catchphrases: list[str] = Field(min_length=1, max_length=5)
    portrait_pixel: str = Field(min_length=8, max_length=200)
    portrait_engraving: str = Field(min_length=8, max_length=200)
    move_policy: MovePolicy
    gender: Literal["male", "female", "neutral"] = "neutral"
    is_preset: bool = True
    base_preset_id: str | None = None

    @field_validator("catchphrases")
    @classmethod
    def _clean_phrases(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value if " ".join(item.split())]
        if not cleaned:
            raise ValueError("at least one catchphrase is required")
        return cleaned[:5]


class CustomMasterInput(BaseModel):
    """User-authored master for MVP."""

    display_name: str = Field(min_length=2, max_length=120)
    blurb: str = Field(min_length=8, max_length=400)
    brief: str = Field(min_length=40, max_length=4000)
    voice_style: str = Field(min_length=8, max_length=800)
    catchphrases: list[str] = Field(min_length=1, max_length=5)
    base_preset_id: str | None = None
    move_policy: MovePolicy | None = None
    portrait_pixel: str | None = None
    portrait_engraving: str | None = None
    gender: Literal["male", "female", "neutral"] = "neutral"

    @field_validator("catchphrases")
    @classmethod
    def _clean_phrases(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value if " ".join(item.split())]
        if not cleaned:
            raise ValueError("at least one catchphrase is required")
        return cleaned[:5]


class CampaignMasterState(BaseModel):
    """Persisted under campaign_setup.custom_fields['game_master']."""

    kind: Literal["preset", "custom"] = "preset"
    preset_id: str | None = "iron_chronicler"
    custom: GameMasterPersona | None = None
    rhythm: MasterRhythmState = Field(default_factory=MasterRhythmState)

    @model_validator(mode="after")
    def _consistent(self) -> CampaignMasterState:
        if self.kind == "preset":
            if not self.preset_id:
                self.preset_id = "iron_chronicler"
            self.custom = None
        elif self.custom is None:
            raise ValueError("custom master requires persona payload")
        return self


class DirectorMoveSelection(BaseModel):
    moves: list[DirectorMove] = Field(min_length=1, max_length=2)
    obligations: list[str] = Field(default_factory=list)
    forced_introduce_contact: bool = False
    weights_used: dict[str, float] = Field(default_factory=dict)
    master_id: str
    master_display_name: str


class SetCampaignMasterRequest(BaseModel):
    kind: Literal["preset", "custom"] = "preset"
    preset_id: str | None = None
    custom: CustomMasterInput | None = None

    @model_validator(mode="after")
    def _require_payload(self) -> SetCampaignMasterRequest:
        if self.kind == "preset" and not self.preset_id:
            raise ValueError("preset_id is required for preset masters")
        if self.kind == "custom" and self.custom is None:
            raise ValueError("custom payload is required for custom masters")
        return self


class CampaignMasterRead(BaseModel):
    campaign_id: str
    state: CampaignMasterState
    resolved: GameMasterPersona
