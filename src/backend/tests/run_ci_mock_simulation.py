"""Run the realistic campaign end-to-end with a deterministic in-process LLM.

This exercises the backend pipeline, scenario director, live theses, scribe,
proposal validation/application and persistence without requiring Ollama.
It does not measure the prose quality of a real model.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter

from app.providers.llm_provider import LLMProvider
from tests.run_realistic_simulation import run_realistic_simulation

COUNTERS: Counter[str] = Counter()


def _message_text(messages) -> str:
    return "\n\n".join(str(message.content) for message in messages)


def _character_card(text: str) -> str:
    name_match = re.search(r"Name:\s*(.+)", text)
    role_match = re.search(r"Campaign role:\s*(.+)", text)
    tone_match = re.search(r"Tone:\s*(.+)", text)
    concept_match = re.search(r"Concept:\s*(.+)", text)
    name = (name_match.group(1).strip() if name_match else "Unknown NPC")
    role = (role_match.group(1).strip() if role_match else "expedition specialist")
    tone = (tone_match.group(1).strip() if tone_match else "observant and restrained")
    concept = (concept_match.group(1).strip() if concept_match else "keeps a private concern")
    return json.dumps(
        {
            "canonical_name": name,
            "description": f"A grounded {role} joining the expedition for a specific purpose.",
            "appearance": f"Practical travel clothes marked by the tools of a {role}.",
            "face_description": "Alert eyes and a travel-worn expression.",
            "body_description": "Fit enough for a difficult expedition.",
            "immutable_features": "A small distinctive scar on one hand.",
            "personality": tone,
            "values": ["survival", "competence", "earned trust"],
            "fears": ["losing control of the mission"],
            "desires": ["complete the current objective", "protect a private truth"],
            "voice": "Concise, concrete and recognisably individual.",
            "speech_patterns": "Answers direct questions and names practical risks.",
            "biography": f"Worked as a {role} before joining the expedition.",
            "backstory_public": f"Known publicly as a capable {role}.",
            "secrets": [concept],
            "emotional_state": "alert",
            "current_intentions": ["assess Eldon", "help without revealing too much"],
            "goals": ["advance the expedition", "survive the citadel"],
            "capabilities": [f"apply {role} expertise", "notice relevant danger"],
            "limitations": ["cannot use unlisted powers", "cannot know private facts without a source"],
            "equipment": [f"basic {role} tools", "ordinary travel gear"],
            "initial_beliefs": ["The official history of the citadel is incomplete."],
            "visual_profile": {"role": role, "tone": tone},
        },
        ensure_ascii=False,
    )


def _player_decision(text: str) -> str:
    COUNTERS["player"] += 1
    preferred = re.search(r"PREFERRED MODE:\s*(\w+)", text)
    suggested = re.search(r"UNDERUSED TARGET:\s*([^\n]+)", text)
    mode = preferred.group(1).strip() if preferred else "action"
    target = suggested.group(1).strip() if suggested else "narrator"
    if target == "narrator" and mode in {"question", "dialogue"}:
        mode = "action"
    intents = {
        "action": "I examine the immediate obstacle from a safe distance and look for an ordinary way forward.",
        "question": f'I ask {target}, "What detail here matters most before we act?"',
        "dialogue": f'I tell {target}, "Give me the risk you are not saying aloud."',
        "plan": "I compare the available approaches and propose the least destructive next step.",
        "decision": "I choose a cautious route and ask the group for one concrete objection before proceeding.",
    }
    intent = intents.get(mode, intents["action"])
    intent = intent[:-1] + f" Step {COUNTERS['player']}." if mode == "action" else intent
    return json.dumps({"target": target, "mode": mode, "intent": intent}, ensure_ascii=False)


def _curator_response(text: str) -> str:
    COUNTERS["curator"] += 1
    turn = COUNTERS["curator"]
    dm_result = text.split("DM RESULT:", 1)[-1].strip().splitlines()[0][:180]
    desired = [
        {
            "thesis_type": "tension",
            "text": f"The scene demands a concrete choice before momentum stalls; revision {turn}.",
            "priority": 8,
            "visibility": "dm",
            "related_entity_ids": [],
        },
        {
            "thesis_type": "unresolved_beat",
            "text": f"The party must turn the latest consequence into progress: {dm_result}",
            "priority": 7,
            "visibility": "dm",
            "related_entity_ids": [],
        },
        {
            "thesis_type": "visual_state",
            "text": f"The environment visibly records the last action and is no longer unchanged at turn {turn}.",
            "priority": 5,
            "visibility": "public",
            "related_entity_ids": [],
        },
        {
            "thesis_type": "relationship_dynamic",
            "text": f"The active companions judge Eldon by whether he listens before committing; turn {turn}.",
            "priority": 4,
            "visibility": "dm",
            "related_entity_ids": [],
        },
        {
            "thesis_type": "music_mood",
            "text": "Low restrained pulse with space for dialogue, rising only around irreversible choices.",
            "priority": 2,
            "visibility": "dm",
            "related_entity_ids": [],
        },
    ]
    return json.dumps({"desired_active": desired}, ensure_ascii=False)


def _scribe_response() -> str:
    COUNTERS["scribe"] += 1
    n = COUNTERS["scribe"]
    proposals = []
    if n % 4 == 0:
        proposals.append(
            {
                "change_type": "event",
                "payload": {
                    "event_type": "confirmed_progress",
                    "description": f"The expedition completed a meaningful incremental step at resolved turn {n}.",
                    "location_id": None,
                    "participant_ids": [],
                    "importance": "normal",
                },
            }
        )
    if n % 7 == 0:
        proposals.append(
            {
                "change_type": "fact",
                "payload": {
                    "subject": "expedition",
                    "predicate": "reached_progress_marker",
                    "object_value": str(n),
                    "visibility": "dm",
                },
            }
        )
    return json.dumps({"proposals": proposals}, ensure_ascii=False)


def _dm_response(messages, text: str) -> str:
    COUNTERS["dm"] += 1
    n = COUNTERS["dm"]
    latest_user = next(
        (str(message.content) for message in reversed(messages) if getattr(message, "role", None) == "user"),
        "Eldon watches the scene.",
    )
    if "You are roleplaying one specific character" in text:
        return (
            f'"I see the risk," the companion says, keeping their answer practical. '
            f'"Your approach is possible, but we should verify one detail first." [response {n}]'
        )
    return (
        f"Eldon acts cautiously: {latest_user[:180]} "
        f"The attempt produces a limited, observable result rather than an automatic success. "
        f"A useful detail becomes clear, the immediate danger changes, and the group gains one concrete next choice. "
        f"No new item or ability appears. [result {n}]"
    )


async def fake_generate_stream(self, messages, config, api_key=None):
    text = _message_text(messages)
    if "You create reviewable NPC cards" in text:
        COUNTERS["character_builder"] += 1
        result = _character_card(text)
        kind = "character_builder"
    elif "You simulate a real tabletop RPG player" in text:
        result = _player_decision(text)
        kind = "player"
    elif "You are the Scene Thesis Curator" in text:
        result = _curator_response(text)
        kind = "curator"
    elif "You are the Memory Scribe" in text:
        result = _scribe_response()
        kind = "scribe"
    else:
        result = _dm_response(messages, text)
        kind = "dm"

    self.last_telemetry = {
        "model": "deterministic-ci-provider",
        "url": "in-process://mock",
        "status": "completed",
        "http_status": 200,
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": max(1, len(text) // 4),
            "completion_tokens": max(1, len(result) // 4),
            "total_tokens": max(2, (len(text) + len(result)) // 4),
        },
        "parsed_frames": 1,
        "malformed_frames": 0,
        "response_characters": len(result),
        "duration_ms": 1,
        "mock_kind": kind,
    }
    yield result


async def main() -> None:
    os.environ.setdefault("PDM_SIM_TURNS", "100")
    os.environ.setdefault("PDM_SIM_DATA_DIR", "./data/mock-simulation")
    os.environ.setdefault("PDM_SIM_MODEL", "deterministic-ci-provider")
    os.environ.setdefault("PDM_SIM_BASE_URL", "http://unused.invalid/v1")
    os.environ.setdefault("PDM_SIM_RESET", "1")
    LLMProvider.generate_stream = fake_generate_stream
    await run_realistic_simulation()
    output = os.path.join(os.environ["PDM_SIM_DATA_DIR"], "mock_provider_calls.json")
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(dict(COUNTERS), handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
