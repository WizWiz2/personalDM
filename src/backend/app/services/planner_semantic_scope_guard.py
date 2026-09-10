from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.models.character import CharacterUpdate
from app.models.turn import ChatMessage
from app.services.entity_identity import identity_key
from app.services.starter_identity import present_character_names
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_undo_service import TurnUndoService

_INSTALLED = False
_PROMOTION_KEY = "identity_promotion"

_SEMANTIC_SCOPE_CONTRACT = """

[SEMANTIC REVIEW SCOPE — AUTHORITATIVE]
You are a contract verifier, NOT a literary critic, alternate-story generator, or completeness editor
beyond the actions the human actually committed to.
- Do not reject a valid plan because an NPC could hypothetically refuse, react differently, provide
  more detail, or because another plausible outcome exists. Judge the chosen current outcome only.
- Do not demand richer prose/detail from Planner fields when the typed result is already concrete
  enough to render. Literary quality belongs to Narrator, not semantic plan review.
- Negative/stationary clauses such as `остаюсь на месте`, `не иду`, `не трогаю`, `не проверяю`, or
  equivalent constraints are boundaries on what must NOT happen; they are not separate committed
  world actions and do not require action_sequence steps.
- A review that claims a movement/focus action is missing must be grounded in an affirmative physical
  commitment in the latest human input. A social addressee, selected listener, question target, or
  stationary clause is not evidence for movement. Never repair a stationary conversation by adding
  travel, approach, or focus_transition that the human did not commit to.
- A blocked action step is semantically complete when resolution=blocked and blocking_reason states
  the concrete current obstacle. A blocked step does NOT require observable_outcome and must not be
  rejected merely because the attempted action did not occur.
- Require action_sequence coverage only for affirmative committed world actions. Do not invent a
  second action from clarification, negation, a state constraint, or a statement explaining why an
  attempted later step is blocked.
- For direct contact, physical presence remains strict: if an unknown person actually replies,
  reacts, or otherwise acts in the proposed current outcome, that person must be typed in
  npc_introductions. This is a canon/presence defect, unlike missing literary detail.
"""


def _clean_presence_name(value: object) -> str:
    text = " ".join(str(value or "").split())
    if " [id=" in text:
        text = text.split(" [id=", 1)[0].strip()
    return identity_key(text)


def _unique_presence_keys(messages: list[ChatMessage]) -> set[str]:
    return {
        key
        for key in (_clean_presence_name(value) for value in present_character_names(messages))
        if key
    }


def _normalize_unproven_npc_introductions(plan):
    """Downgrade unsupported stable names to role-grounded temporary identities.

    This is an authority normalization, not a prose/name classifier. The planner already typed both
    the role and whether it claims a stable personal identity. A stable identity without explicit
    personal_name_evidence has no pre-publication authority, so preserving the role while marking it
    temporary is strictly less permissive than accepting the model-authored personal name. If no
    usable role exists, leave the introduction untouched so the semantic reviewer can fail closed.
    """

    normalized = []
    used: set[str] = set()
    changed = False

    for introduction in plan.npc_introductions:
        canonical = " ".join(str(introduction.canonical_name or "").split())
        evidence = " ".join(str(introduction.personal_name_evidence or "").split())
        canonical_key = identity_key(canonical)

        if introduction.temporary_name or evidence:
            normalized.append(introduction)
            if canonical_key:
                used.add(canonical_key)
            continue

        role = " ".join(str(introduction.role or "").split())
        if not role:
            normalized.append(introduction)
            if canonical_key:
                used.add(canonical_key)
            continue

        base = role[0].upper() + role[1:] if role else role
        candidate = base
        index = 2
        while identity_key(candidate) in used:
            candidate = f"{base} {index}"
            index += 1

        normalized.append(
            introduction.model_copy(
                update={
                    "canonical_name": candidate,
                    "temporary_name": True,
                    "personal_name_evidence": None,
                }
            )
        )
        used.add(identity_key(candidate))
        changed = True

    if changed:
        # CoordinatedTurnPlan is intentionally mutable during pre-execution normalization. Mutating
        # the same instance matters here: the planner state machine keeps this object after review.
        plan.npc_introductions = normalized
    return plan


def _temporary_fields(entity) -> dict:
    fields = getattr(entity, "custom_fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


@dataclass(frozen=True)
class IdentityPromotionSnapshot:
    entity_id: UUID
    canonical_name: str
    aliases: tuple[str, ...]
    custom_fields: dict


@dataclass(frozen=True)
class GuardedMaterializedTurnOutcome:
    introduced_character_ids: tuple[UUID, ...] = ()
    arrived_existing_participants: tuple[tuple[UUID, UUID], ...] = ()
    identity_promotions: tuple[IdentityPromotionSnapshot, ...] = ()

    @property
    def arrived_existing_character_ids(self) -> tuple[UUID, ...]:
        return tuple(entity_id for _scene_id, entity_id in self.arrived_existing_participants)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.introduced_character_ids
            or self.arrived_existing_participants
            or self.identity_promotions
        )


