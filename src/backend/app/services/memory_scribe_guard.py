from __future__ import annotations

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.services.canon_semantics import proposals_from_envelope

_INSTALLED = False


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
        rejected_actor_knowledge: set[str] = set()
        existing_gaps = set(audit.gap_outcome_ids)

        for proposal in extracted:
            canon_meta = (
                proposal.payload.get("_canon")
                if isinstance(proposal.payload.get("_canon"), dict)
                else {}
            )
            outcome_id = str(canon_meta.get("outcome_id") or "").strip()
            if proposal.change_type == ChangeType.CANON_GAP:
                results.append(proposal)
                if outcome_id:
                    existing_gaps.add(outcome_id)
                continue
            if proposal.change_type == ChangeType.SCENE_THESIS:
                continue

            payload = proposal.payload
            if proposal.change_type == ChangeType.KNOWLEDGE:
                # A belief about what an NPC communicated needs structural provenance: the
                # assistant turn itself must be an actor turn for a registered character. Normal
                # Narrator prose about silence, failed contact, body language or observation is
                # never a knowledge-transfer source.
                if acting_character_id is None:
                    audit.rejected_authority_count += 1
                    audit.envelope_valid = False
                    if outcome_id:
                        rejected_actor_knowledge.add(outcome_id)
                    continue
                payload = dict(payload)
                payload["source_character_id"] = str(acting_character_id)

            normalized = self._normalize_payload(
                proposal.change_type,
                payload,
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
            audit.rejected_schema_count += len(new_gaps)
            audit.envelope_valid = False
            audit.error = "One or more supported outcomes failed backend normalization"

        if rejected_actor_knowledge:
            audit.covered_outcome_count = max(
                0,
                audit.covered_outcome_count - len(rejected_actor_knowledge),
            )

        denominator = audit.covered_outcome_count + audit.gap_count
        audit.coverage_ratio = (
            audit.covered_outcome_count / denominator if denominator else 1.0
        )
        audit.proposal_count = len(results)
        self.last_audit = audit.model_dump()
        return results

    MemoryScribe._parse_data = guarded_parse_data
