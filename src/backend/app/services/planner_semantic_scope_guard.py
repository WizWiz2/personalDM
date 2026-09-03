from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.db.tables import Character
from app.models.character import CharacterUpdate
from app.models.turn import ChatMessage
from app.models.turn_authority import ExistingNpcArrival
from app.providers.llm_provider import LLMProviderError
from app.services.entity_identity import identity_key, resolve_character_candidates
from app.services.starter_identity import present_character_names
from app.services.turn_authority_resolvers import (
    NpcIntroductionResolution,
    NpcIntroductionResolver,
)
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_planner import TurnPlanningError
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


def _temporary_fields(entity) -> dict:
    fields = getattr(entity, "custom_fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


async def _apply_contact_recovery_if_needed(
    planner,
    selection,
    context_messages: list[ChatMessage],
    player_input: str,
    plan,
):
    """Resolve the one structurally suspicious case the generic reviewer can miss.

    With at most one physically-present identity, character beats cannot silently materialize a
    second responder. The specialized NPC contact agent decides semantics; this guard only decides
    when that extra adjudication is necessary.
    """

    if plan.npc_introductions or not plan.character_beats:
        return plan
    if len(_unique_presence_keys(context_messages)) > 1:
        return plan

    present_names = list(present_character_names(context_messages))
    issue = (
        "План содержит character_beats при отсутствии типизированного внешнего NPC и при единственной "
        "физически присутствующей личности. Проверь, отвечает ли неизвестный собеседник; если да, "
        "типизируй его до публикации."
    )
    try:
        decision = await planner._recover_npc_contact(
            selection,
            player_input,
            present_names,
            plan,
            [issue],
        )
    except (LLMProviderError, ValueError, TypeError) as exc:
        raise TurnPlanningError(
            "NPC presence recovery failed for a structurally suspicious character beat"
        ) from exc

    if decision.outcome == "no_contact":
        return plan
    if decision.outcome != "introduce" or not decision.npc_introductions:
        raise TurnPlanningError(
            "NPC presence remained ambiguous for a structurally suspicious character beat"
        )

    recovered = plan.model_copy(
        deep=True,
        update={
            "npc_introductions": decision.npc_introductions,
            "addressed_response_requested": True,
            "response_ownership_reason": (
                decision.response_ownership_reason
                or "Неизвестный отвечающий собеседник типизирован специализированным recovery-агентом."
            ),
            "resolution": "conversation",
            "observable_consequences": [
                decision.observable_consequence
                or "Неизвестный собеседник физически отвечает на обращение игрока."
            ],
        },
    )
    planner._sanitize_npc_names(recovered, player_input)
    final_review = await planner._semantic_review(
        selection,
        context_messages,
        player_input,
        recovered,
        present_names,
    )
    if final_review.verdict != "pass":
        problems = final_review.issues or [
            final_review.summary or "NPC recovery plan remained semantically invalid"
        ]
        raise TurnPlanningError(
            "NPC presence recovery remained semantically invalid: " + "; ".join(problems)
        )
    return recovered


async def _promotion_arrivals(
    resolver: NpcIntroductionResolver,
    *,
    campaign_id: UUID,
    introductions: list,
    target_location_id: UUID | None,
) -> dict[UUID, ExistingNpcArrival]:
    """Find exact temporary-role identities whose explicit name has just been revealed."""

    if target_location_id is None:
        return {}
    all_characters = await resolver._entities.list_by_campaign(
        campaign_id,
        entity_type="character",
    )
    ids = [str(entity.id) for entity in all_characters]
    rows = []
    if ids:
        rows = (
            await resolver._session.execute(
                select(Character).where(Character.entity_id.in_(ids))
            )
        ).scalars().all()
    locations = {
        UUID(row.entity_id): UUID(row.current_location_id) if row.current_location_id else None
        for row in rows
    }

    promotions: dict[UUID, ExistingNpcArrival] = {}
    for introduction in introductions:
        if getattr(introduction, "temporary_name", False):
            continue
        matches = resolve_character_candidates(
            all_characters,
            proposed_name=introduction.canonical_name,
            proposed_role=introduction.role,
            temporary_name=False,
            target_location_id=target_location_id,
            character_locations=locations,
        )
        unique = {UUID(str(entity.id)): entity for entity in matches}
        if len(unique) != 1:
            continue
        entity_id, entity = next(iter(unique.items()))
        fields = _temporary_fields(entity)
        if not fields.get("temporary_name"):
            continue
        if identity_key(entity.canonical_name) == identity_key(introduction.canonical_name):
            continue
        promotions[entity_id] = ExistingNpcArrival(
            entity_id=entity_id,
            canonical_name=introduction.canonical_name,
            reason=introduction.reason,
        )
    return promotions


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


async def _promote_authorized_temporary_identities(
    materializer: TurnOutcomeMaterializer,
    authority,
    source_turn_id: UUID,
) -> tuple[IdentityPromotionSnapshot, ...]:
    snapshots: list[IdentityPromotionSnapshot] = []
    for arrival in authority.allowed_existing_npc_arrivals:
        character = await materializer._entities.get_character(arrival.entity_id)
        if character is None:
            continue
        fields = _temporary_fields(character)
        if not fields.get("temporary_name"):
            continue
        if identity_key(character.canonical_name) == identity_key(arrival.canonical_name):
            continue

        snapshot = IdentityPromotionSnapshot(
            entity_id=character.id,
            canonical_name=character.canonical_name,
            aliases=tuple(character.aliases),
            custom_fields=dict(fields),
        )
        aliases = list(character.aliases)
        if character.canonical_name not in aliases:
            aliases.append(character.canonical_name)
        promoted_fields = dict(fields)
        promoted_fields["temporary_name"] = False
        promoted_fields[_PROMOTION_KEY] = {
            "source_turn_id": str(source_turn_id),
            "previous_canonical_name": character.canonical_name,
            "previous_aliases": list(character.aliases),
            "previous_custom_fields": dict(fields),
        }
        await materializer._entities.update_character(
            character.id,
            CharacterUpdate(
                canonical_name=arrival.canonical_name,
                aliases=aliases,
                custom_fields=promoted_fields,
            ),
        )
        snapshots.append(snapshot)
    return tuple(snapshots)


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

    from app.services.turn_authority_planner import TurnAuthorityPlanner

    if _SEMANTIC_SCOPE_CONTRACT not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _SEMANTIC_SCOPE_CONTRACT

    original_plan = TurnAuthorityPlanner.plan

    async def semantically_scoped_plan(
        self,
        selection,
        context_messages,
        *,
        latest_user_input=None,
    ):
        plan = await original_plan(
            self,
            selection,
            context_messages,
            latest_user_input=latest_user_input,
        )
        player_input = latest_user_input or self._latest_user_text(context_messages)
        return await _apply_contact_recovery_if_needed(
            self,
            selection,
            context_messages,
            player_input,
            plan,
        )

    TurnAuthorityPlanner.plan = semantically_scoped_plan

    original_resolve = NpcIntroductionResolver.resolve

    async def promotion_aware_resolve(
        self,
        *,
        campaign_id,
        introductions,
        present_names,
        target_location_id,
    ):
        result = await original_resolve(
            self,
            campaign_id=campaign_id,
            introductions=introductions,
            present_names=present_names,
            target_location_id=target_location_id,
        )
        promotions = await _promotion_arrivals(
            self,
            campaign_id=campaign_id,
            introductions=introductions,
            target_location_id=target_location_id,
        )
        if not promotions:
            return result

        arrivals = {
            arrival.entity_id: arrival for arrival in result.existing_arrivals
        }
        arrivals.update(promotions)
        names = list(result.present_names)
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        by_id = {UUID(str(entity.id)): entity for entity in all_characters}
        for entity_id, arrival in promotions.items():
            old = by_id.get(entity_id)
            old_key = identity_key(old.canonical_name) if old else ""
            replaced = False
            for index, name in enumerate(names):
                if old_key and identity_key(name) == old_key:
                    names[index] = arrival.canonical_name
                    replaced = True
            if not replaced and identity_key(arrival.canonical_name) not in {
                identity_key(name) for name in names
            }:
                names.append(arrival.canonical_name)

        return NpcIntroductionResolution(
            new_introductions=result.new_introductions,
            existing_arrivals=list(arrivals.values()),
            present_names=names,
        )

    NpcIntroductionResolver.resolve = promotion_aware_resolve

    original_materialize = TurnOutcomeMaterializer.materialize
    original_bind = TurnOutcomeMaterializer.bind_to_assistant
    original_rollback = TurnOutcomeMaterializer.rollback

    async def promotion_aware_materialize(self, authority, *, source_turn_id):
        promotions = await _promote_authorized_temporary_identities(
            self,
            authority,
            source_turn_id,
        )
        outcome = await original_materialize(
            self,
            authority,
            source_turn_id=source_turn_id,
        )
        return GuardedMaterializedTurnOutcome(
            introduced_character_ids=tuple(outcome.introduced_character_ids),
            arrived_existing_participants=tuple(outcome.arrived_existing_participants),
            identity_promotions=promotions,
        )

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

    TurnOutcomeMaterializer.materialize = promotion_aware_materialize
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
    "_apply_contact_recovery_if_needed",
    "_unique_presence_keys",
    "install",
]
