from __future__ import annotations

import re

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationError(RuntimeError):
    """Raised when typed authority has no honest player-facing projection."""

    def __init__(self, message: str, authority: TurnAuthority | None = None):
        super().__init__(message)
        self.telemetry = ({"phase": "publication_projection", "authority": authority.model_dump(mode="json")}
                          if authority is not None else {})


class NarrationPublicationGuard:
    """Publish validated prose or a deterministic projection of typed authority.

    This boundary deliberately performs no semantic classification. Planner/Validator own meaning;
    publication only sanitizes unambiguously technical surface, applies exact validator evidence for
    upstream surgical-repair candidates, and renders already-typed authority when prose cannot be
    trusted.
    """

    UUID_PATTERN = re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
    )
    TECHNICAL_PATTERN = re.compile(
        r"(?:\bturn[_ ]authority\b|\bsource_scene_id\b|\btarget_scene_id\b|"
        r"\bsource_location\b|\btarget_location\b|\broute_discovery\b|"
        r"\bvalidator(?:_status)?\b|\bnarration_validation\b|"
        r"player destination is (?:unresolved|not authorized)|"
        r"existing route is required|destination route is currently inactive|"
        r"destination is not an available exit|"
        r"resolved to the current physical location|"
        r"use stay(?:/focus_transition)?|claiming physical travel|"
        r"\blocation_transition\b|\bfocus_transition\b|\bscene_disposition\b|"
        r"\bBLOCKED\b|\bSKIPPED\b|\bCOMPLETED\b|"
        r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}\b)",
        flags=re.IGNORECASE,
    )
    META_PATTERN = re.compile(
        r"(?:candidate\s+narration|engine\s+state|turn\s+authority|"
        r"validator\s+(?:status|result)|narration\s+validation)",
        flags=re.IGNORECASE,
    )
    LEGACY_STUB_PATTERN = re.compile(
        r"(?:действие\s+не\s+выполнено\s*:|"
        r"попытка\s+пока\s+не\s+приводит\s+к\s+подтвержд[её]нному\s+результату|"
        r"продвинуться\s+дальше\s+пока\s+не\s+уда[её]тся)",
        flags=re.IGNORECASE,
    )
    # An execution row has a leading task/owner before its separator. Ordinary
    # dialogue can start with a dash and contain a colon; it is not a ledger.
    LEDGER_PATTERN = re.compile(r"(?:^|\n)[^.!?…:—\n]{1,160}\s—\s[^—\n«»\"]{1,80}:")
    OBLIGATION_PATTERN = re.compile(
        r"получает прямое обращение и даёт ответ",
        flags=re.IGNORECASE,
    )
    DEAD_TURN_PATTERN = re.compile(
        r"^(?:пока\s+)?ничего(?:\s+заметно)?\s+не\s+(?:меняется|происходит)[.!?…]*$",
        flags=re.IGNORECASE,
    )
    STATE_BREAKING_VIOLATIONS = frozenset(
        {
            "player_agency",
            "invalid_movement",
            "invalid_time_advance",
            "sequence_violation",
            "canon_conflict",
        }
    )
    OPENING_TEXTURE_VIOLATIONS = frozenset(
        {
            "ungrounded_complication",
            "absent_object",
            "other",
        }
    )

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        # A conservative Planner fallback is deliberately non-authoritative: it means that
        # control-plane planning failed and no world outcome was established. A passing
        # prose-validator verdict must not turn that empty authority into an invented action,
        # movement, NPC, or clue. With no typed result there is nothing honest to publish.
        conservative_fallback = (
            authority.resolution == "uncertain"
            and authority.scene_disposition == "stay"
            and not authority.observable_consequences
            and not authority.action_sequence
            and authority.acting_character_id is None
        )
        if conservative_fallback:
            raise NarrationPublicationError(
                "Conservative authority has no observable outcome; refusing an empty narrative turn"
            )

        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]
        response = authority.addressed_response
        unproven_response = bool(
            response
            and response.questions
            and (
                validation is None
                or not validation.covers_questions(len(response.questions), candidate)
            )
        )
        if validation is None or errors or unproven_response:
            fallback = cls._emit(authority, cls._safe_authority_projection(authority))
            return fallback, {
                "mode": "authority_projection",
                "candidate_characters": len(candidate),
                "published_characters": len(fallback),
                "error_count": len(errors),
                "candidate_discarded": True,
                "validated_surface": False,
                "scene_development_projected": cls._development_was_projected(authority, fallback),
                "unproven_question_coverage": unproven_response,
            }

        # Validator checks established facts for every domain. A passing look
        # must keep its prose just like any other passing act; the mere presence
        # of remembered facts is not a reason to dump them into the game response.
        # Normalize only for deterministic inspection. If the candidate passes these
        # hard publication invariants, preserve the narrator's original paragraphing
        # and punctuation rather than flattening good prose into one line.
        candidate_surface = candidate.strip()
        inspected = cls._clean(candidate_surface)
        if inspected and cls._player_facing_fragment(inspected) is None:
            inspected = ""
        if inspected:
            return cls._emit(authority, candidate_surface), {
                "mode": "validated_candidate",
                "candidate_characters": len(candidate),
                "published_characters": len(candidate_surface),
                "error_count": 0,
                "candidate_discarded": False,
                "validated_surface": True,
            }

        fallback = cls._emit(authority, cls._safe_authority_projection(authority))
        return fallback, {
            "mode": "authority_projection",
            "candidate_characters": len(candidate),
            "published_characters": len(fallback),
            "error_count": 0,
            "candidate_discarded": True,
            "validated_surface": False,
            "scene_development_projected": cls._development_was_projected(authority, fallback),
        }

    @staticmethod
    def _sentences(text: str) -> list[str]:
        chunks: list[str] = []
        buf: list[str] = []
        for ch in text:
            buf.append(ch)
            if ch in ".!?":
                sentence = "".join(buf).strip()
                if sentence:
                    chunks.append(sentence)
                buf = []
        tail = "".join(buf).strip()
        if tail:
            chunks.append(tail)
        return chunks

    @classmethod
    def _observation_remainder(cls, candidate: str, subjects: list[str]) -> str:
        """Keep look sentences that are not about a slot the machine already owns.

        The subject is the stored fact key. This is not a lexicon of open or closed.
        """
        keys = [subject.casefold() for subject in subjects if subject and subject.strip()]
        if not keys:
            return ""
        kept: list[str] = []
        for sentence in cls._sentences(candidate):
            folded = sentence.casefold()
            if any(key in folded for key in keys):
                continue
            kept.append(sentence)
        return " ".join(kept).strip()

    @staticmethod
    def _emit(authority: TurnAuthority, text: str) -> str:
        from app.services.play_surface_contract import apply_play_surface

        return apply_play_surface(authority, text)

    @classmethod
    def _safe_authority_projection(cls, authority: TurnAuthority) -> str:
        rendered = cls.render_authority(authority)
        safe = cls._player_facing_fragment(rendered)
        if safe:
            return "\n\n".join([cls._as_sentence(safe), *cls._development_projection(authority)])
        raise NarrationPublicationError(
            "TurnAuthority has no player-facing typed outcome; refusing generic no-change fiction"
        )

    @classmethod
    def _development_projection(cls, authority: TurnAuthority) -> list[str]:
        development = authority.scene_development
        if not development:
            return []
        fragments = []
        if development.world_development:
            fragments.extend([development.world_development.development,
                              development.world_development.player_opportunity])
        for action in development.actions:
            fragments.append(action.action)
            if action.player_opportunity:
                fragments.append(action.player_opportunity)
        safe = [cls._player_facing_fragment(fragment) for fragment in fragments]
        # Keep all approved acts together or omit their receipts together.
        return [cls._as_sentence(fragment) for fragment in safe] if safe and all(safe) else []

    @classmethod
    def _development_was_projected(cls, authority: TurnAuthority, published: str) -> bool:
        fragments = cls._development_projection(authority)
        surface = cls._clean(published)
        return bool(fragments) and all(cls._clean(fragment) in surface for fragment in fragments)

    @classmethod
    def surgical_repair_candidate(
        cls,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str | None, dict]:
        """Remove only exact spans selected by semantic Validator; never infer meaning locally."""
        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]
        if not errors:
            return None, {"strategy": "not_applicable", "reason": "no_error_violations"}

        # Missing obligated response beat cannot be fixed by deleting prose — fail closed.
        if any(str(item.evidence or "").startswith("addressed:") for item in errors):
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "addressed_response_obligation_not_surgically_repairable",
                "error_count": len(errors),
            }

        cleaned, matched = cls._drop_flagged_segments(candidate, validation)
        cleaned = cls._clean(cleaned)
        original_len = max(1, len(cls._clean(candidate)))
        retained_ratio = round(len(cleaned) / original_len, 4) if cleaned else 0.0

        if matched != len(errors):
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "not_all_error_evidence_matched",
                "matched_errors": matched,
                "error_count": len(errors),
                "retained_ratio": retained_ratio,
            }
        if len(cleaned) < 24 or retained_ratio < 0.20:
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "too_little_safe_surface_remained",
                "matched_errors": matched,
                "error_count": len(errors),
                "retained_ratio": retained_ratio,
            }
        if cls._player_facing_fragment(cleaned) is None:
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "remaining_surface_not_player_facing",
                "matched_errors": matched,
                "error_count": len(errors),
                "retained_ratio": retained_ratio,
            }

        return cleaned, {
            "strategy": "deterministic_span_removal",
            "status": "candidate",
            "matched_errors": matched,
            "error_count": len(errors),
            "retained_ratio": retained_ratio,
            "removed_characters": max(0, original_len - len(cleaned)),
        }

    @classmethod
    def keep_substantial_opening(
        cls,
        draft: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str | None, dict]:
        """Keep a substantial opening only when Validator classifies leftovers as texture."""
        cleaned = cls._clean(draft)
        if len(cleaned) < 400 or cls._player_facing_fragment(cleaned) is None:
            return None, {
                "strategy": "keep_raw_texture",
                "status": "skipped",
                "reason": "too_short",
            }
        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]
        if not errors:
            return cleaned, {"strategy": "keep_raw_texture", "status": "no_errors"}
        if any(item.violation_type in cls.STATE_BREAKING_VIOLATIONS for item in errors):
            return None, {
                "strategy": "keep_raw_texture",
                "status": "skipped",
                "reason": "state_breaking",
            }
        if not all(item.violation_type in cls.OPENING_TEXTURE_VIOLATIONS for item in errors):
            return None, {
                "strategy": "keep_raw_texture",
                "status": "skipped",
                "reason": "non_texture_violation",
            }
        return cleaned, {
            "strategy": "keep_raw_texture",
            "status": "kept",
            "error_count": len(errors),
        }

    @classmethod
    def render_authority(cls, authority: TurnAuthority) -> str:
        """Render only executed/typed outcomes; never promote guidance hooks into world truth."""
        if authority.clarification_required:
            return authority.clarification_required
        blocked = cls._blocked_in_world_fallback(authority)
        if blocked is not None:
            parts: list[str] = []
            sequence = authority.action_sequence or {}
            steps = sequence.get("steps") if isinstance(sequence, dict) else None
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    if step.get("status") == "completed":
                        safe = cls._player_facing_fragment(step.get("observable_outcome"))
                        if safe:
                            cls._append_unique(parts, safe)
                    elif step.get("status") == "blocked":
                        cls._append_unique(parts, blocked)
            if not parts:
                cls._append_unique(parts, blocked)
            for fragment in cls._response_fragments(authority):
                cls._append_unique(parts, fragment)
            return " ".join(cls._as_sentence(value) for value in parts if value.strip()).strip()

        parts = cls._response_fragments(authority)
        # Observation describes. It does not replace a state a completed world step already set.
        observation_outcomes: set[str] = set()
        replaced_subjects = {
            subject.casefold() for subject in authority.established_subjects
            if subject and subject.casefold() in authority.player_input.casefold()
        }
        sequence = authority.action_sequence if isinstance(authority.action_sequence, dict) else {}
        steps = sequence.get("steps")
        if authority.established_state and isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict) or step.get("action_type") != "observation":
                    continue
                outcome = " ".join(str(step.get("observable_outcome") or "").split())
                if outcome:
                    observation_outcomes.add(outcome)
        # Per-step receipts are authoritative even when the resolver omitted the
        # redundant turn-level consequences. Planned/skipped acts never project.
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict) or step.get("status") != "completed":
                    continue
                outcome = " ".join(str(step.get("observable_outcome") or "").split())
                if outcome in observation_outcomes:
                    if not authority.established_subjects:
                        continue
                    replaced_subjects.update(
                        subject.casefold() for subject in authority.established_subjects
                        if subject and subject.casefold() in outcome.casefold()
                    )
                    outcome = cls._observation_remainder(outcome, authority.established_subjects)
                safe = cls._player_facing_fragment(outcome)
                if safe:
                    cls._append_unique(parts, safe)
        response = authority.addressed_response
        response_contents = {
            " ".join(words.split())
            for words in ([answer.words for answer in response.answers] or [response.direct_response or ""])
        } if response else set()
        for consequence in authority.observable_consequences:
            normalized = " ".join(str(consequence or "").split())
            if normalized in observation_outcomes or normalized in response_contents:
                continue
            safe = cls._player_facing_fragment(consequence)
            if safe:
                cls._append_unique(parts, safe)
        # Stored state replaces observations about the same stored subject; it
        # is not a transcript to append to every unrelated completed result.
        has_executed_result = bool(parts)
        for item in authority.established_state:
            if has_executed_result and authority.established_subjects and not any(
                item.casefold().startswith(subject) for subject in replaced_subjects
            ):
                continue
            safe = cls._player_facing_fragment(item)
            if safe:
                cls._append_unique(parts, safe)

        if (
            not parts
            and authority.source_location_path != authority.target_location_path
            and authority.target_location_path
        ):
            destination = cls._player_facing_fragment(authority.target_location_path[-1])
            if destination:
                # Canonical names are stored in nominative form. Keep them untouched instead of
                # attempting Russian inflection in deterministic code.
                cls._append_unique(parts, f"Вы приходите туда, куда направлялись: {destination}")

        actor_scoped = authority.scene_disposition == "actor_turn" or bool(
            authority.acting_character_id
        )
        if actor_scoped:
            for beat in authority.character_beats:
                safe = cls._player_facing_fragment(beat)
                if safe:
                    cls._append_unique(parts, safe)
            # ending_hook is narrator guidance, not an executed fact. It can contain an unresolved
            # player choice (for example, "Вера решает принять вызов или отказаться"), so the
            # fail-closed deterministic projection must never publish it as if it happened.
            if not parts:
                actor = cls._player_facing_fragment(authority.acting_character_name or "Собеседник")
                cls._append_unique(parts, f"{actor or 'Собеседник'} умолкает")

        if not parts:
            raise NarrationPublicationError(
                "TurnAuthority contains no executed player-facing result", authority
            )

        return " ".join(cls._as_sentence(value) for value in parts if value.strip()).strip()

    @classmethod
    def _response_fragments(cls, authority: TurnAuthority) -> list[str]:
        response = authority.addressed_response
        parts: list[str] = []
        if response:
            labels = {response.speaker_name, authority.acting_character_name, *response.speaker_aliases}
            cleaned_words = []
            for words in response.speech_fragments():
                words = words.strip()
                for _ in range(3):
                    head, sep, tail = words.partition(":")
                    if not sep or head.strip().casefold() not in {
                        name.casefold() for name in labels if name
                    }:
                        break
                    words = tail.strip().strip("«»")
                cleaned_words.append(words)
            for words in [" ".join(dict.fromkeys(cleaned_words))]:
                safe = cls._player_facing_fragment(words)
                if safe:
                    cls._append_unique(
                        parts,
                        f"{response.speaker_name}: «{safe}»" if response.speaker_name else safe,
                    )
            for answer in response.answers:
                if answer.delivery == "nonverbal":
                    safe = cls._player_facing_fragment(answer.words)
                    if safe:
                        cls._append_unique(parts, safe)
        return parts

    @classmethod
    def _blocked_in_world_fallback(cls, authority: TurnAuthority) -> str | None:
        sequence = authority.action_sequence or {}
        steps = sequence.get("steps")
        if not isinstance(steps, list):
            return None
        for step in steps:
            if not isinstance(step, dict) or step.get("status") != "blocked":
                continue
            return TurnAuthority._public_blocked_outcome(step)
        return None

    @classmethod
    def _player_facing_fragment(cls, value: object) -> str | None:
        clean = " ".join(str(value or "").split()).strip()
        if not clean:
            return None
        if cls.DEAD_TURN_PATTERN.fullmatch(clean):
            return None
        if cls.LEGACY_STUB_PATTERN.search(clean):
            return None
        if cls.UUID_PATTERN.search(clean) or cls.TECHNICAL_PATTERN.search(clean):
            return None
        if cls.META_PATTERN.search(clean):
            return None
        if cls.LEDGER_PATTERN.search(clean) or cls.OBLIGATION_PATTERN.search(clean):
            return None
        return clean

    @classmethod
    def _drop_flagged_segments(
        cls,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, int]:
        if not validation or not validation.violations:
            return candidate, 0
        segments = cls._segments(candidate)
        error_evidence = [
            cls._key(item.evidence) for item in validation.violations if item.severity == "error"
        ]
        if not error_evidence:
            return candidate, 0
        matched: set[int] = set()
        kept: list[str] = []
        for segment in segments:
            normalized = cls._key(segment)
            reject = False
            for index, evidence in enumerate(error_evidence):
                if len(evidence) < 6 or not normalized:
                    continue
                if evidence in normalized or normalized in evidence:
                    matched.add(index)
                    reject = True
            if not reject:
                kept.append(segment)
        return " ".join(kept), len(matched)

    @classmethod
    def _drop_player_owned_segments(cls, candidate: str, player_name: str | None) -> str:
        """Deprecated compatibility no-op; player ownership is semantic Validator territory."""
        del cls, player_name
        return candidate

    @staticmethod
    def _segments(text: str) -> list[str]:
        return [
            value.strip()
            for value in re.split(r"(?<=[.!?…])\s+|[\r\n]+", text or "")
            if value.strip()
        ]

    @staticmethod
    def _key(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip(" \t\r\n—–-")

    @classmethod
    def _append_unique(cls, target: list[str], value: object) -> None:
        clean = " ".join(str(value or "").split()).strip()
        if clean and cls._key(clean) not in {cls._key(item) for item in target}:
            target.append(clean)

    @staticmethod
    def _as_sentence(value: str) -> str:
        clean = value.strip()
        if not clean:
            return clean
        return clean if clean.endswith((".", "!", "?", "…", "»", '"')) else clean + "."


__all__ = ["NarrationPublicationError", "NarrationPublicationGuard"]
