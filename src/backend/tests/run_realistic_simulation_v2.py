from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.memory import resolve_proposal
from app.api.world_state import CharacterDraft, create_character_from_draft
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import (
    Belief,
    CharacterGoal,
    Entity,
    Event,
    ProposedChange,
    RelationshipAssertion,
    Scene,
    SceneThesis,
)
from app.db.tables import (
    Turn as DBTurn,
)
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterUpdate
from app.models.entity import EntityCreate, EntityType
from app.models.event import EventCreate
from app.models.proposed_change import ProposalAction
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate, SceneUpdate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.campaign_service import CampaignService
from app.services.context_compiler import ContextCompiler
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.thesis_curator import ThesisCurator
from app.services.turn_runner import TurnRunner

try:
    from .simulation_database import upgrade_simulation_database
    from .simulation_dynamic_campaign import (
        _reject_near_miss_npc_names,
        catalog_summary,
        ensure_phase_available,
    )
    from .simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase
except ImportError:
    from simulation_database import upgrade_simulation_database
    from simulation_dynamic_campaign import (
        _reject_near_miss_npc_names,
        catalog_summary,
        ensure_phase_available,
    )
    from simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase


OUTCOME_PATTERNS = (
    r"\bя (?:успешно|точно|немедленно)\b",
    r"\bя (?:нахожу|обнаруживаю|открываю|побеждаю|убиваю|исцеляю|решаю)\b",
    r"\b(?:дверь|ворота|замок|враг|ритуал) (?:открывается|ломается|побеждён|срабатывает)\b",
    r"\bI (?:successfully|discover|find|open|defeat|solve)\b",
)
WORD_PATTERN = re.compile(r"[\w]+", flags=re.UNICODE)
JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", flags=re.DOTALL)
NPC_TITLE_TOKENS = {
    "мастер",
    "сестра",
    "брат",
    "братус",
    "архивариус",
    "маэстра",
    "старейшина",
}


def _npc_name_stems(name: str) -> set[str]:
    stems: set[str] = set()
    for token in re.findall(r"[А-Яа-яЁё]+", name.casefold()):
        if token in NPC_TITLE_TOKENS or len(token) < 3:
            continue
        if token.endswith(("а", "я", "ь", "й")) and len(token) > 3:
            token = token[:-1]
        stems.add(token)
    return stems


def validate_russian_narrative(text: str) -> tuple[bool, str | None]:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False, "text contains no letters"
    cyrillic = 0
    for char in letters:
        script_name = unicodedata.name(char, "")
        if "CYRILLIC" in script_name:
            cyrillic += 1
        elif "LATIN" not in script_name:
            return False, f"text contains non-Russian script character {char!r}"
    if cyrillic / len(letters) < 0.55:
        return False, "text is not predominantly Russian"
    for token in "".join(char if char.isalpha() else " " for char in text).split():
        scripts = {
            "cyrillic" if "CYRILLIC" in unicodedata.name(char, "") else "latin"
            for char in token
            if "CYRILLIC" in unicodedata.name(char, "")
            or "LATIN" in unicodedata.name(char, "")
        }
        if len(scripts) > 1:
            return False, f"text contains mixed-script token {token!r}"
    if re.search(r"[а-яё]{2}[А-ЯЁ][а-яё]{2}", text):
        return False, "text contains glued Russian words"
    if re.search(r"\b([а-яё]{3,})\.{3}\1[а-яё]*", text, re.IGNORECASE):
        return False, "text contains a broken repeated word"
    if re.search(
        r"\b([а-яё]{4,})\.{3}[^\n]{0,80}\b\1[а-яё]*",
        text,
        re.IGNORECASE,
    ):
        return False, "text contains a broken repeated phrase"
    if re.search(
        r"\bне\s+(уверенност\w*|страх\w*|тишин\w*|темнот\w*|"
        r"напряжени\w*)\b.{0,80}\bа\b.{0,60}\b\1\b",
        text,
        re.IGNORECASE,
    ):
        return False, "text contains a self-contradictory repeated phrase"
    if re.search(r"\b[а-яё]{2,}\s+\.{3}\s*[а-яё]+\b", text, re.IGNORECASE):
        return False, "text contains a broken word around an ellipsis"
    if re.search(r"\bразберешив\w*\b", text, re.IGNORECASE):
        return False, "text contains a malformed Russian word"
    if re.search(r"[а-яё,]\s+(?:Он|Она|Они|Его|Её)\b", text):
        return False, "text contains a missing sentence boundary"
    return True, None


