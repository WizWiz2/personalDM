from __future__ import annotations

from app.config import settings
from app.models.player_intent import PlayerIntentContract, PlayerIntentReview
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlanningError


_INTENT_PROMPT = """[PLAYER INTENT INTERPRETER]
You convert exactly one human RPG turn into immutable player-authority IR. You do NOT resolve the
world, decide success/failure, invent NPC reactions, choose routes, write prose, or repair campaign
state. Return exactly PlayerIntentContract.

The contract contains only what the HUMAN actually committed to now:
- actions is an ordered list of affirmative atomic world actions. Preserve their stated order.
- Ordinary speech, a greeting, a question, a claim, or telling someone information is NOT an action
  step. Represent expected dialogue with addressed_response_requested/addressed_character_name.
- A negative/stationary boundary ("не иду", "остаюсь здесь", "не проверяю") is not an action.
- An unresolved alternative/condition is not executed. Preserve it in pending_player_choice and/or
  protected_player_decisions instead of choosing a branch.
- movement means changing canonical physical location. Moving/turning/approaching within the current
  room/scene is interaction, not movement.
- For movement, destination_location is only the human-selected endpoint for that atomic move, never
  a route policy or prose route path. Preserve two movement actions only when the human actually
  commits to reaching two distinct location boundaries in order. Route media such as stairs,
  corridor or courtyard are not promoted to actions when they merely describe the path to one final
  destination.
- Do not decide whether a destination already exists, whether a route may be discovered, or whether
  the move is possible. The deterministic world compiler owns all of that after this contract freezes.
- Inventory fields must use IDs supplied by AUTHORITATIVE CONTEXT. Never invent IDs. give requires
  the recipient entity id. drop means release into the current place; place means a named
  surface/container/position.
- rest/wait may carry elapsed_time/time_after only when the human establishes it.
- identity_reveal_requested=true when the human explicitly asks a present person for their name.
- addressed_character_name must use the current known designation, never a future/invented answer.

Do not encode consequences in the intent. No action here means the world is unchanged yet; a later
outcome resolver owns external consequences.
"""

_REVIEW_PROMPT = """[PLAYER INTENT FIDELITY REVIEW]
You verify only HUMAN INPUT -> PlayerIntentContract fidelity. You do not judge world feasibility,
route availability, success/failure, NPC behavior, prose quality, or whether a destination exists.
Return exactly PlayerIntentReview.

Return repair_required only when the contract:
- omits an affirmative committed world action;
- adds an action/choice not supplied by the human;
- changes action order;
- turns speech/question/claim into a world action;
- turns a negative/stationary clause into an action;
- turns local body motion inside one scene into canonical movement;
- loses an explicitly committed intermediate destination, or invents route-media as another action;
- chooses an unresolved alternative;
- misstates addressed-response or explicit identity-request ownership.

Do not ask for richer detail and do not propose world outcomes. Issues must describe only the
input/IR mismatch.
"""

_REPAIR_PROMPT = """[PLAYER INTENT IR REPAIR]
Repair only the listed fidelity defects between the latest human input and the rejected
PlayerIntentContract. Return one complete replacement PlayerIntentContract. Do not resolve the world,
invent consequences, or change actions that were not implicated by the issues.
"""


class PlayerIntentInterpreter:
    """One semantic owner for human commitments, with exactly one bounded repair opportunity."""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self.audit: list[dict] = []

    @staticmethod
    def _authoritative_context(context_messages: list[ChatMessage]) -> str:
        """Keep semantic parsing anchored to machine/campaign state without replaying prose history."""
        if not context_messages:
            return ""
        # ContextCompiler puts its layered authoritative state in the first system message. The
        # latest human input is supplied separately below, so old transcript turns are intentionally
        # excluded from intent extraction.
        return context_messages[0].content

    async def _interpret_once(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        *,
        repair_issues: list[str] | None = None,
        rejected: PlayerIntentContract | None = None,
    ) -> PlayerIntentContract:
        user = (
            "[AUTHORITATIVE CONTEXT]\n"
            + self._authoritative_context(context_messages)
            + "\n\n[LATEST HUMAN INPUT]\n"
            + player_input
        )
        system = _INTENT_PROMPT
        if repair_issues is not None and rejected is not None:
            system += "\n\n" + _REPAIR_PROMPT
            user += (
                "\n\n[REVIEW ISSUES]\n- "
                + "\n- ".join(repair_issues)
                + "\n\n[REJECTED INTENT CONTRACT]\n"
                + rejected.model_dump_json()
            )
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user),
            ],
            max_tokens=1000,
            temperature=0.0 if repair_issues is not None else settings.PLANNER_TEMPERATURE,
            response_model=PlayerIntentContract,
        )
        return PlayerIntentContract.model_validate(data)

    async def _review(
        self,
        selection: RoleModelSelection,
        player_input: str,
        contract: PlayerIntentContract,
    ) -> PlayerIntentReview:
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(role="system", content=_REVIEW_PROMPT),
                ChatMessage(
                    role="user",
                    content=(
                        "[LATEST HUMAN INPUT]\n"
                        + player_input
                        + "\n\n[PLAYER INTENT CONTRACT]\n"
                        + contract.model_dump_json()
                    ),
                ),
            ],
            max_tokens=500,
            temperature=0.0,
            response_model=PlayerIntentReview,
        )
        return PlayerIntentReview.model_validate(data)

    async def interpret(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
    ) -> PlayerIntentContract:
        """Interpret -> review -> at most one repair -> final review, then freeze."""
        try:
            contract = await self._interpret_once(
                selection,
                context_messages,
                player_input,
            )
            review = await self._review(selection, player_input, contract)
            self.audit.append(
                {
                    "phase": "initial",
                    "intent": contract.model_dump(mode="json"),
                    "review": review.model_dump(mode="json"),
                }
            )
            if review.verdict == "pass":
                return contract

            issues = review.issues or [review.summary or "Intent contract is not faithful."]
            repaired = await self._interpret_once(
                selection,
                context_messages,
                player_input,
                repair_issues=issues,
                rejected=contract,
            )
            final_review = await self._review(selection, player_input, repaired)
            self.audit.append(
                {
                    "phase": "repair",
                    "intent": repaired.model_dump(mode="json"),
                    "review": final_review.model_dump(mode="json"),
                }
            )
            if final_review.verdict != "pass":
                remaining = final_review.issues or [
                    final_review.summary or "Intent contract remained unfaithful."
                ]
                raise TurnPlanningError(
                    "player intent contract remained invalid after one repair: "
                    + "; ".join(remaining)
                )
            return repaired
        except TurnPlanningError:
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(f"player intent interpretation failed: {exc}") from exc


__all__ = ["PlayerIntentInterpreter"]