async def _restore_promotion_snapshots(materializer, snapshots) -> None:
    for snapshot in snapshots:
        await materializer._entities.update_character(
            snapshot.entity_id,
            CharacterUpdate(
                canonical_name=snapshot.canonical_name,
                aliases=list(snapshot.aliases),
                custom_fields=dict(snapshot.custom_fields),
            ),
        )


async def _restore_published_promotions(session, campaign_id: UUID, assistant_turn_id: UUID) -> None:
    from app.db.repositories.entity_repo import EntityRepository

    entities = EntityRepository(session)
    for character in await entities.list_by_campaign(campaign_id, entity_type="character"):
        fields = _temporary_fields(character)
        promotion = fields.get(_PROMOTION_KEY)
        if not isinstance(promotion, dict):
            continue
        if promotion.get("source_turn_id") != str(assistant_turn_id):
            continue
        previous_fields = promotion.get("previous_custom_fields")
        if not isinstance(previous_fields, dict):
            previous_fields = {}
        previous_name = str(promotion.get("previous_canonical_name") or "").strip()
        if not previous_name:
            continue
        aliases = promotion.get("previous_aliases")
        if not isinstance(aliases, list):
            aliases = []
        await entities.update_character(
            character.id,
            CharacterUpdate(
                canonical_name=previous_name,
                aliases=aliases,
                custom_fields=previous_fields,
            ),
        )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.turn_authority_planner import SemanticPlanReview, TurnAuthorityPlanner

    if _SEMANTIC_SCOPE_CONTRACT not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _SEMANTIC_SCOPE_CONTRACT

    # Preserve the public TurnAuthorityPlanner.plan boundary owned by systemless_authority_guard.
    # Presence suspicion is expressed as a semantic review failure so the existing repair/recovery
    # state machine remains the only plan orchestration path.
    original_review = TurnAuthorityPlanner._semantic_review

    async def presence_scoped_review(
        self,
        selection,
        context_messages,
        player_input,
        plan,
        present_names=None,
    ):
        _normalize_unproven_npc_introductions(plan)
        review = await original_review(
            self,
            selection,
            context_messages,
            player_input,
            plan,
            present_names,
        )
        if review.verdict != "pass":
            return review
        if plan.npc_introductions or not plan.character_beats:
            return review
        if len(_unique_presence_keys(context_messages)) > 1:
            return review
        issue = (
            "NPC/PRESENCE: character_beats описывают действие/ответ внешнего персонажа, но при "
            "единственной физически присутствующей личности npc_introductions пуст. Нужно либо "
            "типизировать неизвестного отвечающего NPC, либо убрать его действия из результата."
        )
        return SemanticPlanReview(
            verdict="repair_required",
            summary="План не типизирует возможного внешнего отвечающего персонажа.",
            issues=[issue],
        )

    TurnAuthorityPlanner._semantic_review = presence_scoped_review

    # Compatibility for undoing historical promotions; new name bindings are owned
    # exclusively by the registrar after publication, never by role/location matches.
    original_bind = TurnOutcomeMaterializer.bind_to_assistant
    original_rollback = TurnOutcomeMaterializer.rollback

    async def promotion_aware_bind(self, outcome, assistant_turn_id):
        await original_bind(self, outcome, assistant_turn_id)
        for snapshot in getattr(outcome, "identity_promotions", ()):
            character = await self._entities.get_character(snapshot.entity_id)
            if character is None:
                continue
            fields = _temporary_fields(character)
            promotion = fields.get(_PROMOTION_KEY)
            if not isinstance(promotion, dict):
                continue
            promotion = dict(promotion)
            promotion["source_turn_id"] = str(assistant_turn_id)
            fields[_PROMOTION_KEY] = promotion
            await self._entities.update_character(
                snapshot.entity_id,
                CharacterUpdate(custom_fields=fields),
            )

    async def promotion_aware_rollback(self, outcome):
        await original_rollback(self, outcome)
        await _restore_promotion_snapshots(
            self,
            getattr(outcome, "identity_promotions", ()),
        )

    TurnOutcomeMaterializer.bind_to_assistant = promotion_aware_bind
    TurnOutcomeMaterializer.rollback = promotion_aware_rollback

    original_reconcile = TurnUndoService._reconcile_derived_state

    async def promotion_aware_reconcile(self, campaign_id, assistant_turn_id):
        await original_reconcile(self, campaign_id, assistant_turn_id)
        await _restore_published_promotions(
            self._session,
            campaign_id,
            assistant_turn_id,
        )

    TurnUndoService._reconcile_derived_state = promotion_aware_reconcile
    _INSTALLED = True


__all__ = [
    "GuardedMaterializedTurnOutcome",
    "IdentityPromotionSnapshot",
    "_SEMANTIC_SCOPE_CONTRACT",
    "_normalize_unproven_npc_introductions",
    "_unique_presence_keys",
    "install",
]
