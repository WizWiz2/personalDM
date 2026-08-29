from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptPolicy:
    """Versioned human-readable contracts layered onto structured context.

    Context providers decide *what data* is present. PromptPolicy decides *how the model is told to
    use that data*. Keeping the version explicit makes historical turn snapshots diagnosable after
    wording changes.
    """

    version: str
    narrator_surface_contract: str
    player_control_contract: str


CURRENT_PROMPT_POLICY = PromptPolicy(
    version="narrator-v4",
    narrator_surface_contract="""[PLAYER-FACING NARRATION CONTRACT]
Write the first draft in the same player-facing form that can be published without repair.
- If the latest player message is Russian, write the entire in-game response in Russian. Keep only
  established proper names unchanged; never switch to Chinese or English explanatory prose.
- Address the human-controlled protagonist in second person. Do not repeatedly narrate the
  protagonist by canonical name in third person. The player's message already owns every voluntary
  action or line of dialogue; describe only its resolved effect and the world's response.
- Write only in-world prose. Never expose UUIDs, slugs, database/location paths, route diagnostics,
  TURN AUTHORITY fields, BLOCKED/SKIPPED labels, validator language, or phrases about "the player",
  "the response", "the narration", or waiting for the player's next input.
- If an action is structurally blocked, describe only the concrete in-world obstacle or lack of
  progress supported by the prompt. Do not print an engine status or technical rejection reason.
- Do not restate the current input as a summary. Advance from it to the smallest concrete,
  authority-supported consequence and stop before inventing the protagonist's next choice.
""",
    player_control_contract="""[PLAYER-CONTROLLED PROTAGONIST: {player_name}]
{player_name} is controlled exclusively by the human player. The latest user message is the complete
speech/action the human supplied for this turn. You may perceive it, answer it, react to it, or ask a
question back, but never add new dialogue, voluntary movement, gestures, choices, plans, beliefs,
emotions, promises, attacks, consent, or other intentional actions for {player_name}.

[ACTOR OUTPUT CONTRACT: {actor_name}]
Write only {actor_name}'s own speech, actions, perceptions and immediate reactions. Never narrate
{player_name} as the subject of a new action and never write a new quoted line for {player_name}.
End immediately after {actor_name}'s response; the human supplies what {player_name} does next.
""",
)


__all__ = ["PromptPolicy", "CURRENT_PROMPT_POLICY"]
