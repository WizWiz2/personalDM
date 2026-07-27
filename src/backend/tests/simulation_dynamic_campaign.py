from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.models.scene_thesis import ThesisType
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection

try:
    from .simulation_scenario import (
        NpcConcept,
        ObjectiveCriterion,
        ScenarioPhase,
        ScenarioPulse,
        ThesisSeed,
    )
except ImportError:
    from simulation_scenario import (
        NpcConcept,
        ObjectiveCriterion,
        ScenarioPhase,
        ScenarioPulse,
        ThesisSeed,
    )


ChangeTypeName = Literal[
    "fact",
    "event",
    "relationship",
    "movement",
    "knowledge",
    "item_transfer",
]


def _require_russian_text(value: str, *, field_name: str) -> None:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        raise ValueError(f"{field_name} must contain readable Russian text")
    cyrillic = 0
    for char in letters:
        script_name = unicodedata.name(char, "")
        if "CYRILLIC" in script_name:
            cyrillic += 1
        elif "LATIN" not in script_name:
            raise ValueError(
                f"{field_name} contains a non-Russian script character {char!r}"
            )
    if cyrillic / len(letters) < 0.55:
        raise ValueError(f"{field_name} must be predominantly Russian")
    for token in "".join(char if char.isalpha() else " " for char in value).split():
        scripts = {
            "cyrillic" if "CYRILLIC" in unicodedata.name(char, "") else "latin"
            for char in token
            if "CYRILLIC" in unicodedata.name(char, "")
            or "LATIN" in unicodedata.name(char, "")
        }
        if len(scripts) > 1:
            raise ValueError(f"{field_name} contains mixed-script token {token!r}")


