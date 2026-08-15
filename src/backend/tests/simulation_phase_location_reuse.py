from __future__ import annotations

from app.models.entity import EntityCreate, EntityType


def install_phase_location_reuse(runtime) -> None:
    """Make benchmark phase seeding idempotent without changing the product engine."""

    director_cls = runtime.ScenarioDirector
    if getattr(director_cls, "_phase_location_reuse_installed", False):
        return

    original_enter_phase = director_cls.enter_phase

    async def resolve_phase_location(self, phase):
        locations = await self.entities.list_by_campaign(self.campaign_id, "location")
        exact = [
            entity
            for entity in locations
            if entity.canonical_name.casefold() == phase.title.casefold()
        ]
        if len(exact) > 1:
            raise RuntimeError(
                f"Benchmark invariant violated: duplicate phase locations named {phase.title!r}"
            )
        if exact:
            return exact[0]
        return await self.entities.create(
            self.campaign_id,
            EntityCreate(
                entity_type=EntityType.LOCATION,
                canonical_name=phase.title,
                description=phase.location_description,
                custom_fields={"scenario_phase": phase.slug},
            ),
        )

    async def idempotent_enter_phase(self, index, state):
        phase = runtime.PHASES[index]
        existing_scenes = await self.scenes.list_by_campaign(self.campaign_id)
        existing = next(
            (scene for scene in existing_scenes if scene.title == phase.title),
            None,
        )
        if existing and existing.status == "active":
            return await original_enter_phase(self, index, state)

        location = await resolve_phase_location(self, phase)
        original_create = self.entities.create

        async def reuse_phase_create(campaign_id, data):
            if (
                data.entity_type == EntityType.LOCATION
                and data.canonical_name.casefold() == phase.title.casefold()
            ):
                return location
            return await original_create(campaign_id, data)

        self.entities.create = reuse_phase_create
        try:
            return await original_enter_phase(self, index, state)
        finally:
            self.entities.create = original_create

    director_cls.enter_phase = idempotent_enter_phase
    director_cls._phase_location_reuse_installed = True
