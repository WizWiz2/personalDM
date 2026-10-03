from __future__ import annotations

_INSTALLED = False

_NPC_PROFILE_PLANNING_CONTRACT = """

[NPC PUBLIC PROFILE — DURABLE CHARACTER CARD]
Every genuinely new NPC in npc_introductions becomes a durable Character and may immediately receive
an automatically generated portrait. Treat its public profile as part of the typed world outcome,
not as optional flavor.
- description: write a useful public character-card description (normally 1–3 concise sentences).
  State who this person appears to be in the current fiction, their outward demeanor/role and one or
  two memorable public traits. Do not include hidden motives, secrets or facts the player has not
  learned.
- appearance: give a concrete portrait-ready visual description: apparent age range, build/posture,
  face/hair or equivalent species features, clothing/gear and distinctive visible details. Prefer
  stable traits that future scenes and portrait generation can preserve.
- voice: when the NPC speaks, give a short stable description of voice/speech manner when useful.
- description and appearance must be actual descriptions, not just a repetition of canonical_name or
  role. Keep them setting-appropriate and consistent with established campaign context.
Narrator receives this exact profile and should render it consistently. The generated portrait is
DERIVED from this canonical public profile and never becomes a source of canon by itself.
"""

_NPC_PROFILE_REVIEW_CONTRACT = """

- NPC PROFILE: every genuinely new npc_introduction must carry a useful public `description` and a
  concrete portrait-ready `appearance`. Missing, empty, role-only, or token descriptions require
  repair. The profile may establish outward traits for a newly created NPC, but must not expose
  hidden motives/secrets or contradict campaign context.
"""

def npc_profile_issues(plan) -> list[str]:
    """Machine-provable minimum for a newly canonicalized public NPC profile."""
    issues: list[str] = []
    for item in getattr(plan, "npc_introductions", []) or []:
        description = " ".join(str(getattr(item, "description", "") or "").split())
        appearance = " ".join(str(getattr(item, "appearance", "") or "").split())
        role = " ".join(str(getattr(item, "role", "") or "").split())
        name = str(getattr(item, "canonical_name", "новый NPC"))
        if len(description) < 32 or (role and description.casefold() == role.casefold()):
            issues.append(
                f"Для нового NPC «{name}» нужен содержательный public description, а не имя/роль-заглушка."
            )
        if len(appearance) < 32:
            issues.append(
                f"Для нового NPC «{name}» нужен конкретный portrait-ready appearance с устойчивыми видимыми чертами."
            )
    return issues


def install() -> None:
    """Add NPC-profile planning contracts and the structural no-check executor defense."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.action_sequence_executor import ActionSequenceExecutor
    from app.services.turn_authority_planner import SemanticPlanReview, TurnAuthorityPlanner

    if _NPC_PROFILE_PLANNING_CONTRACT not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _NPC_PROFILE_PLANNING_CONTRACT
    if _NPC_PROFILE_REVIEW_CONTRACT not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _NPC_PROFILE_REVIEW_CONTRACT
    if _NPC_PROFILE_PLANNING_CONTRACT not in TurnAuthorityPlanner.NPC_CONTACT_RECOVERY_PROMPT:
        TurnAuthorityPlanner.NPC_CONTACT_RECOVERY_PROMPT += _NPC_PROFILE_PLANNING_CONTRACT

    current_plan_review = TurnAuthorityPlanner._semantic_review

    async def profile_enforced_plan_review(
        self,
        selection,
        context_messages,
        player_input,
        plan,
        present_names=None,
    ):
        result = await current_plan_review(
            self,
            selection,
            context_messages,
            player_input,
            plan,
            present_names,
        )
        if result.verdict != "pass":
            return result
        issues = npc_profile_issues(plan)
        if not issues:
            return result
        return SemanticPlanReview(
            verdict="repair_required",
            summary="Новый NPC должен получить устойчивое публичное описание и внешность.",
            issues=issues,
        )

    TurnAuthorityPlanner._semantic_review = profile_enforced_plan_review

    original_execute = ActionSequenceExecutor.execute

    async def reject_escaped_checks(
        self,
        campaign_id,
        source_scene_id,
        trigger_turn_id,
        plan,
        **kwargs,
    ):
        if any(getattr(step, "resolution", None) == "requires_check" for step in plan.steps):
            raise ValueError(
                "systemless planner contract violation: requires_check escaped typed planning"
            )
        return await original_execute(
            self,
            campaign_id,
            source_scene_id,
            trigger_turn_id,
            plan,
            **kwargs,
        )

    ActionSequenceExecutor.execute = reject_escaped_checks
    _INSTALLED = True


__all__ = ["install", "npc_profile_issues"]
