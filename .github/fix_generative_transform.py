from pathlib import Path

path = Path(".github/generative_simulation_transform.py")
text = path.read_text(encoding="utf-8")

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

helper_anchor = '''    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:120]!r}")
'''
helper_replacement = '''    count = text.count(old)
    if (
        path.endswith("memory_scribe.py")
        and "player_character_id" in old
        and "scene_participant_ids" in old
        and count == 2
    ):
        target.write_text(text.replace(old, new), encoding="utf-8")
        return
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:120]!r}")
'''
if text.count(helper_anchor) != 1:
    raise SystemExit(f"replace_once helper anchor count={text.count(helper_anchor)}")
text = text.replace(helper_anchor, helper_replacement)

path.write_text(text, encoding="utf-8")
