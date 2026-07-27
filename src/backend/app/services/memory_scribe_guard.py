from __future__ import annotations

import re
import unicodedata

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.services.canon_semantics import proposals_from_envelope


_INSTALLED = False
_EVIDENCE_STOPWORDS = {
    "котор",
    "также",
    "после",
    "перед",
    "через",
    "начал",
    "стала",
    "стало",
    "будет",
    "может",
    "своег",
    "своей",
    "этого",
}
_PLAYER_META_FORMS = {
    "игрок": "Элдон",
    "игрока": "Элдона",
    "игроку": "Элдону",
    "игроком": "Элдоном",
    "игроке": "Элдоне",
}


def _has_script_corruption(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_script_corruption(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_script_corruption(item) for item in value)
    if not isinstance(value, str):
        return False
    for char in value:
        if not char.isalpha():
            continue
        script_name = unicodedata.name(char, "")
        if "CYRILLIC" not in script_name and "LATIN" not in script_name:
            return True
    for token in "".join(char if char.isalpha() else " " for char in value).split():
        scripts = {
            "cyrillic" if "CYRILLIC" in unicodedata.name(char, "") else "latin"
            for char in token
            if "CYRILLIC" in unicodedata.name(char, "")
            or "LATIN" in unicodedata.name(char, "")
        }
        if len(scripts) > 1:
            return True
    return False


def _evidence_supports_description(canon_meta: dict) -> bool:
    description = str(canon_meta.get("description") or "")
    evidence = str(canon_meta.get("evidence") or "")
    if not description or not evidence:
        return True

    def all_stems(value: str) -> set[str]:
        words = re.findall(r"[А-ЯЁа-яё]+", value)
        return {
            word.casefold()[:5]
            for word in words
            if (
                len(word) >= 5
                and word.casefold()[:5] not in _EVIDENCE_STOPWORDS
            )
        }

    def stems(value: str) -> set[str]:
        capitalized = {
            word.casefold()[:5]
            for word in re.findall(r"[А-ЯЁ][а-яё]+", value)
            if len(word) >= 5
        }
        return all_stems(value) - capitalized

    description_stems = stems(description)
    evidence_stems = stems(evidence)
    relation_targets = re.findall(
        r"\b(?:в|во|о|об|про|против|из-за)\s+"
        r"([А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})?)",
        description,
    )
    for target in relation_targets:
        target_stems = all_stems(target)
        if target_stems and not target_stems <= all_stems(evidence):
            return False
    return not description_stems or bool(description_stems & evidence_stems)


def _replace_singular_player_meta(value: str) -> str:
    return re.sub(
        r"\b(?:игрок|игрока|игроку|игроком|игроке)\b",
        lambda match: _PLAYER_META_FORMS[match.group(0).casefold()],
        value,
        flags=re.IGNORECASE,
    )


def _replace_player_meta_in_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _replace_player_meta_in_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_player_meta_in_value(item) for item in value]
    if isinstance(value, str):
        return _replace_singular_player_meta(value)
    return value


def _downgrade_sourced_knowledge_claim(
    proposal: ProposedChangeCreate,
    canon_meta: dict,
) -> None:
    if (
        proposal.change_type != ChangeType.KNOWLEDGE
        or canon_meta.get("kind") != "knowledge_transfer"
        or canon_meta.get("authority")
        not in {"public_observation", "character_claim"}
        or not proposal.payload.get("source_character_id")
    ):
        return
    canon_meta["authority"] = "character_claim"
    proposition = str(proposal.payload.get("proposition") or "").strip()
    evidence = str(canon_meta.get("evidence") or "").strip()
    # For a sourced statement, the witnessed words are safer canon than the
    # Scribe's paraphrase: paraphrasing can silently reverse intent ("purge"
    # becoming "study") while still sharing all of the same entity names.
    if evidence:
        proposition = evidence.strip("\"'«» ").strip()
    if proposition and not proposition.startswith("Заявление источника:"):
        proposal.payload["proposition"] = f"Заявление источника: {proposition}"
    proposal.payload["status"] = "believed"