def validate_dm_player_agency(
    text: str,
    location_description: str = "",
    active_npcs: list[str] | None = None,
    player_mode: str | None = None,
    recent_history: str = "",
    allow_paid_cost: bool = False,
) -> tuple[bool, str | None]:
    try:
        _reject_near_miss_npc_names(
            [text],
            active_npcs or [],
            location="DM prose",
        )
    except ValueError:
        return False, "DM uses a near-miss active NPC name"
    if re.search(
        r"\b(?:Эльдон|Елдон|Элден|Элдэн)\w*\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM uses an inconsistent player name"
    if re.search(
        r"\bnarrator\b|\bтвой запрос\b|"
        r"\bигрок(?:а|у|ом|е|и|ов|ами)?\b|"
        r"\*{2}(?:конкретное последствие|напряжение|результат|итог)\b|"
        r"\bнапряжение оста[её]тся (?:высоким|низким|средним)\b|"
        r"[—-]{3,}",
        text,
        re.IGNORECASE,
    ):
        return False, "DM exposes control-plane or benchmark prose"
    if re.search(r"(?m)^\s*\d+[.)]\s+(?:\*\*)?", text):
        return False, "DM uses report-style numbered prose"
    if re.search(
        r"(?mi)^\s*\*{0,2}(?:последствия|итог|результат|"
        r"проверка канона|изменения канона)\*{0,2}\s*:",
        text,
    ):
        return False, "DM exposes control-plane or benchmark prose"
    if re.search(
        r"\b(?:анализатор\w*\s+сред\w*|спектральн\w+\s+анализ\w*|"
        r"электромагнитн\w*|реактор\w*|плазм\w*|радиоактив\w*|"
        r"генетическ\w*|компьютер\w*|лабораторн\w+\s+халат\w*|"
        r"клеточн\w+\s+уровн\w*|дифференциальн\w+\s+диагностик\w*|"
        r"перенасыщенн\w+\s+раствор\w*|эпицентр\w*\s+экссудац\w*|"
        r"химическ\w+\s+(?:состав\w*|разложени\w*|барьер\w*)|"
        r"градиент\w+\s+(?:плотност\w*|концентрац\w*))\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM introduces modern or science-fiction genre drift"
    if re.search(
        r"\b(?:доста[её]т|достал[аи]?|доставая|вынул[аи]?|вынув|вынимает)\b"
        r".{0,100}\b(?:колб\w*|пробирк\w*|"
        r"склянк\w*|луп\w*|ступк\w*)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents an undeclared inventory item"
    diary_event = re.search(
        r"\b(?:дневник\w*|книжк\w*|книг\w*)\b[\s\S]{0,500}\b(?:вылет\w*|вышвыр\w*|"
        r"пад\w*|упал\w*)\b|\b(?:вылет\w*|вышвыр\w*|пад\w*|упал\w*)\b"
        r"[\s\S]{0,500}\b(?:дневник\w*|книжк\w*|книг\w*)\b",
        text,
        re.IGNORECASE,
    )
    if diary_event and re.search(
        r"\b(?:дневник\w*|книжк\w*|книг\w*)\b",
        recent_history,
        re.IGNORECASE,
    ):
        return False, "DM repeats an already resolved scene event"
    secret_cost_pattern = re.compile(
        r"\b(?:теря\w*|утрат\w*|ст[её]р\w*|лиш\w*|"
        r"смо[её]т\w*|смыва\w*|забер\w*|забира\w*)\b.{0,100}\b"
        r"(?:имен\w*|воспоминан\w*|памят\w*)\b",
        re.IGNORECASE,
    )
    verana_revealed_cost = bool(
        re.search(r"\bВерана\b", recent_history, re.IGNORECASE)
        and secret_cost_pattern.search(recent_history)
    )
    if secret_cost_pattern.search(text) and not verana_revealed_cost:
        paragraphs = re.split(r"\n\s*\n", text)
        for index, paragraph in enumerate(paragraphs):
            if not secret_cost_pattern.search(paragraph):
                continue
            if re.search(r"\bТарн\b", paragraph, re.IGNORECASE):
                return False, "DM leaks Verana's secret through the wrong NPC"
            if re.search(r"\bВерана\b", paragraph, re.IGNORECASE):
                continue
            previous = paragraphs[index - 1] if index else ""
            if (
                paragraph.lstrip().startswith(("—", "«", '"'))
                and re.search(r"\bВерана\b", previous, re.IGNORECASE)
            ):
                continue
            return False, "DM leaks Verana's secret through the wrong NPC"
    if re.search(
        r"\b(?:утрачу|потеряю|лишусь)\s+(?:сво[её]\s+)?имен\w*\b|"
        r"\b(?:утрачу|потеряю|лишусь)\b.{0,60}\bлиц\w+\s+наставник\w*\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM distorts the defined personal cost"
    if (
        re.search(
            r"\bпамят\w*\b.{0,80}\bбез\s+имен\w*\b|"
            r"\bбез\s+имен\w*\b.{0,80}\bпамят\w*\b",
            text,
            re.IGNORECASE,
        )
        and not re.search(r"\bВерана\b", text, re.IGNORECASE)
    ):
        return False, "DM leaks Verana's secret through the wrong NPC"
    if not allow_paid_cost and re.search(
        r"\bцен\w*\b.{0,100}\b(?:уже\s+)?(?:заплачен\w*|уплачен\w*)\b|"
        r"\b(?:уже\s+)?(?:заплатил[аи]?|уплатил[аи]?|потерял[аи]?)\b"
        r".{0,100}\b(?:цен\w*|последн\w+\s+т[её]пл\w+\s+воспоминан\w*)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM pays the personal cost before the terminal finale"
    if re.search(
        r"\b(?:после\s+\w+\s+хода|ситуация\s+изменилась|"
        r"в\s+сцене\s+появилось|также\s+зафиксировано)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM exposes turn bookkeeping in narrative prose"
    if re.search(
        r"\b(?:жезл\w*|посох\w*|кинжал\w*|меш(?:ок|оч\w*)|"
        r"книг\w*|трав\w*)\b[\s\S]{0,200}\b(?:вспых\w*|засвет\w*|"
        r"светится|пульсир\w*|завис\w*|"
        r"взлет\w*|парит\w*|не\s+упал\w*|сам(?:а|о)?\s+двин\w*|"
        r"воздух\w*\s+(?:вокруг\s+)?(?:дрож\w*|задрожал\w*))\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents an undeclared magical item property"
    if re.search(
        r"\b(?:жезл\w*|посох\w*|кинжал\w*|меш(?:ок|оч\w*)|"
        r"книг\w*|трав\w*)\b[^.!?\n]{0,35}\b(?:заговор\w*|зашептал\w*)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents an undeclared magical item property"
    if re.search(
        r"\b(?:его|в его)\s+взгляд\w*\b.{0,80}\b(?:уверенн\w*|"
        r"настороженн\w*|решимост\w*|понимани\w*)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents Eldon's internal state"
    if re.search(
        r"\bест(?:ь| вас)\b.{0,40}\bкак\s+ед\w+\s+в\s+котл\w*\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM uses incoherent figurative language"
    if re.search(
        r"\b(?:их|наш|следующий)\s+следующ(?:ий|его)\s+шаг\s+"
        r"(?:был\s+)?(?:определ[её]н|реш[её]н)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM decides the player's next step"
    retrospective_pattern = re.compile(
        r"\b(?:ранее|раньше(?!\s+времени\b)|до этого|перед этим)\b"
        r"(?!\s+(?:молчал[аи]?|не\s+(?:говорил[аи]?|вмешивал(?:ся|ась))))",
        re.IGNORECASE,
    )
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if not retrospective_pattern.search(sentence):
            continue
        sentence_stems = {
            word.casefold()[:4]
            for word in re.findall(r"[А-ЯЁа-яё]{4,}", sentence)
            if word.casefold() not in {"ранее", "раньше", "этого"}
        }
        history_stems = {
            word.casefold()[:4]
            for word in re.findall(r"[А-ЯЁа-яё]{4,}", recent_history)
        }
        if len(sentence_stems & history_stems) < 2:
            return False, "DM makes an unsupported retrospective assertion"
    if (
        re.search(
            r"\b(?:поляна|лес|тропа|опушка|земля|окраин\w*|поле|"
            r"улиц\w*|двор\w*|поселени\w*)\b",
            location_description,
            re.IGNORECASE,
        )
        and re.search(
            r"\b(?:помещение|комната|коридор|потолок|подоконник|"
            r"лаборатори\w*|кабинет\w*|аудитори\w*)\b",
            text,
            re.IGNORECASE,
        )
    ):
        return False, "DM contradicts the outdoor scene location"
    if (
        re.search(
            r"\b(?:поселени\w*|улиц\w*|переул\w*|дом\w*|лавк\w*)\b",
            location_description,
            re.IGNORECASE,
        )
        and not re.search(r"\b(?:лес\w*|чащ\w*|холм\w*)\b", location_description, re.IGNORECASE)
        and re.search(r"\b(?:край\s+леса|лес\w*|чащ\w*|холм\w*)\b", text, re.IGNORECASE)
    ):
        return False, "DM moves the settlement scene into undeclared wilderness"
    if re.search(
        r"\bтебе кажется\b|"
        r"\bты\s+(?:не\s+)?(?:думаешь|чувствуешь|хочешь|желаешь|надеешься|"
        r"боишься|решаешь|ожидаешь|жд[её]шь|осозна[её]шь|понимаешь|"
        r"помнишь|концентрируешься)\b|"
        r"\bтвой\s+(?:взгляд|внимание|желание|страх|мысл[ьи]|чувств\w*)\b|"
        r"\bкаждый мускул напряж[её]н\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents Eldon's internal state"
    if re.search(
        r"\bваши?\s+(?:глаз\w*|колен\w*|взгляд\w*|лиц\w*|рук\w*|ног\w*)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM narrates the player character"
    speech_verbs = (
        r"сказал|говорит|спросил|спрашивает|ответил|отвечает|"
        r"произн[её]с|произносит|повторил|повторяет|объяснил|объясняет|"
        r"предложил|потребовал|прошептал|крикнул|"
        r"констатир\w*|добавил|возразил|продолжил"
    )
    quote_speech_verbs = rf"(?:{speech_verbs}|заметил)"
    if re.search(
        rf"[«\"](?:[^»\"\n]|\n(?!\n)){{1,700}}[»\"]"
        rf"[\s,—-]*(?:{quote_speech_verbs})\s+Элдон\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents direct speech for Eldon"
    if re.search(
        rf"(?:^|\n)\s*—[\s\S]{{1,700}}?—\s*"
        rf"(?:{quote_speech_verbs})\s+Элдон\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents direct speech for Eldon"
    if re.search(
        rf"\bЭлдон\b[^\n]{{0,220}}[«\"][\s\S]{{1,320}}[»\"]"
        rf"[\s,—-]*(?:{quote_speech_verbs})\s+(?:Элдон|он)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents direct speech for Eldon"
    if re.search(
        rf"\bЭлдон\s+(?:{speech_verbs})\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents direct speech for Eldon"
    if player_mode in {"question", "dialogue", "plan", "decision"} and re.search(
        r"\bЭлдон\b[^.\n]{0,180}\b(?:кивает|кивнув|прислоняется|"
        r"прислонившись|отходит|отойдя|подходит|подойдя|"
        r"отступает|садится|вста[её]т|бер[её]т|поднимает|опускает|"
        r"направляется|ид[её]т|двигается|начинает\s+двигаться|"
        r"(?:начинает|стал|принялся)\s+[а-яё]+|поворачивается)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents an unrequested physical action for Eldon"
    interior_verbs = (
        r"думает|думал|чувствует|чувствовал|хочет|хотел|желает|желал|"
        r"надеется|надеялся|боится|боялся|решает|решил|"
        r"ожидает|ожидал|жд[её]т|ждал|знает|знал|уверен|"
        r"осозна[её]т|осознал|понимает|понял|"
        r"помнит|помнил|ощущает|ощутил|готовится|"
        r"игнорирует|игнорировал|подсчитывает|подсчитал|"
        r"делает\s+(?:быстрый\s+)?вывод|сделал\s+вывод"
    )
    eldon_context = False
    for paragraph in re.split(r"\n\s*\n", text):
        folded = paragraph.casefold().strip()
        if re.match(
            r"^(?:(?:когда|пока|если)\s+)?элдон\b",
            folded,
        ):
            eldon_context = True
        elif eldon_context and folded.startswith(("—", "-")):
            return False, "DM invents direct speech for Eldon"
        elif eldon_context and folded.startswith(("«", '"')):
            if re.search(
                rf"[»\"][\s,—-]*(?:{quote_speech_verbs})\s+он\b",
                paragraph,
                re.IGNORECASE,
            ):
                return False, "DM invents direct speech for Eldon"
            eldon_context = False
        elif eldon_context and not (
            re.match(r"^(?:он|его|в его)\b", folded)
            or re.search(r"\b(?:он|его|ему|им)\b", folded)
        ):
            eldon_context = False
        if not eldon_context:
            continue
        if re.search(r"[«\"]", paragraph):
            return False, "DM invents direct speech for Eldon"
        if re.search(
            rf"\b(?:Элдон|он)\s+(?:не\s+)?(?:{interior_verbs})\b",
            paragraph,
            re.IGNORECASE,
        ):
            return False, "DM invents Eldon's internal state"
        if re.search(
            r"\b(?:делает\s+(?:быстрый\s+)?вывод|сделал\s+вывод)\b",
            paragraph,
            re.IGNORECASE,
        ):
            return False, "DM invents Eldon's internal state"
        if re.search(
            rf"\bкак будто\s+(?:{interior_verbs})\b",
            paragraph,
            re.IGNORECASE,
        ):
            return False, "DM invents Eldon's internal state"
        if re.search(
            r"\b(?:помня|понимая|зная|осознавая|решая|стараясь|"
            r"надеясь|ожидая|решив|игнорируя)\b",
            paragraph,
            re.IGNORECASE,
        ):
            return False, "DM invents Eldon's internal state"
        if re.search(
            r"\b(?:его|в его)\s+(?:внимание|сосредоточенность)\b",
            paragraph,
            re.IGNORECASE,
        ):
            return False, "DM invents Eldon's internal state"
        if re.search(
            r"\*(?:Я|Мне|Меня|Мой|Моя|Моё|Мы)\b[^*]{1,300}\*",
            paragraph,
        ):
            return False, "DM invents Eldon's internal state"
        if re.search(
            r"\b(?:его|в его)\s+(?:глубок\w+\s+)?(?:желание|надежд\w+|страх\w+|"
            r"мысл\w+|чувств\w+)\b",
            paragraph,
            re.IGNORECASE,
        ):
            return False, "DM invents Eldon's internal state"
    if re.search(
        r"\b(?:доста[её]т|вынимает|бер[её]т)\s+из\s+(?:своего\s+)?инвентаря\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM invents an undeclared inventory item"
    undeclared_eldon_item = (
        r"(?:портативн\w+\s+)?анализатор\w*|"
        r"пробоотборн\w+\s+трубочк\w*|"
        r"блокнот\w*"
    )
    # Keep the ownership check local.  A scene can legitimately mention Eldon
    # and then show a declared item belonging to an NPC (for example Elias's
    # analyzer) in another paragraph.
    for paragraph in re.split(r"\n\s*\n", text):
        if (
            re.search(
                rf"\bЭлдон\b.{{0,100}}\b(?:{undeclared_eldon_item})\b",
                paragraph,
                re.IGNORECASE,
            )
            or re.search(
                rf"\b(?:{undeclared_eldon_item})\b.{{0,100}}\bЭлдон\b",
                paragraph,
                re.IGNORECASE,
            )
        ):
            return False, "DM invents an undeclared inventory item"
    if player_mode in {"dialogue", "question", "plan", "decision"} and re.search(
        r"(?:^|\n+)\s*(?:Элдон|Вы|Ты)\b",
        text,
        re.IGNORECASE,
    ):
        return False, "DM narrates the player character"
    return True, None


class ObjectiveEvaluation(BaseModel):
    status: Literal["progressing", "resolved", "failed", "blocked"] = "progressing"
    evidence: str = ""
    outcome_summary: str | None = None
    confirmed_pulses: list[int] = Field(default_factory=list)
    criteria_met: list[str] = Field(default_factory=list)
    criterion_evidence: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class PlayerDecision:
    target: str
    mode: Literal["action", "dialogue", "question", "plan", "decision"]
    intent: str

    def render(self) -> str:
        return f"[/talk {self.target}] {self.intent.strip()}"


@dataclass
class PhaseRuntime:
    index: int
    phase: ScenarioPhase
    scene_id: UUID
    location_id: UUID
    active_characters: dict[str, UUID]
    phase_turn: int = 0
    injected_pulses: set[int] = field(default_factory=set)
    confirmed_pulses: set[int] = field(default_factory=set)
    criteria_met: set[str] = field(default_factory=set)
    durable_changes: list[str] = field(default_factory=list)


@dataclass
class SimulationState:
    run_id: str
    campaign_id: str | None = None
    logical_turn: int = 1
    phase_index: int = 0
    phase_turn: int = 0
    injected_pulses: list[int] = field(default_factory=list)
    confirmed_pulses: list[int] = field(default_factory=list)
    criteria_met: list[str] = field(default_factory=list)
    durable_changes: list[str] = field(default_factory=list)
    player_journal: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    completed: bool = False

    @classmethod
    def load(cls, path: Path) -> SimulationState | None:
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def save(self, path: Path) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


class TraceStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[int, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    self.records[int(record["turn"])] = record
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue

    def upsert(self, record: dict) -> None:
        self.records[int(record["turn"])] = record
        self.flush()

    def flush(self) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            for turn in sorted(self.records):
                handle.write(json.dumps(self.records[turn], ensure_ascii=False) + "\n")
        temp.replace(self.path)

    def write_play_log(self, path: Path, total_turns: int) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write("REALISTIC AUTONOMOUS CAMPAIGN V2\n\n")
            for number in sorted(self.records):
                record = self.records[number]
                handle.write("=" * 72 + "\n")
                handle.write(
                    f"TURN {number}/{total_turns} | {record.get('phase_title', record.get('phase'))}\n"
                )
                handle.write(f"OBJECTIVE: {record.get('objective', '')}\n")
                handle.write(
                    "ACTIVE NPCS: " + ", ".join(record.get("active_npcs", [])) + "\n"
                )
                player = record.get("player", {})
                handle.write(
                    f"PLAYER [{player.get('mode')} -> {player.get('target')}]: "
                    f"{player.get('intent', '')}\n"
                )
                handle.write("-" * 72 + "\n")
                handle.write(f"DM: {record.get('dm', '')}\n")
                evaluation = record.get("evaluation") or {}
                if evaluation:
                    handle.write(
                        f"OBJECTIVE STATUS: {evaluation.get('status')} | "
                        f"{evaluation.get('evidence', '')}\n"
                    )
                for item in record.get("accepted", []):
                    handle.write(f"ACCEPTED: {item}\n")
                for item in record.get("rejected", []):
                    handle.write(f"REJECTED: {item}\n")
                handle.write("THESES:\n")
                for item in record.get("active_theses", []):
                    handle.write(f" - [{item.get('type')}] {item.get('text')}\n")
                handle.write("\n")
        temp.replace(path)


class PlayerPolicy:
    MODES = ("question", "action", "dialogue", "plan", "action", "decision")

    def __init__(self) -> None:
        self.recent_fingerprints: deque[str] = deque(maxlen=16)
        self.mode_counts: Counter[str] = Counter()
        self.target_counts: Counter[str] = Counter()
        self.rejected_outcomes = 0
        self.repeated_actions = 0
        self.fallbacks = 0

    @staticmethod
    def fingerprint(value: str) -> str:
        tokens = [token.casefold() for token in WORD_PATTERN.findall(value)]
        return " ".join(tokens[:60])

    def preferred_mode(self, turn_number: int) -> str:
        return self.MODES[(turn_number - 1) % len(self.MODES)]

    def suggested_target(self, active_npcs: list[str], mode: str) -> str:
        if mode in {"question", "dialogue", "plan", "decision"} and active_npcs:
            return min(
                active_npcs,
                key=lambda name: (
                    self.target_counts[name.casefold()],
                    name.casefold(),
                ),
            )
        return "narrator"

    def validate(
        self,
        decision: PlayerDecision,
        active_npcs: list[str],
    ) -> tuple[bool, str | None]:
        active = {name.casefold() for name in active_npcs}
        if decision.target.casefold() != "narrator" and decision.target.casefold() not in active:
            return False, f"target {decision.target!r} is not active"
        if not decision.intent.strip() or len(decision.intent) > 700:
            return False, "intent is empty or too long"
        language_valid, language_error = validate_russian_narrative(decision.intent)
        if not language_valid:
            return False, language_error
        if not re.search(
            r"\b(?:я|мне|меня|мой|моя|моё|элдон)\b",
            decision.intent,
            re.IGNORECASE,
        ):
            return False, "intent does not identify Eldon as the acting player"
        if re.search(
            r"\b(?:Элдон|элдон)\s+[А-ЯЁ][а-яё]+\b",
            decision.intent,
        ):
            return False, "intent assigns an unsupported surname or title to Eldon"
        if re.search(
            r"\bкарт(?:а|у|е|ой|ы)\b",
            decision.intent,
            re.IGNORECASE,
        ):
            return False, "intent invents an undeclared inventory item"
        if re.search(
            r"\b(?:фланелев\w+|повязк\w*)\b",
            decision.intent,
            re.IGNORECASE,
        ):
            return False, "intent invents an undeclared inventory item"
        if re.search(
            r"\b(?:оставш\w+|оказавш\w+)\b.{0,120}\bпосле\b|"
            r"\bпосле\s+(?:прикосновения|перехода|прибытия)\b",
            decision.intent,
            re.IGNORECASE,
        ):
            return False, "intent invents an unsupported prior outcome"
        if re.search(
            r"\b(?:он|элдон)\s+знает,\s+что\b.{0,180}\b"
            r"(?:собеседник\w*|остальн\w*|они|группа)\s+буд\w*",
            decision.intent,
            re.IGNORECASE,
        ):
            return False, "intent invents future NPC reactions"
        if decision.mode in {"question", "dialogue"}:
            intent_folded = decision.intent.casefold()
            mentioned = {
                name.casefold()
                for name in active_npcs
                if any(
                    re.search(rf"\b{re.escape(stem)}[а-яё]*\b", intent_folded)
                    for stem in _npc_name_stems(name)
                )
            }
            target_folded = decision.target.casefold()
            if mentioned and target_folded not in mentioned:
                return False, (
                    f"intent addresses {sorted(mentioned)!r} but target is "
                    f"{decision.target!r}"
                )
            if decision.mode == "question" and "?" not in decision.intent:
                return False, "question mode has no question"
            if (
                not re.search(r"\b(?:я|мне|мой|моя|моё|элдон)\b", intent_folded)
                and any(
                    re.search(
                        rf"\b{re.escape(stem)}[а-яё]*\s+"
                        r"(?:обращается|говорит|спрашивает|предлагает|решает|"
                        r"пытается|осматривает|ид[её]т|делает)\b",
                        intent_folded,
                    )
                    for name in active_npcs
                    for stem in _npc_name_stems(name)
                )
            ):
                return False, "intent makes an NPC the acting subject"
        if any(re.search(pattern, decision.intent, re.IGNORECASE) for pattern in OUTCOME_PATTERNS):
            self.rejected_outcomes += 1
            return False, "intent declares an outcome"
        fingerprint = self.fingerprint(decision.intent)
        if fingerprint and fingerprint in self.recent_fingerprints:
            self.repeated_actions += 1
            return False, "intent repeats a recent action"
        return True, None

    def remember(self, decision: PlayerDecision) -> None:
        self.recent_fingerprints.append(self.fingerprint(decision.intent))
        self.mode_counts[decision.mode] += 1
        self.target_counts[decision.target.casefold()] += 1

    def fallback(
        self,
        active_npcs: list[str],
        mode: str,
        objective: str,
        latest_result: str,
        active_theses: list[str],
        turn_number: int,
        *,
        count_fallback: bool = True,
    ) -> PlayerDecision:
        if count_fallback:
            self.fallbacks += 1
        target = self.suggested_target(active_npcs, mode)
        if mode == "dialogue" and turn_number >= 3:
            verana_target = next(
                (name for name in active_npcs if name.casefold().startswith("верана")),
                None,
            )
            if verana_target:
                target = verana_target
        scene_context = "\n".join([latest_result, *active_theses]).casefold()
        risk_request = (
            f"Я обращаюсь к {target}: «Скажите прямо, что в происходящем связано "
            "лично с вами и какую точную будущую цену потребует возвращение правды; "
            "не утверждайте, что цена уже уплачена»."
            if target.casefold().startswith("верана")
            else (
                f"Я формулирую просьбу: «{target}, прямо назовите личный риск, "
                "который группа принимает, если продолжит расследование выбранным способом»."
            )
        )
        targeted_variants: list[tuple[str, str, str]] = []
        if re.search(r"\b(?:дневник|книг\w*|книжк\w*)\b", scene_context):
            targeted_variants.append(
                (
                    "action",
                    "narrator",
                    "Я использую закреплённую верёвку как страховку и пробую вытянуть "
                    "дневник за край кожаного переплёта из чёрной лужи, не объявляя успех.",
                )
            )
        if turn_number >= 3 and re.search(
            r"\b(?:печат\w*|усыплен\w*|обряд\w+\s+забвени\w*|"
            r"последн\w+\s+т[её]пл\w+\s+воспоминан\w*)\b",
            scene_context,
        ):
            verana = next(
                (name for name in active_npcs if name.casefold().startswith("верана")),
                None,
            )
            if verana:
                targeted_variants.append(
                    (
                        "dialogue",
                        verana,
                        f"Я обращаюсь к {verana}: «Скажите прямо, что в этих знаках "
                        "кажется вам лично знакомым и какую точную цену вы ожидаете "
                        "за возвращение скрытой правды?»",
                    )
                )
        variants = targeted_variants + [
            (
                "question",
                target,
                f"Я задаю вопрос: «{target}, какой один наблюдаемый признак сейчас надёжнее всего укажет направление источника и изменит наш следующий шаг?»",
            ),
            (
                "dialogue",
                target,
                risk_request,
            ),
            (
                "plan",
                target,
                f"Я вслух предлагаю план: {target} может сравнить два ближайших следа воздействия, затем выбрать один безопасный проверяемый шаг, не объявляя результат заранее.",
            ),
            (
                "decision",
                target,
                f"Я ставлю перед группой выбор и обращаюсь к {target}: «Что проверяем сначала — защищённый образец или безопасный путь к зоне сильнейшего воздействия?»",
            ),
            (
                "action",
                "narrator",
                "Я использую обычные навыки руиниста: осматриваю следы, крепления и доступные пути, не касаясь магических элементов и не объявляя успех.",
            ),
            (
                "action",
                "narrator",
                "Я закрепляю верёвку, ставлю фонарь как ориентир и сравниваю силу воздействия в двух доступных точках, чтобы наметить безопасный следующий тест.",
            ),
        ]
        preferred_variants = [
            variant for variant in variants if variant[0] == mode
        ]
        other_variants = [
            variant for variant in variants if variant[0] != mode
        ]
        if preferred_variants:
            offset = turn_number % len(preferred_variants)
            preferred_variants = (
                preferred_variants[offset:] + preferred_variants[:offset]
            )
        for candidate_mode, candidate_target, intent in (
            preferred_variants + other_variants
        ):
            if candidate_mode != "action" and candidate_target == "narrator":
                candidate_target = self.suggested_target(
                    active_npcs,
                    candidate_mode,
                )
                intent = intent.replace("narrator", candidate_target)
            if candidate_target == "narrator" or candidate_target in active_npcs:
                decision = PlayerDecision(
                    target=candidate_target,
                    mode=candidate_mode,
                    intent=intent,
                )
                valid, _ = self.validate(decision, active_npcs)
                if valid:
                    return decision
        return PlayerDecision(
            target="narrator",
            mode="action",
            intent=(
                "Я кратко называю группе уже обнаруженные признаки, не упоминая ходов "
                f"или правил игры, затем предлагаю новую проверку по цели «{objective}».")
        )


def parse_json_object(raw: str) -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    match = JSON_OBJECT_PATTERN.search(clean)
    if not match:
        raise ValueError("response does not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise TypeError("response JSON is not an object")
    return value


CYRILLIC_TRANSLITERATION = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def _latin_slug(value: str) -> str:
    transliterated = value.casefold().translate(CYRILLIC_TRANSLITERATION)
    return "_".join(re.findall(r"[a-z0-9]+", transliterated))


def parse_player_decision(raw: str, active_npcs: list[str]) -> PlayerDecision:
    data = parse_json_object(raw)
    canonical = {name.casefold(): name for name in active_npcs}
    target_raw = str(data.get("target", "narrator")).strip()
    target_key = target_raw.casefold()
    if target_key in {"activenpc", "active_npc"} and active_npcs:
        target_key = active_npcs[0].casefold()
    elif "|" in target_key and "narrator" in target_key:
        target_key = "narrator"
    if target_key in {
        "eldon",
        "aldon",
        "elden",
        "элдон",
        "елдон",
        "player",
        "self",
        "игрок",
        "я",
    }:
        target_key = "narrator"
    if target_key not in canonical and target_key != "narrator":
        target_slug = _latin_slug(target_key)
        scored = sorted(
            (
                SequenceMatcher(None, target_slug, _latin_slug(name)).ratio(),
                name,
            )
            for name in active_npcs
        )
        if scored and scored[-1][0] >= 0.78:
            runner_up = scored[-2][0] if len(scored) > 1 else 0.0
            if scored[-1][0] - runner_up >= 0.08:
                target_key = scored[-1][1].casefold()
    target = canonical.get(
        target_key,
        "narrator" if target_key == "narrator" else target_raw,
    )
    mode = str(data.get("mode", "action")).strip().casefold()
    if mode not in {"action", "dialogue", "question", "plan", "decision"}:
        mode = "action"
    if target == "narrator" and mode in {"dialogue", "question"} and active_npcs:
        target = active_npcs[0]
    return PlayerDecision(
        target=target,
        mode=mode,
        intent=str(data.get("intent", "")).strip(),
    )


def eldon_card() -> CharacterDraft:
    return CharacterDraft(
        canonical_name="Eldon",
        description="Практичный руинист экспедиции, ищущей источник чёрного дождя.",
        appearance="Потёртый дорожный плащ, короткие тёмные волосы и шрам над правой бровью.",
        personality="Практичный, подозрительный к лёгким ответам, суховатый, но умеющий слушать.",
        values=["жизнь спутников", "проверяемые свидетельства", "свобода выбора"],
        fears=["стать чужим инструментом", "потерять людей из-за безрассудного любопытства"],
        desires=["найти источник чёрного дождя", "вывести экспедицию живой"],
        voice="Низкий, прямой, с сухим юмором.",
        speech_patterns="Задаёт конкретные вопросы и называет риск до действия.",
        biography=(
            "Бывший охранник караванов и исследователь руин. Во время первого чёрного "
            "ливня он настоял на опасном маршруте и потерял наставника."
        ),
        backstory_public=(
            "Элдон присоединился к экспедиции, потому что умеет читать следы в руинах "
            "и уже пережил один чёрный ливень."
        ),
        secrets=[
            "Элдон винит себя в гибели наставника и боится, что разгадка потребует "
            "от него отказаться от последней вещи, оставшейся от погибшего."
        ],
        emotional_state="настороженное любопытство",
        current_intentions=[
            "понять, кому можно доверять",
            "проследить воздействие чёрного дождя до источника",
        ],
        goals=[
            "Найти источник чёрного дождя",
            "Сохранить экспедицию",
            "Не переложить личную цену разгадки на спутников",
        ],
        capabilities=["осматривать обычные механизмы", "работать простыми отмычками", "сражаться кинжалом", "лазать с верёвкой", "замечать практическую опасность"],
        limitations=["не умеет колдовать", "не распознаёт сверхъестественное без помощи", "не использует продвинутую технику", "не объявляет результаты своих действий"],
        equipment=["дорожный фонарь", "пеньковая верёвка", "обычный кинжал", "набор простых отмычек", "фляга"],
        initial_beliefs=[
            "Чёрный дождь имеет конкретный источник, а не является обычной погодой."
        ],
        visual_profile={"palette": "brown, iron and weathered green"},
    )


def deterministic_fallback_card(seed: NpcConcept, location_id: UUID) -> CharacterDraft:
    role = seed.campaign_role
    tone = seed.tone
    concept_words = " ".join(seed.concept.split())
    return CharacterDraft(
        canonical_name=seed.name,
        description=f"{role.capitalize()}; в экспедиции выполняет одну чёткую функцию и скрывает личную ставку.",
        appearance=f"Походная одежда {seed.name}, приспособленная к роли: {role}; заметная деталь связана с профессией.",
        face_description=f"Выражение лица {seed.name} отражает манеру: {tone}.",
        body_description="Телосложение и осанка соответствуют повседневной работе, без сверхъестественных особенностей.",
        immutable_features=f"Узнаваемая профессиональная деталь {seed.name}, которую нельзя случайно потерять между сценами.",
        personality=tone,
        values=["профессиональная компетентность", "личная автономия", "выживание группы"],
        fears=[f"что станет известно: {concept_words[:180]}", "потерять контроль над своей ролью в экспедиции"],
        desires=["выполнить свою задачу", "сохранить личный секрет до подходящего момента"],
        voice=f"Манера речи следует описанию «{tone}» и отличается от остальных участников.",
        speech_patterns=f"Использует лексику своей роли ({role}), отвечает на конкретный вопрос и не повторяет универсальные формулы о риске.",
        biography=f"Присоединился к экспедиции как {role} после события, связанного с личным секретом.",
        backstory_public=f"Группе известен как {role}.",
        secrets=[seed.concept],
        emotional_state=f"собранность, окрашенная чертой: {tone}",
        current_intentions=["проявить полезность в текущей сцене", "не раскрыть секрет без причины"],
        goals=["продвинуть текущую цель экспедиции", "разрешить личный конфликт, не разрушив группу"],
        capabilities=[f"надёжно применять знания по роли: {role}", "замечать детали, относящиеся к своей профессии"],
        limitations=["не использует способности вне своей роли", "не знает чужих секретов без передачи знания"],
        equipment=[f"личный дорожный набор {seed.name}", f"профессиональные инструменты {seed.name}"],
        initial_beliefs=["Цитадель опаснее, чем утверждают публичные источники."],
        visual_profile={"role": role, "tone": tone, "fallback": True},
        current_location_id=location_id,
    )


async def build_character_card(
    provider: LLMProvider,
    config,
    api_key: str | None,
    seed: NpcConcept,
    location_id: UUID,
) -> tuple[CharacterDraft, str]:
    prompt = f"""Создай различимую карточку NPC для долгой русскоязычной кампании.
Верни только JSON с ключами CharacterDraft: canonical_name, description, appearance,
face_description, body_description, immutable_features, personality, values, fears,
desires, voice, speech_patterns, biography, backstory_public, secrets,
emotional_state, current_intentions, goals, capabilities, limitations, equipment,
initial_beliefs, visual_profile.

Имя: {seed.name}
Концепция: {seed.concept}
Роль: {seed.campaign_role}
Тон: {seed.tone}

Требования:
- Все текстовые поля на русском языке.
- Каждая карточка должна иметь отличимый голос и профессиональную лексику.
- 1-4 элемента в каждом списке.
- Никаких неограниченных сил, техники или предметов вне роли.
- Equipment содержит уникальные конкретные экземпляры с именем владельца.
"""
    raw = ""
    try:
        async for token in provider.generate_stream(
            [ChatMessage(role="system", content=prompt)],
            config,
            api_key,
            max_tokens=1400,
            temperature=0.45,
        ):
            raw += token
        payload = parse_json_object(raw)
        payload["current_location_id"] = location_id
        card = CharacterDraft.model_validate(payload)
        return card, "model"
    except (
        LLMProviderError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as first_error:
        repair_prompt = f"""Исправь JSON карточки NPC {seed.name}.
Верни только валидный JSON для CharacterDraft, без markdown. Все поля на русском.
Ошибка проверки: {first_error}
Повреждённый ответ:
{raw[-6000:]}
"""
        repaired = ""
        try:
            async for token in provider.generate_stream(
                [ChatMessage(role="system", content=repair_prompt)],
                config,
                api_key,
                max_tokens=1400,
                temperature=0.1,
            ):
                repaired += token
            payload = parse_json_object(repaired)
            payload["current_location_id"] = location_id
            card = CharacterDraft.model_validate(payload)
            return card, "repair"
        except (
            LLMProviderError,
            ValidationError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return deterministic_fallback_card(seed, location_id), "fallback"


class ScenarioDirector:
    def __init__(
        self,
        session: AsyncSession,
        campaign_id: UUID,
        player_id: UUID,
        provider: LLMProvider,
        config,
        api_key: str | None,
        stats: Counter,
    ):
        self.session = session
        self.campaign_id = campaign_id
        self.player_id = player_id
        self.provider = provider
        self.config = config
        self.api_key = api_key
        self.stats = stats
        self.campaign_service = CampaignService(session)
        self.entities = EntityRepository(session)
        self.scenes = SceneRepository(session)
        self.events = EventRepository(session)
        self.curator = ThesisCurator(session)
        self.characters: dict[str, UUID] = {"Eldon": player_id}
        self.current: PhaseRuntime | None = None

    async def restore_characters(self) -> None:
        for entity in await self.entities.list_by_campaign(self.campaign_id, "character"):
            self.characters[entity.canonical_name] = entity.id

    async def ensure_npc(self, name: str, location_id: UUID) -> UUID:
        if name in self.characters:
            return self.characters[name]
        seed = NPCS[name]
        card, source = await build_character_card(
            self.provider,
            self.config,
            self.api_key,
            seed,
            location_id,
        )
        self.stats[f"character_builder_{source}"] += 1
        built = await create_character_from_draft(
            self.campaign_id,
            card.model_copy(update={"current_location_id": location_id}),
            session=self.session,
        )
        self.characters[name] = built.character.id
        return built.character.id

    async def close_current(self, status: str, summary: str, source_turn_id: UUID | None) -> None:
        if not self.current:
            return
        await self.events.create(
            self.campaign_id,
            EventCreate(
                event_type="scene_outcome",
                description=f"[{status}] {summary}",
                location_id=self.current.location_id,
                importance="important",
                participant_ids=list(self.current.active_characters.values()),
                source_turns=[source_turn_id] if source_turn_id else [],
            ),
        )
        await self.curator.close_scene(self.current.scene_id)
        await self.scenes.update(
            self.current.scene_id,
            SceneUpdate(status="completed"),
        )
        await self.session.commit()

    async def enter_phase(self, index: int, state: SimulationState) -> PhaseRuntime:
        phase = PHASES[index]
        existing_scenes = await self.scenes.list_by_campaign(self.campaign_id)
        existing = next(
            (scene for scene in existing_scenes if scene.title == phase.title),
            None,
        )
        if existing and existing.status == "active":
            participants = await self.scenes.get_participants(existing.id)
            active = {
                name: entity_id
                for name, entity_id in self.characters.items()
                if entity_id in participants
            }
            player = await self.entities.get_character(self.player_id)
            runtime = PhaseRuntime(
                index=index,
                phase=phase,
                scene_id=existing.id,
                location_id=player.current_location_id,
                active_characters=active,
                phase_turn=state.phase_turn,
                injected_pulses=set(state.injected_pulses),
                confirmed_pulses=set(state.confirmed_pulses),
            )
            self.current = runtime
            return runtime

        location = await self.entities.create(
            self.campaign_id,
            EntityCreate(
                entity_type=EntityType.LOCATION,
                canonical_name=phase.title,
                description=phase.location_description,
                custom_fields={"scenario_phase": phase.slug},
            ),
        )
        for name in dict.fromkeys((*phase.introduced_npcs, *phase.active_npcs)):
            await self.ensure_npc(name, location.id)
        active = {name: self.characters[name] for name in phase.active_npcs}
        active["Eldon"] = self.player_id
        scene = await self.scenes.create(
            self.campaign_id,
            SceneCreate(
                title=phase.title,
                location_description=phase.location_description,
                mood=phase.mood,
                tension=phase.tension,
            ),
        )
        for character_id in active.values():
            await self.scenes.add_participant(scene.id, character_id)
            await self.entities.update_character(
                character_id,
                CharacterUpdate(current_location_id=location.id),
            )
        await self.campaign_service.update_campaign(
            self.campaign_id,
            CampaignUpdate(current_scene_id=scene.id),
        )
        await self.scenes.create_thesis(
            scene.id,
            SceneThesisCreate(
                thesis_type=ThesisType.INTENTION,
                text=f"Режиссёрская граница: {phase.director_note}",
                priority=10,
                visibility="dm",
                pinned=True,
                related_entity_ids=[self.player_id],
            ),
        )
        for seed in phase.opening_theses:
            await self.scenes.create_thesis(
                scene.id,
                SceneThesisCreate(
                    thesis_type=seed.thesis_type,
                    text=seed.text,
                    priority=seed.priority,
                    visibility=seed.visibility,
                    related_entity_ids=[
                        self.characters[name]
                        for name in seed.related_names
                        if name in self.characters
                    ],
                ),
            )
        await self.events.create(
            self.campaign_id,
            EventCreate(
                event_type="scene_transition",
                description=f"Экспедиция вошла в сцену {phase.title}. Цель: {phase.objective}",
                location_id=location.id,
                importance="important",
                participant_ids=list(active.values()),
            ),
        )
        await self.session.commit()
        runtime = PhaseRuntime(
            index=index,
            phase=phase,
            scene_id=scene.id,
            location_id=location.id,
            active_characters=active,
        )
        self.current = runtime
        return runtime

    async def inject_due_pulses(self, runtime: PhaseRuntime, hard_limit: int) -> None:
        thresholds = [
            max(2, round(pulse.at_fraction * hard_limit))
            for pulse in runtime.phase.pulses
        ]
        active_theses = await self.scenes.list_theses_by_scene(
            runtime.scene_id,
            active_only=True,
        )
        existing_texts = {thesis.text for thesis in active_theses}
        for pulse_index, pulse in enumerate(runtime.phase.pulses):
            if pulse_index in runtime.injected_pulses:
                continue
            if runtime.phase_turn < thresholds[pulse_index]:
                continue
            pulse_text = (
                f"Неразыгранное осложнение: {pulse.event}. {pulse.thesis.text}"
            )
            if pulse_text in existing_texts:
                runtime.injected_pulses.add(pulse_index)
                continue
            await self.scenes.create_thesis(
                runtime.scene_id,
                SceneThesisCreate(
                    thesis_type=pulse.thesis.thesis_type,
                    text=pulse_text,
                    priority=pulse.thesis.priority,
                    visibility=pulse.thesis.visibility,
                    related_entity_ids=[
                        self.characters[name]
                        for name in pulse.thesis.related_names
                        if name in self.characters
                    ],
                ),
            )
            existing_texts.add(pulse_text)
            runtime.injected_pulses.add(pulse_index)
            self.stats["pulses_injected"] += 1
        await self.session.commit()

    async def confirm_pulses(
        self,
        runtime: PhaseRuntime,
        indexes: list[int],
        source_turn_id: UUID,
    ) -> None:
        for index in indexes:
            if index not in runtime.injected_pulses or index in runtime.confirmed_pulses:
                continue
            if index < 0 or index >= len(runtime.phase.pulses):
                continue
            pulse = runtime.phase.pulses[index]
            await self.events.create(
                self.campaign_id,
                EventCreate(
                    event_type="scenario_pulse",
                    description=pulse.event,
                    location_id=runtime.location_id,
                    importance="important",
                    participant_ids=list(runtime.active_characters.values()),
                    source_turns=[source_turn_id],
                ),
            )
            runtime.confirmed_pulses.add(index)
            self.stats["pulses_confirmed"] += 1
        await self.session.commit()


async def generate_player_decision(
    provider: LLMProvider,
    config,
    api_key: str | None,
    compiler: ContextCompiler,
    campaign_id: UUID,
    runtime: PhaseRuntime,
    player_id: UUID,
    history: list,
    policy: PlayerPolicy,
    turn_number: int,
    active_theses: list[str],
) -> PlayerDecision:
    active_npcs = list(runtime.phase.active_npcs)
    preferred = policy.preferred_mode(turn_number)
    suggested = policy.suggested_target(active_npcs, preferred)
    messages, _ = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=player_id,
        scene_id=runtime.scene_id,
        max_budget_override=1500,
    )
    trusted_context = messages[0].content if messages else ""
    recent = "\n".join(
        f"{'ДМ' if turn.role == 'assistant' else 'ИГРОК'}: {turn.content}"
        for turn in history[-10:]
    )
    system = f"""Ты имитируешь живого игрока настольной RPG, а не второго ДМа.
Верни один JSON: {{"target":"narrator" или конкретное имя из списка АКТИВНЫЕ NPC,"mode":"action|dialogue|question|plan|decision","intent":"1-3 предложения"}}.

ЦЕЛЬ СЦЕНЫ: {runtime.phase.objective}
АКТИВНЫЕ NPC: {', '.join(active_npcs)}
ПРЕДПОЧТИТЕЛЬНЫЙ ТИП ХОДА: {preferred}
НЕДОИСПОЛЬЗОВАННЫЙ NPC: {suggested}
ТЕКУЩИЕ ТЕЗИСЫ: {' | '.join(active_theses[-8:])}

Правила:
- intent только на русском языке.
- Опиши только речь, вопрос, план, решение или попытку действия Элдона.
- Не объявляй успех, находку, урон, реакцию NPC, открытие двери или смену сцены.
- Реагируй на последний результат, а не на абстрактное «препятствие».
- Используй конкретный предмет, наблюдение или компетенцию, когда это уместно.
- После похожей попытки меняй подход.
- Не повторяй фразы из списка недавних действий.

ДОВЕРЕННЫЙ КОНТЕКСТ ЭЛДОНА:
{trusted_context}"""
    user = (
        f"НЕДАВНЯЯ ИГРА:\n{recent or '(начало сцены)'}\n\n"
        "НЕДАВНИЕ ДЕЙСТВИЯ, КОТОРЫЕ НЕЛЬЗЯ ПОВТОРЯТЬ:\n"
        + "\n".join(policy.recent_fingerprints)
    )
    error = None
    for _ in range(2):
        raw = ""
        correction = f"\nПредыдущий JSON отклонён: {error}." if error else ""
        try:
            async for token in provider.generate_stream(
                [
                    ChatMessage(role="system", content=system + correction),
                    ChatMessage(role="user", content=user),
                ],
                config,
                api_key,
                max_tokens=1024,
                temperature=0.75,
            ):
                raw += token
            decision = parse_player_decision(raw, active_npcs)
            valid, error = policy.validate(decision, active_npcs)
            if valid:
                policy.remember(decision)
                return decision
        except (ValueError, ValidationError, LLMProviderError) as exc:
            error = str(exc)

    latest_result = next(
        (turn.content for turn in reversed(history) if turn.role == "assistant"),
        "",
    )
    decision = policy.fallback(
        active_npcs,
        preferred,
        runtime.phase.objective,
        latest_result,
        active_theses,
        turn_number,
    )
    policy.remember(decision)
    return decision


async def evaluate_objective(
    provider: LLMProvider,
    config,
    api_key: str | None,
    runtime: PhaseRuntime,
    recent_history: list,
    assistant_content: str,
    active_theses: list[str],
    minimum_turns: int,
) -> ObjectiveEvaluation:
    if runtime.phase_turn < minimum_turns:
        return ObjectiveEvaluation(
            status="progressing",
            evidence=f"Минимальная длина сцены ещё не достигнута: {runtime.phase_turn}/{minimum_turns}",
        )
    pending = [
        f"{index}: {runtime.phase.pulses[index].event}"
        for index in sorted(runtime.injected_pulses - runtime.confirmed_pulses)
    ]
    recent = "\n".join(
        f"{'ДМ' if turn.role == 'assistant' else 'ИГРОК'}: {turn.content}"
        for turn in recent_history[-8:]
    )
    prompt = f"""Ты проверяешь фактическое состояние цели сцены RPG.
Не оценивай качество прозы. Верни только JSON:
{{"status":"progressing|resolved|failed|blocked","evidence":"короткая цитата или факт","outcome_summary":"итог или null","confirmed_pulses":[индексы]}}

ЦЕЛЬ: {runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {runtime.phase_turn}
КРИТЕРИИ: {chr(10).join(f"- {item.key}: {item.description}" for item in runtime.phase.completion_criteria) or '- нет'}
DURABLE CHANGES: {chr(10).join(runtime.durable_changes[-50:]) or '- нет'}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
ОЖИДАЮЩИЕ ОСЛОЖНЕНИЯ:
{chr(10).join(pending) or '- нет'}

НЕДАВНЯЯ ИГРА:
{recent}

ПОСЛЕДНИЙ РЕЗУЛЬТАТ ДМА:
{assistant_content}

Resolved только если цель действительно достигнута в повествовании.
Failed только если цель стала невозможна или выбран явный провал.
Blocked, если нужен новый подход, но сцена ещё продолжается.
Не считай режиссёрский тезис или план уже случившимся событием.
"""
    raw = ""
    try:
        async for token in provider.generate_stream(
            [ChatMessage(role="system", content=prompt)],
            config,
            api_key,
            max_tokens=320,
            temperature=0.1,
        ):
            raw += token
        return ObjectiveEvaluation.model_validate(parse_json_object(raw))
    except (
        LLMProviderError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return ObjectiveEvaluation(
            status="progressing",
            evidence="Evaluator недоступен; цель не считается выполненной без доказательства.",
        )


async def resolve_turn_proposals(
    session: AsyncSession,
    assistant_turn_id: UUID,
) -> tuple[list[str], list[str]]:
    repo = ProposedChangeRepository(session)
    accepted: list[str] = []
    rejected: list[str] = []
    for proposal in await repo.get_for_turn(assistant_turn_id):
        if proposal.status == "invalid":
            rejected.append(proposal.payload.get("_validation_error", "invalid proposal"))
            await repo.resolve(proposal.id, ProposalAction(status="rejected"))
            continue
        if proposal.status != "proposed":
            continue
        try:
            await resolve_proposal(
                proposal.id,
                ProposalAction(status="accepted"),
                session=session,
            )
            accepted.append(f"{proposal.change_type}: {proposal.payload}")
        except Exception as exc:  # noqa: BLE001
            # Proposal application is an isolation boundary: one malformed semantic
            # change must be rejected without aborting the whole benchmark turn.
            await session.rollback()
            rejected.append(f"{proposal.change_type}: {exc}")
    await session.commit()
    return accepted, rejected


async def find_logical_pair(
    session: AsyncSession,
    campaign_id: UUID,
    run_id: str,
    logical_turn: int,
):
    result = await session.execute(
        select(DBTurn)
        .where(DBTurn.campaign_id == str(campaign_id))
        .order_by(DBTurn.created_at)
    )
    user = None
    assistant = None
    for row in result.scalars().all():
        if not row.context_snapshot:
            continue
        try:
            snapshot = json.loads(row.context_snapshot)
        except (json.JSONDecodeError, TypeError):
            continue
        marker = snapshot.get("simulation") or {}
        if marker.get("run_id") != run_id or marker.get("logical_turn") != logical_turn:
            continue
        if row.role == "user":
            user = row
        elif row.role == "assistant":
            assistant = row
    if user and assistant is None:
        child = await session.execute(
            select(DBTurn)
            .where(
                DBTurn.parent_turn_id == user.id,
                DBTurn.role == "assistant",
                DBTurn.status == "active",
            )
            .order_by(DBTurn.created_at.desc())
        )
        assistant = child.scalars().first()
    return user, assistant


async def latest_assistant_for_user(session: AsyncSession, user_id: str):
    result = await session.execute(
        select(DBTurn)
        .where(DBTurn.parent_turn_id == user_id, DBTurn.role == "assistant")
        .order_by(DBTurn.created_at.desc())
    )
    return result.scalars().first()


async def count_campaign_rows(session, model, campaign_id: UUID) -> int:
    query = select(func.count()).select_from(model)
    if hasattr(model, "campaign_id"):
        query = query.where(model.campaign_id == str(campaign_id))
    elif model is SceneThesis:
        query = query.join(Scene, Scene.id == SceneThesis.scene_id).where(
            Scene.campaign_id == str(campaign_id)
        )
    return int((await session.execute(query)).scalar_one())


async def run_realistic_simulation_v2() -> None:
    global NPCS, PHASES
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path = Path(os.getenv("PDM_SIM_DB", str(data_dir / "realistic_simulation.db")))
    log_path = data_dir / "realistic_simulation_play.log"
    trace_path = data_dir / "realistic_simulation_trace.jsonl"
    report_path = data_dir / "realistic_simulation_report.md"
    state_path = data_dir / "realistic_simulation_state.json"
    scenario_path = data_dir / "realistic_simulation_scenario.json"

    should_reset = os.getenv("PDM_SIM_RESET", "1") == "1"
    if should_reset:
        for path in (
            database_path,
            database_path.with_suffix(database_path.suffix + "-wal"),
            database_path.with_suffix(database_path.suffix + "-shm"),
            log_path,
            trace_path,
            report_path,
            state_path,
            scenario_path,
        ):
            if path.exists():
                path.unlink()

    turns_limit = max(20, int(os.getenv("PDM_SIM_TURNS", "200")))
    model_name = os.getenv("PDM_SIM_MODEL", "gemma4:e4b")
    base_url = os.getenv("PDM_SIM_BASE_URL", "http://127.0.0.1:11434/v1")
    context_window = int(os.getenv("PDM_SIM_CONTEXT_WINDOW", "8192"))
    stop_on_failure = os.getenv("PDM_SIM_STOP_ON_PROVIDER_FAILURE", "1") == "1"

    state = SimulationState.load(state_path)
    if not state:
        state = SimulationState(run_id=os.getenv("PDM_SIM_RUN_ID", str(uuid4())))
    trace = TraceStore(trace_path)

    alembic_revision = upgrade_simulation_database(database_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        campaigns = CampaignService(session)
        campaign_repo = CampaignRepository(session)
        existing_campaigns = await campaign_repo.list_all()
        campaign = None
        if state.campaign_id:
            campaign = await campaign_repo.get_by_id(UUID(state.campaign_id))
        if not campaign and existing_campaigns and not should_reset:
            campaign = next(
                (
                    item
                    for item in existing_campaigns
                    if item.name == "Хроники Бездны: реалистичная автономная кампания"
                ),
                None,
            )
        simulation_system_instructions = (
            "Ты приземлённый Dungeon Master в жанре тёмного фэнтези. "
            "Пиши исключительно на русском языке. Игрок заявляет только намерения; "
            "ты определяешь исходы и даёшь конкретные последствия. Уважай карточки NPC, "
            "инвентарь, способности, знания и тезисы. Не говори за Элдона, не повторяй "
            "универсальные формулы о риске и не заменяй действие новым вопросом. "
            "Вообще не описывай действия, позу, взгляд, мысли или реакции Элдона: "
            "даже при проверке его действия описывай только наблюдаемый результат "
            "в мире и ответы NPC. Имя Элдона допустимо лишь в обращении NPC. "
            "Мир доиндустриальный: не вводи современную технику или научную фантастику. "
            "Оставайся строго в текущей описанной локации; не добавляй лес, чащу, "
            "холмы или другую местность к уличной сцене поселения. "
            "Не используй современный научный и медицинский жаргон: никаких градиентов, "
            "эпицентров, экссудации, дифференциальной диагностики, химического состава "
            "или перенасыщенных растворов; наблюдения называй простыми ремесленными словами. "
            "Секрет из контекста нельзя упоминать даже намёком, пока активный тезис "
            "или событие прямо не сделает его наблюдаемым. Не придумывай NPC новые "
            "предметы: используй только явно перечисленный инвентарь. "
            "Не приписывай предметам магические свойства, которых нет в каноне. "
            "Каждое режиссёрское событие происходит только один раз; после него "
            "описывай последствия, но не разыгрывай то же появление заново. "
            "Если в контексте есть «Неразыгранное осложнение», разыграй его в этом "
            "ответе прежде любого нового события. Если Элдон прямо спрашивает Верану "
            "о её личной связи и точной цене, она обязана назвать заданные в "
            "режиссёрской границе обряд, утрату имени и лица наставника и последнее "
            "тёплое воспоминание; не подменяй эту цену золотом, свободой или статусом. "
            "До последней фазы терминальной арки цена только названа и ожидается: "
            "никогда не утверждай, что она уже заплачена или что воспоминание уже потеряно. "
            "Если игрок спрашивает об одном наблюдаемом признаке направления, NPC "
            "обязан прямо назвать ровно один физический признак и сказать, в какую "
            "сторону он ведёт; не заменяй ответ общей теорией угрозы. "
            "Пиши естественной грамотной прозой без нагромождения метафор; "
            "каждую реплику явно связывай с говорящим."
        )
        simulation_narrative_style = (
            "Два-четыре компактных абзаца романной прозы, конкретные сенсорные детали, "
            "различимые голоса NPC и завершённый результат каждого хода. "
            "Не более одной развёрнутой метафоры на абзац."
        )
        if not campaign:
            campaign = await campaigns.create_campaign(
                CampaignCreate(
                    name="Хроники Бездны: реалистичная автономная кампания",
                    description="Objective-driven LLM-vs-LLM benchmark with idempotent resume.",
                    system_instructions=simulation_system_instructions,
                    narrative_style=simulation_narrative_style,
                )
            )
        elif (
            campaign.system_instructions != simulation_system_instructions
            or campaign.narrative_style != simulation_narrative_style
        ):
            campaign = await campaigns.update_campaign(
                campaign.id,
                CampaignUpdate(
                    system_instructions=simulation_system_instructions,
                    narrative_style=simulation_narrative_style,
                ),
            )
            assert campaign is not None
        state.campaign_id = str(campaign.id)
        state.save(state_path)
        campaign_id = campaign.id

        config = await campaigns.configure_provider(
            campaign_id,
            ProviderConfigCreate(
                base_url=base_url,
                model_name=model_name,
                context_window=context_window,
            ),
        )
        config_repo = ProviderConfigRepository(session)
        await config_repo.get_decrypted_key(campaign_id)
        role_router = RoleModelRouter(config_repo)
        provider = LLMProvider()
        builder_selection = await role_router.resolve(
            campaign_id,
            ModelRole.CHARACTER_BUILDER,
            config,
        )
        evaluator_selection = await role_router.resolve(
            campaign_id,
            ModelRole.EVALUATOR,
            config,
        )
        player_selection = await role_router.resolve(
            campaign_id,
            ModelRole.PLAYER,
            config,
        )
        scenario_selection = await role_router.resolve(
            campaign_id,
            ModelRole.SCENARIO_BUILDER,
            config,
        )
        if any(
            selection is None
            for selection in (
                builder_selection,
                evaluator_selection,
                player_selection,
                scenario_selection,
            )
        ):
            raise RuntimeError("Role model routing requires a configured campaign provider")
        entities = EntityRepository(session)
        characters = await entities.list_by_campaign(campaign_id, "character")
        player_entity = next(
            (entity for entity in characters if entity.canonical_name == "Eldon"),
            None,
        )
        if not player_entity:
            player = await create_character_from_draft(
                campaign_id,
                eldon_card(),
                session=session,
            )
            player_id = player.character.id
        else:
            player_id = player_entity.id

        if campaign.player_character_id != player_id:
            campaign = await campaigns.update_campaign(
                campaign_id, CampaignUpdate(player_character_id=player_id)
            )
            await session.commit()

        catalog = await ensure_phase_available(
            path=scenario_path,
            reset=should_reset,
            phase_index=state.phase_index,
            provider=provider,
            router=role_router,
            selection=scenario_selection,
            previous_outcomes=state.player_journal,
        )
        NPCS = catalog.runtime_npcs()
        PHASES = catalog.runtime_phases()

        stats: Counter = Counter()
        director = ScenarioDirector(
            session,
            campaign_id,
            player_id,
            provider,
            builder_selection.config,
            builder_selection.api_key,
            stats,
        )
        await director.restore_characters()
        runner = TurnRunner(session)
        compiler = ContextCompiler(session)
        turns = TurnRepository(session)
        scenes = SceneRepository(session)
        policy = PlayerPolicy()
        started = time.time()

        while state.logical_turn <= turns_limit and not state.completed:
            if state.phase_index >= len(PHASES):
                if catalog.arcs and catalog.arcs[-1].terminal:
                    state.completed = True
                    state.save(state_path)
                    break
                catalog = await ensure_phase_available(
                    path=scenario_path,
                    reset=False,
                    phase_index=state.phase_index,
                    provider=provider,
                    router=role_router,
                    selection=scenario_selection,
                    previous_outcomes=state.player_journal,
                )
                NPCS = catalog.runtime_npcs()
                PHASES = catalog.runtime_phases()
                await director.restore_characters()

            runtime = await director.enter_phase(state.phase_index, state)
            runtime.phase_turn = state.phase_turn
            runtime.injected_pulses = set(state.injected_pulses)
            runtime.confirmed_pulses = set(state.confirmed_pulses)
            runtime.criteria_met = set(state.criteria_met)
            runtime.durable_changes = list(state.durable_changes)
            minimum_phase_turns = max(4, int(runtime.phase.min_turns))
            hard_phase_limit = max(
                minimum_phase_turns + 4,
                int(runtime.phase.max_turns),
            )
            await director.inject_due_pulses(runtime, hard_phase_limit)

            active_theses_rows = await scenes.list_theses_by_scene(
                runtime.scene_id,
                active_only=True,
            )
            active_thesis_texts = [item.text for item in active_theses_rows]
            history = await turns.get_history(campaign_id, limit=20, active_only=True)

            existing_user, existing_assistant = await find_logical_pair(
                session,
                campaign_id,
                state.run_id,
                state.logical_turn,
            )

            if existing_assistant and existing_assistant.content.lstrip().startswith("[Generation failed"):
                print(f"[simulation] Deleting failed logical turn {state.logical_turn} from DB to re-attempt.")
                from sqlalchemy import delete as sql_delete
                if existing_user:
                    await session.execute(
                        sql_delete(DBTurn).where(
                            DBTurn.parent_turn_id == existing_user.id
                        )
                    )
                    await session.execute(sql_delete(DBTurn).where(DBTurn.id == existing_user.id))
                else:
                    await session.execute(
                        sql_delete(DBTurn).where(
                            DBTurn.id == existing_assistant.id
                        )
                    )
                await session.commit()
                existing_user = None
                existing_assistant = None
                state.consecutive_failures = 0
                state.save(state_path)

            if existing_assistant and existing_assistant.status == "active":
                decision = PlayerDecision(
                    target="narrator",
                    mode="action",
                    intent="Восстановленный после сбоя ход; исход уже сохранён в БД.",
                )
                dm_text = existing_assistant.content
                assistant_turn_id = UUID(existing_assistant.id)
                from app.services.post_turn_processor import PostTurnProcessor

                await PostTurnProcessor(session).process_turn(assistant_turn_id)
                accepted, rejected = await resolve_turn_proposals(
                    session,
                    assistant_turn_id,
                )
                for change in accepted:
                    if change not in runtime.durable_changes:
                        runtime.durable_changes.append(change)
                runtime.durable_changes = runtime.durable_changes[-80:]
            else:
                decision = await generate_player_decision(
                    provider,
                    player_selection.config,
                    player_selection.api_key,
                    compiler,
                    campaign_id,
                    runtime,
                    player_id,
                    history,
                    policy,
                    state.logical_turn,
                    active_thesis_texts,
                )
                player_text = decision.render()
                actor_id = (
                    runtime.active_characters.get(decision.target)
                    if decision.target != "narrator"
                    else None
                )
                simulation_marker = {
                    "run_id": state.run_id,
                    "logical_turn": state.logical_turn,
                    "phase_index": state.phase_index,
                    "phase_slug": runtime.phase.slug,
                }
                existing_user_id = None
                if existing_user:
                    existing_user.status = "active"
                    existing_user_id = UUID(existing_user.id)
                    await session.flush()
                dm_text = ""
                async for token in runner.run_turn_stream(
                    campaign_id,
                    TurnCreate(
                        role="user",
                        content=player_text,
                        scene_id=runtime.scene_id,
                        acting_character_id=actor_id,
                        context_snapshot={"simulation": simulation_marker},
                    ),
                    existing_user_turn_id=existing_user_id,
                ):
                    dm_text += token
                await session.commit()
                user_row, assistant_row = await find_logical_pair(
                    session,
                    campaign_id,
                    state.run_id,
                    state.logical_turn,
                )
                if assistant_row is None and user_row is not None:
                    assistant_row = await latest_assistant_for_user(session, user_row.id)
                assistant_turn_id = UUID(assistant_row.id) if assistant_row else None
                rejected_dm_text = dm_text
                narrative_valid, narrative_error = validate_russian_narrative(dm_text)
                if narrative_valid:
                    arc = catalog.arcs[runtime.phase.arc_index]
                    allow_paid_cost = bool(
                        arc.terminal
                        and runtime.phase.slug == arc.phases[-1].slug
                    )
                    narrative_valid, narrative_error = validate_dm_player_agency(
                        dm_text,
                        runtime.phase.location_description,
                        list(runtime.phase.active_npcs),
                        decision.mode,
                        "\n".join(turn.content for turn in history[-12:]),
                        allow_paid_cost,
                    )
                if not narrative_valid and assistant_row is not None:
                    dm_text = f"[Generation failed: narrative quality: {narrative_error}]"
                    assistant_row.content = dm_text
                    await session.commit()
                if "[Generation failed" in dm_text or not assistant_turn_id:
                    state.consecutive_failures += 1
                    stats["generation_failures"] += 1
                    trace.upsert(
                        {
                            "turn": state.logical_turn,
                            "run_id": state.run_id,
                            "phase": runtime.phase.slug,
                            "phase_title": runtime.phase.title,
                            "objective": runtime.phase.objective,
                            "active_npcs": list(runtime.phase.active_npcs),
                            "player": asdict(decision),
                            "dm": dm_text.strip(),
                            "rejected_dm": rejected_dm_text.strip(),
                            "generation_failed": True,
                            "active_theses": [
                                {
                                    "id": str(item.id),
                                    "type": item.thesis_type,
                                    "text": item.text,
                                    "visibility": item.visibility,
                                }
                                for item in active_theses_rows
                            ],
                        }
                    )
                    trace.write_play_log(log_path, turns_limit)
                    state.injected_pulses = sorted(runtime.injected_pulses)
                    state.confirmed_pulses = sorted(runtime.confirmed_pulses)
                    state.save(state_path)
                    if stop_on_failure:
                        print(
                            f"[simulation] Provider failure on logical turn {state.logical_turn}; "
                            "state saved for idempotent resume."
                        )
                        break
                    state.logical_turn += 1
                    state.save(state_path)
                    continue

                state.consecutive_failures = 0
                accepted, rejected = await resolve_turn_proposals(
                    session,
                    assistant_turn_id,
                )
                for change in accepted:
                    if change not in runtime.durable_changes:
                        runtime.durable_changes.append(change)
                runtime.durable_changes = runtime.durable_changes[-80:]

            active_theses_rows = await scenes.list_theses_by_scene(
                runtime.scene_id,
                active_only=True,
            )
            active_thesis_texts = [item.text for item in active_theses_rows]
            recent_history = await turns.get_history(campaign_id, limit=12, active_only=True)
            evaluation = await evaluate_objective(
                provider,
                evaluator_selection.config,
                evaluator_selection.api_key,
                runtime,
                recent_history,
                dm_text,
                active_thesis_texts,
                minimum_phase_turns,
            )
            if assistant_turn_id:
                await director.confirm_pulses(
                    runtime,
                    evaluation.confirmed_pulses,
                    assistant_turn_id,
                )

            force_close = runtime.phase_turn + 1 >= hard_phase_limit
            phase_finished = evaluation.status in {"resolved", "failed"} or force_close
            if force_close and evaluation.status not in {"resolved", "failed"}:
                evaluation = ObjectiveEvaluation(
                    status="failed",
                    evidence="Достигнут жёсткий лимит сцены без подтверждённого достижения цели.",
                    outcome_summary=(
                        "Группа покидает сцену с незавершённой целью; следующая сцена получает "
                        "явное последствие этого провала."
                    ),
                    confirmed_pulses=evaluation.confirmed_pulses,
                )

            record = {
                "turn": state.logical_turn,
                "run_id": state.run_id,
                "phase": runtime.phase.slug,
                "phase_title": runtime.phase.title,
                "phase_turn": runtime.phase_turn + 1,
                "objective": runtime.phase.objective,
                "active_npcs": list(runtime.phase.active_npcs),
                "player": asdict(decision),
                "dm": dm_text.strip(),
                "generation_failed": False,
                "accepted": accepted,
                "rejected": rejected,
                "evaluation": evaluation.model_dump(),
                "active_theses": [
                    {
                        "id": str(item.id),
                        "type": item.thesis_type,
                        "text": item.text,
                        "visibility": item.visibility,
                    }
                    for item in active_theses_rows
                ],
            }
            trace.upsert(record)
            trace.write_play_log(log_path, turns_limit)

            state.logical_turn += 1
            runtime.phase_turn += 1
            state.phase_turn = runtime.phase_turn
            state.injected_pulses = sorted(runtime.injected_pulses)
            state.confirmed_pulses = sorted(runtime.confirmed_pulses)
            state.criteria_met = sorted(runtime.criteria_met)
            state.durable_changes = list(runtime.durable_changes)

            if phase_finished:
                await director.close_current(
                    evaluation.status,
                    evaluation.outcome_summary or evaluation.evidence,
                    assistant_turn_id,
                )
                state.player_journal.append(
                    evaluation.outcome_summary
                    or evaluation.evidence
                    or f"Сцена {runtime.phase.title} завершена со статусом {evaluation.status}."
                )
                state.player_journal = state.player_journal[-24:]
                state.phase_index += 1
                state.phase_turn = 0
                state.injected_pulses = []
                state.confirmed_pulses = []
                state.criteria_met = []
                state.durable_changes = []
                director.current = None
                finished_arc = catalog.arcs[runtime.phase.arc_index]
                if (
                    finished_arc.terminal
                    and runtime.phase.slug == finished_arc.phases[-1].slug
                ):
                    state.completed = True
            state.save(state_path)

            if state.logical_turn % 5 == 0:
                print(
                    f"[{state.logical_turn - 1}/{turns_limit}] phase={runtime.phase.slug}; "
                    f"phase_turn={runtime.phase_turn}; status={evaluation.status}; "
                    f"{(time.time() - started) / max(1, state.logical_turn - 1):.2f}s/turn"
                )

        all_turns = await turns.get_history(campaign_id, limit=turns_limit * 4, active_only=False)
        active_theses = await session.execute(
            select(func.count()).select_from(SceneThesis).where(SceneThesis.status == "active")
        )
        completed_scene_active_theses = await session.execute(
            select(func.count())
            .select_from(SceneThesis)
            .join(Scene, Scene.id == SceneThesis.scene_id)
            .where(Scene.status == "completed", SceneThesis.status == "active")
        )
        proposal_counts = await session.execute(
            select(ProposedChange.status, func.count()).group_by(ProposedChange.status)
        )
        proposal_summary = dict(proposal_counts.all())
        counts = {
            "entities": await count_campaign_rows(session, Entity, campaign_id),
            "events": await count_campaign_rows(session, Event, campaign_id),
            "relationships": await count_campaign_rows(session, RelationshipAssertion, campaign_id),
            "thesis_versions": await count_campaign_rows(session, SceneThesis, campaign_id),
            "active_theses": int(active_theses.scalar_one()),
            "active_theses_in_completed_scenes": int(completed_scene_active_theses.scalar_one()),
            "beliefs": int((await session.execute(select(func.count()).select_from(Belief))).scalar_one()),
            "goals": int((await session.execute(select(func.count()).select_from(CharacterGoal))).scalar_one()),
            "facts": len(await FactRepository(session).list_active(campaign_id)),
            "accepted_proposals": int(proposal_summary.get("accepted", 0)),
            "rejected_proposals": int(proposal_summary.get("rejected", 0)),
            "invalid_proposals": int(proposal_summary.get("invalid", 0)),
        }
        logical_records = list(trace.records.values())
        generation_failures = sum(bool(item.get("generation_failed")) for item in logical_records)
        builder_total = sum(
            stats[key]
            for key in ("character_builder_model", "character_builder_repair", "character_builder_fallback")
        )
        lines = [
            "# Отчёт о реалистичной автономной кампании v2",
            "",
            f"- Run ID: `{state.run_id}`",
            f"- Alembic revision: `{alembic_revision}`",
            f"- Generated scenario: `{json.dumps(catalog_summary(catalog), ensure_ascii=False)}`",
            f"- Кампания: {campaign.name}",
            f"- Запланированный предел ходов: {turns_limit}",
            f"- Уникальных логических ходов: {len(logical_records)}",
            f"- Следующий логический ход: {state.logical_turn}",
            f"- Кампания завершена: {state.completed}",
            f"- Пройдено фаз: {state.phase_index}/{len(PHASES)}",
            f"- Строк turns в SQLite: {len(all_turns)}",
            f"- Ошибок генерации в уникальных ходах: {generation_failures}",
            f"- Character Builder model/repair/fallback: {stats['character_builder_model']}/{stats['character_builder_repair']}/{stats['character_builder_fallback']}",
            f"- Character Builder fallback rate: {(stats['character_builder_fallback'] / builder_total * 100 if builder_total else 0):.1f}%",
            f"- Player fallbacks: {policy.fallbacks}",
            f"- Отклонено player-outcomes: {policy.rejected_outcomes}",
            f"- Отклонено повторов: {policy.repeated_actions}",
            f"- Phrases unique: {len({policy.fingerprint(item.get('player', {}).get('intent', '')) for item in logical_records})}",
            *(f"- {name}: {value}" for name, value in counts.items()),
            f"- Время текущего запуска: {(time.time() - started) / 60:.2f} минут",
            "",
            "## Поведение игрока",
            *(f"- {mode}: {policy.mode_counts[mode]}" for mode in PlayerPolicy.MODES),
            "",
            f"- SQLite: `{database_path}`",
            f"- State: `{state_path}`",
            f"- Лог: `{log_path}`",
            f"- JSONL: `{trace_path}`",
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        await session.commit()
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    await engine.dispose()
    print("=== REALISTIC AUTONOMOUS CAMPAIGN V2 FINISHED ===")


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation_v2())
