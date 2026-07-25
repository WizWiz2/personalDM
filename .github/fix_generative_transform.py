from pathlib import Path

path = Path(".github/generative_simulation_transform.py")
text = path.read_text(encoding="utf-8")

# Keep Fact.confidence in both the old and new source snippets.
visibility_anchor = (
    "    '''    visibility: Mapped[str] = mapped_column(String(50), "
    "default=\"dm\", nullable=False)"
)
visibility_replacement = (
    "    '''    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)\n"
    "    visibility: Mapped[str] = mapped_column(String(50), default=\"dm\", nullable=False)"
)
if text.count(visibility_anchor) != 2:
    raise SystemExit(
        f"fact-scope transform anchor count={text.count(visibility_anchor)}"
    )
text = text.replace(visibility_anchor, visibility_replacement)

# The runtime only needs conversion helpers, not the catalog type itself.
catalog_import = "        CampaignCatalog,\n"
if text.count(catalog_import) != 2:
    raise SystemExit(f"CampaignCatalog import count={text.count(catalog_import)}")
text = text.replace(catalog_import, "")


def remove_replace_once(marker: str) -> None:
    global text
    marker_index = text.find(marker)
    if marker_index < 0:
        raise SystemExit(f"memory-scribe transform marker missing: {marker[:100]!r}")
    start = text.rfind("replace_once(\n", 0, marker_index)
    if start < 0:
        raise SystemExit(f"replace_once start missing for: {marker[:100]!r}")
    end = text.find("\n)\n", marker_index)
    if end < 0:
        raise SystemExit(f"replace_once end missing for: {marker[:100]!r}")
    text = text[:start] + text[end + 3 :]


# Do not thread scene_id through private parsing helpers: tests and extensions
# intentionally wrap these stable signatures.
for marker in (
    "'''            scene_participant_ids=scene_participant_ids,",
    "'''        authoritative_text: str = \"\",",
    "'''            player_character_id,\n            scene_participant_ids,",
    "'''        scene_participant_ids: list[str],\n    ) -> list[ProposedChangeCreate]:",
    "'''                player_character_id,\n                scene_participant_ids,",
    "'''        scene_participant_ids: list[str],\n    ) -> dict | None:",
):
    remove_replace_once(marker)

# Fact normalization reads the scene captured by the production processing call.
fact_scope_old = '''            if scope == "scene" and scene_id is not None:
                resolved["scope"] = "scene"
                resolved["scene_id"] = str(scene_id)
'''
fact_scope_new = '''            current_scene_id = getattr(self, "_current_scene_id", None)
            if scope == "scene" and current_scene_id is not None:
                resolved["scope"] = "scene"
                resolved["scene_id"] = str(current_scene_id)
'''
if text.count(fact_scope_old) != 1:
    raise SystemExit(f"fact scene-context anchor count={text.count(fact_scope_old)}")
text = text.replace(fact_scope_old, fact_scope_new)

# Add narrow runtime patches without changing stable private parsing signatures.
text += '''
replace_once(
    "src/backend/app/services/memory_scribe.py",
    \'\'\'        return self._parse_data(
            data,
            authoritative_text=assistant_content,
\'\'\',
    \'\'\'        self._current_scene_id = scene_id
        return self._parse_data(
            data,
            authoritative_text=assistant_content,
\'\'\',
)

replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    \'\'\'        for name in phase.introduced_npcs:
            await self.ensure_npc(name, location.id)
        active = {name: self.characters[name] for name in phase.active_npcs}
\'\'\',
    \'\'\'        for name in dict.fromkeys((*phase.introduced_npcs, *phase.active_npcs)):
            await self.ensure_npc(name, location.id)
        active = {name: self.characters[name] for name in phase.active_npcs}
\'\'\',
)
'''

path.write_text(text, encoding="utf-8")