def _source_character_from_evidence(
    canon_meta: dict,
    known_entities: dict,
    player_character_id,
    authoritative_text: str = "",
):
    player_id = str(player_character_id or "")
    evidence = str(canon_meta.get("evidence") or "").strip(
        "\"'«» "
    ).strip()
    if evidence and authoritative_text:
        anchor = evidence[:80].casefold()
        position = authoritative_text.casefold().find(anchor)
        if position >= 0:
            prefix = authoritative_text[:position].casefold()
            preceding = sorted(
                (
                    (prefix.rfind(str(name).casefold()), str(name), value)
                    for name, value in known_entities.items()
                    if str(value) != player_id
                ),
                reverse=True,
            )
            if preceding and preceding[0][0] >= 0:
                return preceding[0][2]

    haystack = " ".join(
        str(canon_meta.get(key) or "")
        for key in ("description", "evidence")
    ).casefold()
    candidates = sorted(
        (
            (str(name), value)
            for name, value in known_entities.items()
            if (
                str(name).strip()
                and str(name).casefold() in haystack
                and str(value) != player_id
            )
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    return candidates[0][1] if candidates else None


def install() -> None:
    """Turn post-extraction normalization failures into explicit canon gaps."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services.memory_scribe import MemoryScribe

    def guarded_parse_data(
        self,
        data,
        authoritative_text,
        known_entities,
        known_ids,
        acting_character_id,
        player_character_id,
        scene_participant_ids,
    ):
        extracted, audit = proposals_from_envelope(data, authoritative_text)
        results: list[ProposedChangeCreate] = []
        surviving_outcomes: set[str] = set()
        failed_normalization: dict[str, dict] = {}
        existing_gaps = set(audit.gap_outcome_ids)
        repaired_gaps: set[str] = set()

        for proposal in extracted:
            proposal.payload = _replace_player_meta_in_value(proposal.payload)
            canon_meta = (
                proposal.payload.get("_canon")
                if isinstance(proposal.payload.get("_canon"), dict)
                else {}
            )
            if canon_meta and str(canon_meta.get("description") or "").strip():
                canon_meta["description"] = _replace_singular_player_meta(
                    str(canon_meta["description"])
                )
            _downgrade_sourced_knowledge_claim(proposal, canon_meta)
            outcome_id = str(canon_meta.get("outcome_id") or "").strip()
            if not _evidence_supports_description(canon_meta):
                if outcome_id:
                    failed_normalization[outcome_id] = {
                        **canon_meta,
                        "_evidence_mismatch": True,
                    }
                continue
            if _has_script_corruption(proposal.payload):
                if outcome_id:
                    failed_normalization[outcome_id] = canon_meta
                continue
            if proposal.change_type == ChangeType.CANON_GAP:
                if (
                    outcome_id
                    and canon_meta.get("kind") == "knowledge_transfer"
                    and canon_meta.get("authority") == "character_claim"
                    and player_character_id
                    and str(canon_meta.get("evidence") or "").strip()
                ):
                    source_character_id = _source_character_from_evidence(
                        canon_meta,
                        known_entities,
                        player_character_id,
                        authoritative_text,
                    )
                    if source_character_id:
                        evidence = str(canon_meta["evidence"]).strip(
                            "\"'«» "
                        ).strip()
                        canon_meta["preserved_as"] = (
                            "evidence_backed_character_claim"
                        )
                        results.append(
                            ProposedChangeCreate(
                                change_type=ChangeType.KNOWLEDGE,
                                payload={
                                    "recipient_id": str(player_character_id),
                                    "source_character_id": str(
                                        source_character_id
                                    ),
                                    "proposition": (
                                        f"Заявление источника: {evidence}"
                                    ),
                                    "confidence": 0.8,
                                    "status": "believed",
                                    "_canon": canon_meta,
                                },
                            )
                        )
                        surviving_outcomes.add(outcome_id)
                        repaired_gaps.add(outcome_id)
                        existing_gaps.discard(outcome_id)
                        continue
                if (
                    outcome_id
                    and canon_meta.get("authority")
                    in {"dm_confirmed", "public_observation", "character_claim"}
                    and str(canon_meta.get("description") or "").strip()
                ):
                    canon_meta = {
                        **canon_meta,
                        "preserved_as": "narrative_event",
                    }
                    results.append(
                        ProposedChangeCreate(
                            change_type=ChangeType.EVENT,
                            payload={
                                "event_type": "narrative_event",
                                "description": str(canon_meta["description"]).strip(),
                                "participant_ids": [],
                                "_canon": canon_meta,
                            },
                        )
                    )
                    surviving_outcomes.add(outcome_id)
                    repaired_gaps.add(outcome_id)
                    existing_gaps.discard(outcome_id)
                    continue
                results.append(proposal)
                if outcome_id:
                    existing_gaps.add(outcome_id)
                continue
            if proposal.change_type == ChangeType.SCENE_THESIS:
                continue

            normalized = self._normalize_payload(
                proposal.change_type,
                proposal.payload,
                known_entities,
                known_ids,
                acting_character_id,
                player_character_id,
                scene_participant_ids,
            )
            if normalized:
                results.append(
                    ProposedChangeCreate(
                        change_type=proposal.change_type,
                        payload=normalized,
                    )
                )
                if outcome_id:
                    surviving_outcomes.add(outcome_id)
            elif outcome_id:
                failed_normalization[outcome_id] = canon_meta

        for outcome in data.get("outcomes", []) if isinstance(data, dict) else []:
            if not isinstance(outcome, dict):
                continue
            outcome_id = str(outcome.get("id") or "").strip()
            if (
                not outcome_id
                or outcome_id not in set(audit.gap_outcome_ids)
                or outcome_id in repaired_gaps
                or outcome.get("authority")
                not in {"dm_confirmed", "public_observation", "character_claim"}
                or not str(outcome.get("description") or "").strip()
                or _has_script_corruption(outcome)
            ):
                continue
            canon_meta = {
                "outcome_id": outcome_id,
                "kind": str(outcome.get("kind") or "event"),
                "description": str(outcome["description"]).strip(),
                "evidence": str(outcome.get("evidence") or "").strip(),
                "authority": str(outcome["authority"]),
                "operation": "assert",
                "preserved_as": "narrative_event",
            }
            if (
                canon_meta["kind"] == "knowledge_transfer"
                and canon_meta["authority"] == "character_claim"
                and player_character_id
                and canon_meta["evidence"]
            ):
                source_character_id = _source_character_from_evidence(
                    canon_meta,
                    known_entities,
                    player_character_id,
                    authoritative_text,
                )
                if source_character_id:
                    evidence = canon_meta["evidence"].strip(
                        "\"'«» "
                    ).strip()
                    canon_meta["preserved_as"] = (
                        "evidence_backed_character_claim"
                    )
                    results.append(
                        ProposedChangeCreate(
                            change_type=ChangeType.KNOWLEDGE,
                            payload={
                                "recipient_id": str(player_character_id),
                                "source_character_id": str(source_character_id),
                                "proposition": (
                                    f"Заявление источника: {evidence}"
                                ),
                                "confidence": 0.8,
                                "status": "believed",
                                "_canon": canon_meta,
                            },
                        )
                    )
                    surviving_outcomes.add(outcome_id)
                    repaired_gaps.add(outcome_id)
                    existing_gaps.discard(outcome_id)
                    continue
            if not _evidence_supports_description(canon_meta):
                evidence = str(canon_meta.get("evidence") or "").strip()
                if not evidence:
                    continue
                canon_meta = {
                    **canon_meta,
                    "_evidence_mismatch": True,
                    "preserved_as": "evidence_backed_narrative_event",
                }
                event_description = (
                    "Подтверждённое наблюдение сцены: "
                    f"{evidence}"
                )
            else:
                event_description = canon_meta["description"]
            results.append(
                ProposedChangeCreate(
                    change_type=ChangeType.EVENT,
                    payload={
                        "event_type": "narrative_event",
                        "description": event_description,
                        "participant_ids": [],
                        "_canon": canon_meta,
                    },
                )
            )
            surviving_outcomes.add(outcome_id)
            repaired_gaps.add(outcome_id)
            existing_gaps.discard(outcome_id)

        if repaired_gaps:
            remaining_gaps = sorted(
                set(audit.gap_outcome_ids) - repaired_gaps
            )
            audit.gap_outcome_ids = remaining_gaps
            audit.gap_count = len(remaining_gaps)
            audit.covered_outcome_count += len(repaired_gaps)
            denominator = audit.covered_outcome_count + audit.gap_count
            audit.coverage_ratio = (
                audit.covered_outcome_count / denominator if denominator else 1.0
            )
            audit.envelope_valid = (
                audit.rejected_schema_count == 0
                and audit.rejected_evidence_count == 0
                and audit.rejected_authority_count == 0
                and audit.gap_count == 0
            )

        # A model can correctly identify a durable, evidenced occurrence but
        # misclassify an action ("Элдон отметил две точки") as a knowledge
        # transfer.  If the typed payload then fails entity normalization,
        # preserve only the evidenced occurrence as an event.  This is safer
        # than either dropping it or fabricating recipient/source knowledge.
        for outcome_id, canon_meta in failed_normalization.items():
            if outcome_id in surviving_outcomes:
                continue
            if (
                canon_meta.get("kind") == "knowledge_transfer"
                and canon_meta.get("authority") == "character_claim"
                and player_character_id
                and str(canon_meta.get("evidence") or "").strip()
                and not _has_script_corruption(canon_meta.get("evidence"))
            ):
                source_character_id = _source_character_from_evidence(
                    canon_meta,
                    known_entities,
                    player_character_id,
                    authoritative_text,
                )
                if source_character_id:
                    evidence = str(canon_meta["evidence"]).strip(
                        "\"'«» "
                    ).strip()
                    preserved_meta = {
                        **canon_meta,
                        "preserved_as": "evidence_backed_character_claim",
                    }
                    results.append(
                        ProposedChangeCreate(
                            change_type=ChangeType.KNOWLEDGE,
                            payload={
                                "recipient_id": str(player_character_id),
                                "source_character_id": str(source_character_id),
                                "proposition": (
                                    f"Заявление источника: {evidence}"
                                ),
                                "confidence": 0.8,
                                "status": "believed",
                                "_canon": preserved_meta,
                            },
                        )
                    )
                    surviving_outcomes.add(outcome_id)
                    continue
            if (
                canon_meta.get("_evidence_mismatch")
                and canon_meta.get("authority")
                in {"dm_confirmed", "public_observation", "character_claim"}
                and str(canon_meta.get("evidence") or "").strip()
                and not _has_script_corruption(canon_meta.get("evidence"))
            ):
                evidence = str(canon_meta["evidence"]).strip()
                preserved_meta = {
                    **canon_meta,
                    "preserved_as": "evidence_backed_narrative_event",
                }
                results.append(
                    ProposedChangeCreate(
                        change_type=ChangeType.EVENT,
                        payload={
                            "event_type": "narrative_event",
                            "description": (
                                "Подтверждённое наблюдение сцены: "
                                f"{evidence}"
                            ),
                            "participant_ids": [],
                            "_canon": preserved_meta,
                        },
                    )
                )
                surviving_outcomes.add(outcome_id)
                continue
            if (
                canon_meta.get("kind")
                in {
                    "knowledge_transfer",
                    "relationship_change",
                    "movement",
                    "world_state",
                    "fact",
                    "event",
                }
                and not canon_meta.get("_evidence_mismatch")
                and canon_meta.get("authority")
                in {"dm_confirmed", "public_observation", "character_claim"}
                and str(canon_meta.get("description") or "").strip()
                and not _has_script_corruption(canon_meta)
            ):
                preserved_meta = {
                    **canon_meta,
                    "preserved_as": "narrative_event",
                }
                results.append(
                    ProposedChangeCreate(
                        change_type=ChangeType.EVENT,
                        payload={
                            "event_type": "narrative_event",
                            "description": str(
                                preserved_meta["description"]
                            ).strip(),
                            "participant_ids": [],
                            "_canon": preserved_meta,
                        },
                    )
                )
                surviving_outcomes.add(outcome_id)

        new_gaps = sorted(
            set(failed_normalization) - surviving_outcomes - existing_gaps
        )
        for outcome_id in new_gaps:
            results.append(
                ProposedChangeCreate(
                    change_type=ChangeType.CANON_GAP,
                    payload={
                        "_validation_error": (
                            "Evidence-backed outcome failed backend entity or payload normalization"
                        ),
                        "_canon": failed_normalization[outcome_id],
                    },
                )
            )

        if new_gaps:
            all_gaps = sorted(existing_gaps | set(new_gaps))
            audit.gap_outcome_ids = all_gaps
            audit.gap_count = len(all_gaps)
            audit.covered_outcome_count = max(
                0,
                audit.covered_outcome_count - len(new_gaps),
            )
            denominator = audit.covered_outcome_count + audit.gap_count
            audit.coverage_ratio = (
                audit.covered_outcome_count / denominator if denominator else 1.0
            )
            audit.rejected_schema_count += len(new_gaps)
            audit.envelope_valid = False
            failed_details = "; ".join(
                (
                    f"{outcome_id}:"
                    f"{failed_normalization[outcome_id].get('kind', 'unknown')} "
                    f"{failed_normalization[outcome_id].get('description', '')}"
                )[:500]
                for outcome_id in new_gaps
            )
            audit.error = (
                "One or more supported outcomes failed backend normalization: "
                f"{failed_details}"
            )

        audit.proposal_count = len(results)
        self.last_audit = audit.model_dump()
        return results

    MemoryScribe._parse_data = guarded_parse_data
