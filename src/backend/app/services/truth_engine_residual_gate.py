from __future__ import annotations

import json
from collections import Counter
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.truth_engine_residual import (
    ResidualAtomDisposition,
    ResidualClassificationResult,
    ResidualDispositionEnvelope,
    SemanticResidualEnvelope,
    objective_residual,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider
from app.services.role_model_router import ModelRole, RoleModelRouter


class SemanticResidualDispositionGate:
    """Classify already-extracted atoms without allowing the model to create or rewrite any atom.

    The extractor is intentionally recall-oriented. This gate owns the narrower question: whether an
    existing observation is safe for objective canonical state. Its response can refer only to stable
    backend atom keys. Missing, duplicated or invented decisions fail closed to ``unsupported``.
    """

    SYSTEM_PROMPT = """[TE2 OBJECTIVE TRUTH DISPOSITION GATE]
You receive semantic atoms already extracted from one completed RPG turn. You MUST NOT create, merge,
rewrite or remove atoms. Classify every supplied atom_key exactly once using one disposition:

- objective: durable objective world state/relation established by the completed turn.
- epistemic: a belief, claim, rumour, memory, suspicion, report, opinion or other entity-relative
  knowledge rather than objective world truth.
- transient: a momentary action/interation that occurred but is not itself durable current world
  state, such as speaking, listening, remembering, looking, handing something over, or walking.
- receipt_owned: the atom restates physical movement, item ownership/placement, time/focus or another
  state/action already authoritatively represented by STRUCTURED_RECEIPTS.
- presentation: mood, prose colour, framing, scene texture or other narrative presentation detail.
- unsupported: not sufficiently established as durable objective truth, ambiguous, or otherwise unsafe.

Important boundaries:
- Player input alone is never enough to make a world claim objective.
- Dialogue content remains epistemic unless the completed narration independently establishes the
  proposition as objective world truth outside the speaker's claim.
- A structured receipt owns the physical action/state it records. Do not classify a duplicate of that
  action/state as objective merely because narration repeats it.
- A durable semantic consequence can still be objective even when a receipt triggered it. For example,
  if narration explicitly establishes that a pre-existing ongoing relation has ended, that relation
  state is not the same thing as the physical receipt that caused it.
- Prefer unsupported over objective when uncertain.

Return only decisions over the supplied atom_key values. Never invent an atom_key."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        model_router: RoleModelRouter | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self._model_router = model_router or RoleModelRouter(ProviderConfigRepository(session))
        self._llm_provider = llm_provider or LLMProvider()

    async def classify(
        self,
        campaign_id: UUID,
        *,
        envelope: SemanticResidualEnvelope,
        user_content: str,
        assistant_content: str,
        structured_receipts: list[dict] | None = None,
    ) -> ResidualClassificationResult:
        atoms = self._atom_payloads(envelope)
        if not atoms:
            return ResidualClassificationResult(
                decisions=[],
                objective=SemanticResidualEnvelope(),
            )

        allowed_keys = {atom["atom_key"] for atom in atoms}
        selection = await self._model_router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection is None:
            decisions = [
                ResidualAtomDisposition(
                    atom_key=atom_key,
                    disposition="unsupported",
                    reason="No semantic disposition model is configured.",
                )
                for atom_key in sorted(allowed_keys)
            ]
            return ResidualClassificationResult(
                decisions=decisions,
                objective=objective_residual(envelope, decisions),
            )

        data = await self._model_router.generate_json(
            self._llm_provider,
            selection,
            [
                ChatMessage(role="system", content=self.SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "structured_receipts": structured_receipts or [],
                            "player_input": user_content,
                            "completed_narration": assistant_content,
                            "entities": [
                                entity.model_dump(mode="json") for entity in envelope.entities
                            ],
                            "atoms": atoms,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
            ],
            max_tokens=max(450, 120 * len(atoms)),
            temperature=0.0,
            response_model=ResidualDispositionEnvelope,
        )
        returned = ResidualDispositionEnvelope.model_validate(data).decisions
        decisions = self._normalize_decisions(returned, allowed_keys)
        return ResidualClassificationResult(
            decisions=decisions,
            objective=objective_residual(envelope, decisions),
        )

    @staticmethod
    def _atom_payloads(envelope: SemanticResidualEnvelope) -> list[dict]:
        result: list[dict] = []
        for atom in envelope.fluents:
            payload = atom.model_dump(mode="json")
            payload["kind"] = "fluent"
            result.append(payload)
        for atom in envelope.relations:
            payload = atom.model_dump(mode="json")
            payload["kind"] = "relation"
            result.append(payload)
        return sorted(result, key=lambda item: str(item["atom_key"]))

    @staticmethod
    def _normalize_decisions(
        returned: list[ResidualAtomDisposition],
        allowed_keys: set[str],
    ) -> list[ResidualAtomDisposition]:
        counts = Counter(
            decision.atom_key for decision in returned if decision.atom_key in allowed_keys
        )
        unique = {
            decision.atom_key: decision
            for decision in returned
            if decision.atom_key in allowed_keys and counts[decision.atom_key] == 1
        }
        result: list[ResidualAtomDisposition] = []
        for atom_key in sorted(allowed_keys):
            decision = unique.get(atom_key)
            if decision is None:
                result.append(
                    ResidualAtomDisposition(
                        atom_key=atom_key,
                        disposition="unsupported",
                        reason="Disposition response omitted or duplicated this atom key.",
                    )
                )
            else:
                result.append(decision)
        return result


__all__ = ["SemanticResidualDispositionGate"]