class GeneratedCriterion(BaseModel):
    key: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=8, max_length=320)
    allowed_change_types: list[ChangeTypeName] = Field(min_length=1, max_length=4)

    @field_validator("allowed_change_types")
    @classmethod
    def unique_change_types(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class GeneratedThesis(BaseModel):
    thesis_type: ThesisType
    text: str = Field(min_length=8, max_length=420)
    priority: int = Field(default=5, ge=0, le=10)
    visibility: Literal["public", "dm", "character_only"] = "public"
    related_names: list[str] = Field(default_factory=list, max_length=6)


class GeneratedPulse(BaseModel):
    at_fraction: float = Field(ge=0.0, le=1.0)
    event: str = Field(min_length=8, max_length=420)
    thesis: GeneratedThesis

    @field_validator("at_fraction")
    @classmethod
    def keep_pulse_inside_scene(cls, value: float) -> float:
        return min(0.95, max(0.06, value))


class GeneratedNpc(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    concept: str = Field(min_length=60, max_length=500)
    campaign_role: str = Field(min_length=15, max_length=220)
    tone: str = Field(min_length=10, max_length=220)


class GeneratedPhase(BaseModel):
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=4, max_length=140)
    location_description: str = Field(min_length=20, max_length=700)
    mood: str = Field(min_length=3, max_length=120)
    tension: str = Field(min_length=3, max_length=120)
    objective: str = Field(min_length=12, max_length=500)
    introduced_npcs: list[str] = Field(default_factory=list, max_length=5)
    active_npcs: list[str] = Field(min_length=1, max_length=6)
    opening_theses: list[GeneratedThesis] = Field(min_length=2, max_length=6)
    pulses: list[GeneratedPulse] = Field(min_length=2, max_length=4)
    director_note: str = Field(min_length=10, max_length=500)
    completion_criteria: list[GeneratedCriterion] = Field(min_length=2, max_length=4)
    min_turns: int = Field(default=6, ge=4, le=16)
    max_turns: int = Field(default=18, ge=10, le=30)

    @field_validator("tension", mode="before")
    @classmethod
    def normalize_numeric_tension(cls, value: object) -> str:
        text = str(value).strip()
        if text and not any(char.isalpha() for char in text):
            return f"Напряжение: {text}"
        if (
            len([char for char in text if char.isalpha()]) < 8
            or re.fullmatch(r"(?i)напряжение\s*:\s*[—–-]+", text)
        ):
            raise ValueError("tension must describe a concrete dramatic pressure")
        return text

    @model_validator(mode="after")
    def validate_turn_window(self):
        self.max_turns = max(self.max_turns, self.min_turns + 4)
        keys = [item.key for item in self.completion_criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("completion criterion keys must be unique within a phase")
        return self


class GeneratedArc(BaseModel):
    arc_title: str = Field(min_length=4, max_length=180)
    premise: str = Field(min_length=30, max_length=900)
    terminal: bool = False
    npcs: list[GeneratedNpc] = Field(min_length=2, max_length=12)
    phases: list[GeneratedPhase] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def validate_references(self):
        names = [item.name for item in self.npcs]
        if len(names) != len({name.casefold() for name in names}):
            raise ValueError("NPC names must be unique within an arc")
        known = {name.casefold() for name in names}
        for phase in self.phases:
            missing = [name for name in phase.introduced_npcs if name.casefold() not in known]
            if missing:
                raise ValueError(f"introduced NPCs are missing from arc payload: {missing}")
        russian_fields: list[tuple[str, str]] = [
            ("arc_title", self.arc_title),
            ("premise", self.premise),
        ]
        for npc_index, npc in enumerate(self.npcs):
            russian_fields.extend(
                [
                    (f"npcs[{npc_index}].name", npc.name),
                    (f"npcs[{npc_index}].concept", npc.concept),
                    (f"npcs[{npc_index}].campaign_role", npc.campaign_role),
                    (f"npcs[{npc_index}].tone", npc.tone),
                ]
            )
        for phase_index, phase in enumerate(self.phases):
            prefix = f"phases[{phase_index}]"
            russian_fields.extend(
                [
                    (f"{prefix}.title", phase.title),
                    (f"{prefix}.location_description", phase.location_description),
                    (f"{prefix}.mood", phase.mood),
                    (f"{prefix}.tension", phase.tension),
                    (f"{prefix}.objective", phase.objective),
                    (f"{prefix}.director_note", phase.director_note),
                ]
            )
            russian_fields.extend(
                (f"{prefix}.opening_theses[{index}].text", thesis.text)
                for index, thesis in enumerate(phase.opening_theses)
            )
            for pulse_index, pulse in enumerate(phase.pulses):
                russian_fields.extend(
                    [
                        (f"{prefix}.pulses[{pulse_index}].event", pulse.event),
                        (
                            f"{prefix}.pulses[{pulse_index}].thesis.text",
                            pulse.thesis.text,
                        ),
                    ]
                )
            russian_fields.extend(
                (f"{prefix}.completion_criteria[{index}].description", criterion.description)
                for index, criterion in enumerate(phase.completion_criteria)
            )
        for field_name, value in russian_fields:
            _require_russian_text(value, field_name=field_name)
            if re.search(
                r"\b(?:анализатор\w*\s+сред\w*|спектральн\w+\s+анализ\w*|"
                r"электромагнитн\w*|реактор\w*|плазм\w*|радиоактив\w*|"
                r"генетическ\w*|компьютер\w*|лабораторн\w+\s+халат\w*|"
                r"клеточн\w+\s+уровн\w*)\b",
                value,
                re.IGNORECASE,
            ):
                raise ValueError(
                    f"{field_name} introduces modern or science-fiction genre drift"
                )
        return self


class CampaignCatalog(BaseModel):
    format: Literal["personal-dm-generated-simulation"] = "personal-dm-generated-simulation"
    version: int = 1
    seed: str
    arcs: list[GeneratedArc] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> CampaignCatalog | None:
        if not path.exists():
            return None
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        temp.replace(path)

    @property
    def npc_names(self) -> set[str]:
        return {npc.name.casefold() for arc in self.arcs for npc in arc.npcs}

    @property
    def phase_slugs(self) -> set[str]:
        return {phase.slug for arc in self.arcs for phase in arc.phases}

    @property
    def phase_titles(self) -> set[str]:
        return {phase.title.casefold() for arc in self.arcs for phase in arc.phases}

    def canonical_npc_names(self) -> dict[str, str]:
        return {
            npc.name.casefold(): npc.name
            for arc in self.arcs
            for npc in arc.npcs
        }

    def runtime_npcs(self) -> dict[str, NpcConcept]:
        result: dict[str, NpcConcept] = {}
        for arc in self.arcs:
            for npc in arc.npcs:
                result[npc.name] = NpcConcept(
                    name=npc.name,
                    concept=npc.concept,
                    campaign_role=npc.campaign_role,
                    tone=npc.tone,
                )
        return result

    def runtime_phases(self) -> list[ScenarioPhase]:
        result: list[ScenarioPhase] = []
        for arc_index, arc in enumerate(self.arcs):
            for phase in arc.phases:
                result.append(
                    ScenarioPhase(
                        slug=phase.slug,
                        title=phase.title,
                        location_description=phase.location_description,
                        mood=phase.mood,
                        tension=phase.tension,
                        objective=phase.objective,
                        introduced_npcs=tuple(phase.introduced_npcs),
                        active_npcs=tuple(phase.active_npcs),
                        opening_theses=tuple(
                            ThesisSeed(
                                thesis_type=item.thesis_type,
                                text=item.text,
                                priority=item.priority,
                                visibility=item.visibility,
                                related_names=tuple(item.related_names),
                            )
                            for item in phase.opening_theses
                        ),
                        pulses=tuple(
                            ScenarioPulse(
                                at_fraction=item.at_fraction,
                                event=item.event,
                                thesis=ThesisSeed(
                                    thesis_type=item.thesis.thesis_type,
                                    text=item.thesis.text,
                                    priority=item.thesis.priority,
                                    visibility=item.thesis.visibility,
                                    related_names=tuple(item.thesis.related_names),
                                ),
                            )
                            for item in phase.pulses
                        ),
                        director_note=phase.director_note,
                        completion_criteria=tuple(
                            ObjectiveCriterion(
                                key=item.key,
                                description=item.description,
                                allowed_change_types=tuple(item.allowed_change_types),
                            )
                            for item in phase.completion_criteria
                        ),
                        min_turns=phase.min_turns,
                        max_turns=phase.max_turns,
                        arc_index=arc_index,
                    )
                )
        return result


def _canonicalize_related_names(
    values: list[str],
    canonical: dict[str, str],
    active: set[str],
    *,
    location: str,
) -> list[str]:
    result: list[str] = []
    for value in values:
        folded = value.casefold()
        if folded not in canonical:
            raise ValueError(f"{location} references unknown NPC {value!r}")
        if folded not in active:
            raise ValueError(f"{location} references inactive NPC {value!r}")
        result.append(canonical[folded])
    return list(dict.fromkeys(result))


def _reject_near_miss_npc_names(
    texts: list[str],
    canonical_names: list[str],
    *,
    location: str,
) -> None:
    vowels = set("аеёиоуыэюя")
    capitalized_tokens = {
        token
        for text in texts
        for token in re.findall(r"\b[А-ЯЁ][А-Яа-яЁё]{3,}\b", text)
    }
    for canonical_name in canonical_names:
        first_name = re.findall(r"[А-Яа-яЁё]+", canonical_name)[0].casefold()
        root = first_name[:-1] if first_name[-1] in vowels else first_name
        consonants = "".join(
            char for char in root if char not in vowels and char not in "ьъй"
        )
        if len(consonants) < 2:
            continue
        for token in capitalized_tokens:
            folded = token.casefold()
            if folded in {"вернуть"}:
                continue
            token_consonants = "".join(
                char for char in folded if char not in vowels and char not in "ьъй"
            )
            consonant_similarity = SequenceMatcher(
                None,
                token_consonants,
                consonants,
            ).ratio()
            name_similarity = SequenceMatcher(
                None,
                folded,
                first_name,
            ).ratio()
            if (
                (token_consonants == consonants or consonant_similarity >= 0.8)
                and (
                    folded[0] == first_name[0]
                    or name_similarity >= 0.7
                )
                and not folded.startswith(root[:4])
            ):
                raise ValueError(
                    f"{location} uses near-miss NPC name {token!r}; "
                    f"canonical name is {canonical_name!r}"
                )


def normalize_arc_references(catalog: CampaignCatalog, arc: GeneratedArc) -> GeneratedArc:
    """Canonicalize generated names and reject references that would crash a phase."""
    existing = catalog.canonical_npc_names()
    new_names = {npc.name.casefold(): npc.name for npc in arc.npcs}
    duplicates = sorted(set(existing) & set(new_names))
    if duplicates:
        raise ValueError(f"generated arc reused existing NPC names: {duplicates}")

    canonical = {**existing, **new_names}
    introduced = set(existing)
    used_titles = set(catalog.phase_titles)
    arc_titles: set[str] = set()

    for phase in arc.phases:
        title_key = phase.title.casefold()
        if title_key in used_titles or title_key in arc_titles:
            raise ValueError(f"generated arc reused phase title {phase.title!r}")
        arc_titles.add(title_key)

        normalized_introduced: list[str] = []
        for raw_name in phase.introduced_npcs:
            folded = raw_name.casefold()
            if folded not in new_names:
                raise ValueError(
                    f"phase {phase.slug} introduces unknown or old NPC {raw_name!r}"
                )
            if folded in introduced:
                # A previous phase may already have repaired this NPC's first use.
                # Do not emit a second introduction later in the arc.
                continue
            normalized_introduced.append(canonical[folded])
            introduced.add(folded)

        normalized_active: list[str] = []
        for raw_name in phase.active_npcs:
            folded = raw_name.casefold()
            if folded not in canonical:
                raise ValueError(f"phase {phase.slug} activates unknown NPC {raw_name!r}")
            if folded not in introduced:
                # Treat the first active appearance of an arc-local NPC as its
                # introduction. This deterministic repair preserves the model's
                # dramatic intent while keeping the runtime catalog consistent.
                normalized_introduced.append(canonical[folded])
                introduced.add(folded)
            normalized_active.append(canonical[folded])
        phase.introduced_npcs = list(dict.fromkeys(normalized_introduced))
        phase.active_npcs = list(dict.fromkeys(normalized_active))
        active = {name.casefold() for name in phase.active_npcs}

        for index, thesis in enumerate(phase.opening_theses):
            thesis.related_names = _canonicalize_related_names(
                thesis.related_names,
                canonical,
                active,
                location=f"phase {phase.slug} opening_theses[{index}]",
            )
        for index, pulse in enumerate(phase.pulses):
            pulse.thesis.related_names = _canonicalize_related_names(
                pulse.thesis.related_names,
                canonical,
                active,
                location=f"phase {phase.slug} pulses[{index}]",
            )
        presence_fields = [
            *(
                thesis.text
                for thesis in phase.opening_theses
            ),
            *(
                text
                for pulse in phase.pulses
                for text in (pulse.event, pulse.thesis.text)
            ),
        ]
        narrative_fields = [
            phase.objective,
            phase.director_note,
            *presence_fields,
            *(
                criterion.description
                for criterion in phase.completion_criteria
            ),
        ]
        _reject_near_miss_npc_names(
            narrative_fields,
            list(canonical.values()),
            location=f"phase {phase.slug} prose",
        )
        for npc_key, npc_name in canonical.items():
            if npc_key in active:
                continue
            if any(npc_name.casefold() in text.casefold() for text in presence_fields):
                raise ValueError(
                    f"phase {phase.slug} prose references inactive NPC {npc_name!r}"
                )

    return arc


def _compact_previous_outcomes(outcomes: list[str]) -> str:
    cleaned = [" ".join(str(item).split())[:500] for item in outcomes if str(item).strip()]
    return "\n".join(f"- {item}" for item in cleaned[-12:]) or "- это первый акт"


async def generate_arc(
    provider: LLMProvider,
    router: RoleModelRouter,
    selection: RoleModelSelection,
    catalog: CampaignCatalog,
    previous_outcomes: list[str],
) -> GeneratedArc:
    arc_index = len(catalog.arcs)
    max_arcs = max(1, min(6, int(os.getenv("PDM_SIM_MAX_ARCS", "3"))))
    terminal_arc = arc_index + 1 >= max_arcs
    phase_count = max(2, min(6, int(os.getenv("PDM_SIM_ARC_PHASES", "4"))))
    genre = os.getenv("PDM_SIM_GENRE", "приземлённое тёмное фэнтези")
    premise_hint = os.getenv("PDM_SIM_PREMISE", "").strip()
    existing_names = ", ".join(sorted(catalog.npc_names)) or "нет"
    existing_slugs = ", ".join(sorted(catalog.phase_slugs)) or "нет"
    existing_titles = ", ".join(sorted(catalog.phase_titles)) or "нет"
    continuation = _compact_previous_outcomes(previous_outcomes)
    prompt = f"""Ты проектируешь новый акт автономной кампании для проверки RPG-движка.
Верни только JSON по схеме GeneratedArc. Язык всех игровых текстов — русский.

SEED: {catalog.seed}
НОМЕР АКТА: {arc_index + 1} из {max_arcs}
ЖАНР: {genre}
ПОДСКАЗКА ПРЕМИСЫ: {premise_hint or 'нет'}
ПРЕДЫДУЩИЕ ИТОГИ:
{continuation}

УЖЕ ИСПОЛЬЗОВАННЫЕ ИМЕНА NPC: {existing_names}
УЖЕ ИСПОЛЬЗОВАННЫЕ SLUG: {existing_slugs}
УЖЕ ИСПОЛЬЗОВАННЫЕ НАЗВАНИЯ СЦЕН: {existing_titles}

Создай {phase_count} связанные, но различающиеся сцены и 2-8 новых NPC.
Требования:
- Не используй существующие имена, slug и названия сцен повторно.
- Во всех ссылках пиши имя NPC точно так же, как в npcs.name.
- Новый NPC должен появиться в introduced_npcs до первого появления в active_npcs.
- related_names содержит только NPC, активных в этой сцене.
- Каждый NPC имеет профессиональную роль, личную цель, ограничение и секрет, влияющий на выборы.
- Каждая сцена имеет конкретную достижимую цель, 2-4 машинно проверяемых completion_criteria и 2-4 осложнения.
- Критерий описывает наблюдаемое изменение канона, а не настроение, разговор или план.
- allowed_change_types выбирай по смыслу: физический результат требует event/movement/item_transfer/fact; социальное изменение — relationship/knowledge/event; открытие истины — fact/knowledge/event.
- min_turns 5-10, max_turns 12-24. Не растягивай дверную загадку на весь акт.
- Пульсы должны менять ситуацию, а не только добавлять атмосферу.
- Мир доиндустриальный: алхимия использует колбы, весы, ступки, травы и рукописи.
  Запрещены анализаторы, спектральные приборы, электроника, реакторы, плазма,
  радиация, генетика и современная лабораторная терминология.
- Последняя сцена акта даёт итог и причинно продолжает предыдущие последствия.
- Если подсказка премисы требует личную цену или жертву, закрепи её не только в premise: она должна явно присутствовать в objective, director_note или completion_criteria хотя бы одной сцены.
- Для этого акта terminal должен быть {str(terminal_arc).lower()}. Если terminal=true, последняя сцена обязана завершить главный конфликт кампании без крючка продолжения.
- Active NPC должны быть либо уже существующими NPC кампании, либо новыми NPC этого акта.
"""
    validation_feedback = ""
    last_error: ValueError | None = None
    for attempt, temperature in enumerate((0.65, 0.45, 0.25), start=1):
        attempt_prompt = prompt
        if validation_feedback:
            attempt_prompt += (
                "\nПРЕДЫДУЩИЙ ВАРИАНТ ОТКЛОНЁН ВАЛИДАТОРОМ:\n"
                f"{validation_feedback}\n"
                "Сгенерируй весь акт заново и исправь именно эту ошибку. "
                "Не повторяй отклонённые названия и slug.\n"
            )
        try:
            payload = await router.generate_json(
                provider,
                selection,
                [ChatMessage(role="system", content=attempt_prompt)],
                max_tokens=4600,
                temperature=temperature,
                response_model=GeneratedArc,
            )
            arc = GeneratedArc.model_validate(payload)
            # Campaign length is a benchmark contract, not a creative suggestion.
            arc.terminal = terminal_arc
            if len(arc.phases) < phase_count:
                raise ValueError(
                    f"generated arc has {len(arc.phases)} phases; expected at least {phase_count}"
                )
            if len(arc.phases) > phase_count:
                arc.phases = arc.phases[:phase_count]
            arc = normalize_arc_references(catalog, arc)
            motif_text = " ".join(
                [
                    *(phase.objective for phase in arc.phases),
                    *(phase.director_note for phase in arc.phases),
                    *(
                        criterion.description
                        for phase in arc.phases
                        for criterion in phase.completion_criteria
                    ),
                ]
            ).casefold()
            if premise_hint and not terminal_arc and not any(
                marker in motif_text
                for marker in ("цен", "жертв", "плат", "утрат", "потер")
            ):
                raise ValueError(
                    "opening arc failed to establish the premise's personal price or sacrifice"
                )
            if premise_hint and terminal_arc and not any(
                marker in motif_text
                for marker in ("цен", "жертв", "плат", "утрат", "потер")
            ):
                raise ValueError(
                    "terminal arc failed to pay off the premise's personal price or sacrifice"
                )
            used_slugs = catalog.phase_slugs
            if any(phase.slug in used_slugs for phase in arc.phases):
                raise ValueError("generated arc reused an existing phase slug")
        except (LLMProviderError, ValidationError, ValueError) as exc:
            last_error = exc
            validation_feedback = str(exc)
            if attempt < 3:
                continue
            raise
        return arc
    raise last_error or ValueError("generated arc failed validation")


async def ensure_phase_available(
    *,
    path: Path,
    reset: bool,
    phase_index: int,
    provider: LLMProvider,
    router: RoleModelRouter,
    selection: RoleModelSelection,
    previous_outcomes: list[str],
) -> CampaignCatalog:
    catalog = None if reset else CampaignCatalog.load(path)
    if catalog is None:
        seed = os.getenv("PDM_SIM_SCENARIO_SEED", "").strip() or os.urandom(8).hex()
        catalog = CampaignCatalog(seed=seed)
    while phase_index >= len(catalog.runtime_phases()):
        catalog.arcs.append(
            await generate_arc(
                provider,
                router,
                selection,
                catalog,
                previous_outcomes,
            )
        )
        catalog.save(path)
    return catalog


def catalog_summary(catalog: CampaignCatalog) -> dict[str, object]:
    return {
        "seed": catalog.seed,
        "arcs": len(catalog.arcs),
        "phases": len(catalog.runtime_phases()),
        "npcs": len(catalog.runtime_npcs()),
        "titles": [arc.arc_title for arc in catalog.arcs],
    }
