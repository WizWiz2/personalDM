from uuid import UUID

from app.models.turn import ChatMessage
from app.services.prompt_policy import CURRENT_PROMPT_POLICY

NARRATOR_EXECUTION_BOUNDARY = (
    "\nYou are the omniscient narrator. Respect every character card, "
    "capability, limitation, owned item and current location. Resolve the "
    "player's attempted action with a concrete consequence. Do not invent a "
    "successful ability, item or movement absent from structured state.\n"
    "Контракт прогрессии: прямо разреши текущую попытку; покажи хотя бы одно "
    "наблюдаемое последствие, новую конкретную информацию или изменившееся "
    "препятствие; закончи ситуацией, на которую игрок может осмысленно "
    "ответить. Не пересказывай уже установленное и не управляй героем игрока."
)

SCENE_RENDERER_RULES = (
    "Hard rules: characters absent from the physically-present list are not in "
    "the scene. Do not move anyone, advance time, or use an unlisted exit unless "
    "the approved structured transition already did so. Do not invent doors, "
    "routes, objects, or off-screen arrivals as accomplished facts.\n"
    "Текущее место героя — только Location path. Не возвращай тело на более "
    "раннюю точку и не объявляй новое место, если structured transition этого "
    "не сделал. Если игрок спрашивает одно значение, назови ровно одно или "
    "скажи, что разобрать нельзя. Не перечисляй варианты.\n"
)


def planning_context(messages: list[ChatMessage]) -> str:
    """Keep campaign evidence without the renderer's conflicting prose-only contract.

    The same compiled context is used before and after planning. Instructions such as
    'never expose UUIDs' apply to published fiction, but inventory planning requires UUIDs.
    """
    if not messages:
        return ""
    context = messages[0].content
    for renderer_policy in (
        CURRENT_PROMPT_POLICY.narrator_surface_contract,
        NARRATOR_EXECUTION_BOUNDARY,
        SCENE_RENDERER_RULES,
    ):
        context = context.replace(renderer_policy, "")
    # Persona is explicitly style-only. Feeding it to outcome resolution both wastes the
    # control window and lets a severe master's voice masquerade as an in-world obstacle.
    marker = "[GAME MASTER PERSONA — style only, not world facts]"
    start = context.find(marker)
    if start >= 0:
        end = context.find("\n[", start + len(marker))
        context = context[:start] + (context[end:] if end >= 0 else "")
    return context


def intent_reference_context(messages: list[ChatMessage]) -> str:
    """Intent extraction needs referents, not scene goals, obstacles or narrator policy.

    Feeding world-resolution instructions to this phase causes attempted moves to be dropped or
    reclassified as interactions before the route compiler can evaluate them.
    """
    context = planning_context(messages)
    prefixes = (
        "Location path:",
        "World time:",
        "Physically present characters:",
        "Objects physically here:",
        "Player-owned items:",
        "Controlled character:",
    )
    lines = [line for line in context.splitlines() if line.startswith(prefixes)]
    reference = "\n".join(lines) if lines else context
    # Scene occupancy is not a catalog of every narrated landmark. Keep recent
    # published referents so deixis and local approaches can be resolved without
    # inventing a location for a person, portable object or directional clue.
    published = [message.content for message in messages[1:] if message.role == "assistant"]
    if published:
        reference += "\n[RECENT PUBLISHED REFERENTS — not new player authority]\n" + "\n\n".join(
            published[-3:]
        )
    return reference


def action_reference_catalog(messages: list[ChatMessage], prefix: str) -> dict[str, str]:
    """Read UUID/name pairs from engine-owned reference lines, never from player prose."""
    references: dict[str, str] = {}
    for message in messages:
        for line in message.content.splitlines():
            if not line.startswith(prefix):
                continue
            for entry in line[len(prefix):].split(","):
                name, marker, suffix = entry.strip().rpartition(" [id=")
                if not marker or not suffix.endswith("]"):
                    continue
                try:
                    reference = str(UUID(suffix[:-1]))
                except ValueError:
                    continue
                references[reference] = name
    return references


def outcome_reference_context(messages: list[ChatMessage]) -> str:
    """World evidence without transient texture or duplicated execution instructions.

    These are compiler-owned section boundaries, not lexical classification of player prose.
    Campaign agreements, character cards, scene facts and exact entity IDs remain evidence.
    """
    context = planning_context(messages)
    for marker in ("[Recent Scene Texture — transient, non-canon]", "[Progress Watchdog]"):
        start = context.find(marker)
        if start >= 0:
            end = context.find("\n[", start + len(marker))
            context = context[:start] + (context[end:] if end >= 0 else "")
    marker = "Planner inventory contract:\n"
    start = context.find(marker)
    if start >= 0:
        end = context.find("\n[", start + len(marker))
        context = context[:start] + (context[end:] if end >= 0 else "")
    return "\n".join(line for line in context.splitlines()
                     if not line.startswith("Style instructions:")).strip()
