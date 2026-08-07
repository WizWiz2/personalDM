from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from pathlib import Path

from app.providers.llm_provider import LLMProvider
from app.services.memory_scribe import MemoryScribe
from app.services.memory_scribe_guard import install as install_memory_scribe_guard
from app.services.thesis_curator import ThesisCurator

try:
    from . import run_realistic_simulation_v2 as runtime
    from . import simulation_dynamic_campaign as dynamic_campaign
except ImportError:
    import run_realistic_simulation_v2 as runtime
    import simulation_dynamic_campaign as dynamic_campaign


_INSTALLED = False
_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
_TARGET_SUFFIX = re.compile(r"\s*\|\s*(?:activenpc|narrator)\s*$", re.IGNORECASE)


def _simulation_mode() -> str:
    value = os.getenv("PDM_SIM_MODE", "quality").strip().casefold()
    return value if value in {"quality", "endurance", "smoke"} else "quality"


def _latin_key(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    transliterated = "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in folded)
    return "".join(char for char in transliterated if char.isalnum())


def _normalize_player_target(target: str, active_npcs: list[str]) -> str:
    """Resolve only safe, unambiguous target variants emitted by small local LLMs."""
    raw = " ".join(str(target or "").split())
    stripped = _TARGET_SUFFIX.sub("", raw).strip()
    folded = stripped.casefold()
    if folded in {"narrator", "рассказчик", "dm", "master"}:
        return "narrator"

    canonical = {name.casefold(): name for name in active_npcs}
    if folded in canonical:
        return canonical[folded]

    if folded in {"activenpc", "active npc", "npc"}:
        return active_npcs[0] if len(active_npcs) == 1 else raw

    key = _latin_key(stripped)
    if not key:
        return raw
    matches = [name for name in active_npcs if _latin_key(name) == key]
    return matches[0] if len(matches) == 1 else raw


def _repair_arc_introductions(catalog, arc) -> tuple[object, list[tuple[str, str]]]:
    """Repair only the safe case: a known new NPC is active before introduction."""
    repaired = copy.deepcopy(arc)
    existing = set(catalog.canonical_npc_names())
    new_names = {npc.name.casefold(): npc.name for npc in repaired.npcs}
    introduced = set(existing)
    repairs: list[tuple[str, str]] = []

    for phase in repaired.phases:
        for raw_name in phase.introduced_npcs:
            folded = raw_name.casefold()
            if folded in new_names:
                introduced.add(folded)
        for raw_name in phase.active_npcs:
            folded = raw_name.casefold()
            if folded in new_names and folded not in introduced:
                canonical = new_names[folded]
                phase.introduced_npcs.append(canonical)
                phase.introduced_npcs = list(dict.fromkeys(phase.introduced_npcs))
                introduced.add(folded)
                repairs.append((phase.slug, canonical))
    return repaired, repairs


def install(quality_module) -> None:
    """Align quality controls with production provider and resilient endurance runs."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    install_memory_scribe_guard()
    original_uses_mock_stream = quality_module._uses_mock_stream
    original_mock_stream_json = quality_module._mock_stream_json
    original_scribe = MemoryScribe.extract_proposals
    original_curator = ThesisCurator.curate_after_turn
    original_parse_player_decision = runtime.parse_player_decision
    original_generate_arc = dynamic_campaign.generate_arc
    original_normalize_arc = dynamic_campaign.normalize_arc_references

    control_events: list[dict[str, object]] = []
    quality_module.CONTROL_EVENTS = control_events

    def record_event(stage: str, status: str, detail: str = "", **extra) -> None:
        event = {
            "stage": stage,
            "status": status,
            "detail": " ".join(str(detail).split())[:1200],
            **extra,
        }
        control_events.append(event)
        del control_events[:-200]

    quality_module.record_control_event = record_event
    quality_module.benchmark_mode = _simulation_mode

    def write_health() -> None:
        path = Path(os.getenv("PDM_SIM_DATA_DIR", "./data")) / "realistic_simulation_health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": quality_module.benchmark_mode(),
            "control_stats": dict(quality_module.CONTROL_STATS),
            "control_failures": list(quality_module.CONTROL_FAILURES),
            "control_events": list(control_events),
            "valid": not quality_module.CONTROL_FAILURES,
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def health_snapshot():
        return {
            "mode": quality_module.benchmark_mode(),
            "control_stats": dict(quality_module.CONTROL_STATS),
            "control_failures": list(quality_module.CONTROL_FAILURES),
            "control_events": list(control_events),
            "valid": not quality_module.CONTROL_FAILURES,
        }

    quality_module._write_health = write_health
    quality_module.health_snapshot = health_snapshot

    def underlying_class_method(provider):
        return getattr(type(provider), "generate_stream", LLMProvider.generate_stream)

    def uses_mock_stream(provider) -> bool:
        bound = provider.generate_stream
        method = getattr(bound, "__func__", bound)
        if getattr(method, "__name__", "") == "json_stream":
            underlying = underlying_class_method(provider)
            module = getattr(underlying, "__module__", "")
            return module != "app.providers.llm_provider"
        return original_uses_mock_stream(provider)

    async def mock_stream_json(
        provider,
        messages,
        config,
        api_key,
        max_tokens,
        temperature,
    ):
        bound = provider.generate_stream
        method = getattr(bound, "__func__", bound)
        if getattr(method, "__name__", "") != "json_stream":
            return await original_mock_stream_json(
                provider,
                messages,
                config,
                api_key,
                max_tokens,
                temperature,
            )
        underlying = underlying_class_method(provider)
        raw = ""
        async for token in underlying(
            provider,
            messages,
            config,
            api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            raw += token
        return quality_module._balanced_json_object(raw), "mock_stream"

    def control_budget(label: str, requested: int) -> int:
        floors = {
            "builder": 1800,
            "curator": 1600,
            "scribe": 1400,
            "player": 640,
            "evaluator": 640,
        }
        return max(int(requested), floors.get(label, int(requested)))

    async def generate_control_json(
        provider,
        messages,
        config,
        api_key,
        *,
        label,
        max_tokens,
        temperature,
        response_model=None,
    ):
        quality_module.CONTROL_STATS[f"{label}_calls"] += 1
        budget = control_budget(label, max_tokens)
        quality_module.CONTROL_STATS[f"{label}_requested_tokens"] += budget
        try:
            if uses_mock_stream(provider):
                data, transport = await mock_stream_json(
                    provider,
                    messages,
                    config,
                    api_key,
                    budget,
                    temperature,
                )
                if response_model is not None:
                    data = response_model.model_validate(data).model_dump(mode="json")
                attempt = 1
            else:
                data = await provider.generate_json(
                    messages,
                    config,
                    api_key,
                    max_tokens=budget,
                    temperature=temperature,
                    response_model=response_model,
                )
                telemetry = dict(provider.last_telemetry or {})
                transport = str(telemetry.get("transport") or "provider_json")
                attempt = int(telemetry.get("attempt") or 1)
                quality_module.CONTROL_STATS[
                    f"{label}_reasoning_characters"
                ] += int(telemetry.get("reasoning_characters") or 0)
                quality_module.CONTROL_STATS[
                    f"{label}_response_characters"
                ] += int(telemetry.get("response_characters") or 0)
            quality_module.CONTROL_STATS[f"{label}_success"] += 1
            if attempt > 1:
                quality_module.CONTROL_STATS[f"{label}_repair_success"] += 1
            record_event(label, "success", transport=transport, attempt=attempt)
            quality_module._write_health()
            return quality_module.ControlJSONResult(
                data=data,
                attempt=attempt,
                transport=transport,
            )
        except Exception as exc:
            record_event(label, "failed", str(exc))
            quality_module.record_control_failure(label, exc)
            if quality_module.quality_mode():
                raise quality_module.BenchmarkControlError(
                    f"{label} unavailable: {exc}"
                ) from exc
            return None

    def parse_player_decision(raw: str, active_npcs: list[str]):
        decision = original_parse_player_decision(raw, active_npcs)
        target = _normalize_player_target(decision.target, active_npcs)
        if target != decision.target:
            quality_module.CONTROL_STATS["player_target_repairs"] += 1
            record_event(
                "player_target",
                "repaired",
                f"{decision.target!r} -> {target!r}",
            )
            quality_module._write_health()
            return runtime.PlayerDecision(
                target=target,
                mode=decision.mode,
                intent=decision.intent,
            )
        return decision

    def repairing_normalize_arc(catalog, arc):
        repaired, repairs = _repair_arc_introductions(catalog, arc)
        if repairs:
            quality_module.CONTROL_STATS["scenario_auto_repairs"] += len(repairs)
            for slug, name in repairs:
                record_event(
                    "scenario_generation",
                    "auto_repair",
                    f"phase {slug}: introduced {name}",
                )
            quality_module._write_health()
        return original_normalize_arc(catalog, repaired)

    async def resilient_generate_arc(*args, **kwargs):
        last_error: ValueError | None = None
        for attempt in range(1, 3):
            try:
                arc = await original_generate_arc(*args, **kwargs)
                record_event("scenario_generation", "success", attempt=attempt)
                quality_module._write_health()
                return arc
            except ValueError as exc:
                last_error = exc
                quality_module.CONTROL_STATS["scenario_validation_failures"] += 1
                record_event(
                    "scenario_generation",
                    "validation_retry",
                    str(exc),
                    attempt=attempt,
                )
                quality_module._write_health()

        quality_module.CONTROL_STATS["scenario_repair_attempts"] += 1
        dynamic_campaign.normalize_arc_references = repairing_normalize_arc
        try:
            arc = await original_generate_arc(*args, **kwargs)
            record_event("scenario_generation", "success_after_repair", attempt=3)
            quality_module._write_health()
            return arc
        except ValueError as exc:
            last_error = exc
            quality_module.CONTROL_STATS["scenario_validation_failures"] += 1
            record_event("scenario_generation", "failed", str(exc), attempt=3)
            quality_module.record_control_failure("scenario_generation", exc)
            raise
        finally:
            dynamic_campaign.normalize_arc_references = original_normalize_arc

    async def audited_scribe(self, *args, **kwargs):
        quality_module.CONTROL_STATS["scribe_calls"] += 1
        proposals = await original_scribe(self, *args, **kwargs)
        audit = dict(getattr(self, "last_audit", {}) or {})
        failure = None
        if audit.get("legacy_envelope"):
            failure = "legacy Scribe envelope has no outcome evidence"
        elif not audit.get("envelope_valid", True):
            failure = audit.get("error") or "outcome envelope failed semantic validation"
        elif int(audit.get("gap_count") or 0) > 0:
            failure = f"{audit.get('gap_count')} durable outcomes have no canon delta"
        if failure:
            record_event("scribe", "semantic_failure", failure)
            quality_module.record_control_failure("scribe_semantics", failure)
            if quality_module.quality_mode():
                raise quality_module.BenchmarkControlError(
                    f"Scribe semantics invalid: {failure}"
                )
        else:
            quality_module.CONTROL_STATS["scribe_success"] += 1
            quality_module.CONTROL_STATS["scribe_outcomes"] += int(
                audit.get("durable_outcome_count") or 0
            )
            quality_module.CONTROL_STATS["scribe_covered_outcomes"] += int(
                audit.get("covered_outcome_count") or 0
            )
            record_event("scribe", "success")
            quality_module._write_health()
        return proposals

    async def audited_curator(self, *args, **kwargs):
        quality_module.CONTROL_STATS["curator_calls"] += 1
        try:
            result = await original_curator(self, *args, **kwargs)
        except Exception as exc:
            record_event("curator", "failed", str(exc))
            quality_module.record_control_failure("curator", exc)
            if quality_module.quality_mode():
                raise quality_module.BenchmarkControlError(
                    f"Curator unavailable: {exc}"
                ) from exc
            return None
        quality_module.CONTROL_STATS["curator_success"] += 1
        record_event("curator", "success")
        quality_module._write_health()
        return result

    quality_module._uses_mock_stream = uses_mock_stream
    quality_module._mock_stream_json = mock_stream_json
    quality_module.generate_control_json = generate_control_json
    runtime.parse_player_decision = parse_player_decision
    dynamic_campaign.generate_arc = resilient_generate_arc
    MemoryScribe.extract_proposals = audited_scribe
    ThesisCurator.curate_after_turn = audited_curator
