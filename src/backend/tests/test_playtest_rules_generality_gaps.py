"""Negative tests for the generality of rules added on fix/playtest-2026-10-12.

Each xfail states a situation where the new deterministic rule must NOT fire and
currently does (see docs/playtest-fixes-review-2026-10-12.md). They encode the
owner's requirement: similar facts/people that are legitimately distinct stay
distinct. Do not mark them passed by weakening the scenario; fix the rule or
move the decision to its semantic owner.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.fact_repo import FactRepository
from app.models.campaign import CampaignCreate
from app.models.fact import FactCreate
from app.services.entity_identity import resolve_character_candidates
from app.services.entity_registrar import EntityRegistrar
from app.services.script_consistency import foreign_script_intrusions

REVIEW = "docs/playtest-fixes-review-2026-10-12.md"


@pytest.mark.xfail(
    strict=False,
    reason=f"free-text subject is a label, not object identity ({REVIEW} §1)",
)
@pytest.mark.asyncio
async def test_two_puddles_of_the_same_label_stay_active_together(db_session: AsyncSession):
    """Two blood puddles at different places are two facts, not one revision."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Two puddles"))
    repo = FactRepository(db_session)
    scene_id = uuid4()

    async def where(value):
        return await repo.apply_change(
            campaign_id,
            FactCreate(
                subject="кровь", predicate="находится", object_value=value,
                memory_kind="scene_state", scene_id=scene_id,
            ),
        )

    await where("у порога")
    await where("на лестнице")

    active = await repo.list_active(campaign_id)
    assert sorted(fact.object_value for fact in active) == ["на лестнице", "у порога"]


def test_adjacent_name_is_only_an_arbitration_candidate_not_an_identity_decision() -> None:
    """The suffix flags ambiguity; the registrar must adjudicate rather than veto."""
    existing = SimpleNamespace(canonical_name="Макар", aliases=[], status="active")
    assert EntityRegistrar._inflected_twin_of_existing("Макара", [existing]) is True


@pytest.mark.xfail(
    strict=False,
    reason=f"a bare role designation must not hijack an unrelated keeper; the slot bind "
    f"is blind to the profession semantics ({REVIEW} §3b)",
)
def test_foreign_role_designator_does_not_hijack_the_keeper() -> None:
    location = uuid4()
    keeper = SimpleNamespace(
        id=uuid4(), canonical_name="Эйрик Хальворсен", aliases=[],
        custom_fields={"role": "конторщик", "slot_id": str(location)},
    )
    candidates = resolve_character_candidates(
        [keeper],
        proposed_name="Старший рыбак сезонной артели",
        proposed_role="старший рыбак сезонной артели",
        temporary_name=True,
        target_location_id=location,
        character_locations={keeper.id: location},
    )
    assert candidates == []


@pytest.mark.xfail(
    strict=False,
    reason=f"the allowed scripts must be derived from the campaign canon, not a fixed "
    f"stop-list ({REVIEW} §6)",
)
def test_the_novel_may_quote_the_settings_own_script() -> None:
    """In a wuxia campaign 武 is canon prose, not decoder noise; the locale-blind
    detector flags the whole page."""
    assert foreign_script_intrusions("武蔵は霧の中で待っていた。") == []
