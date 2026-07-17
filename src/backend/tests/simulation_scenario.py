from __future__ import annotations

from dataclasses import dataclass

from app.models.scene_thesis import ThesisType


@dataclass(frozen=True)
class NpcConcept:
    name: str
    concept: str
    campaign_role: str
    tone: str


@dataclass(frozen=True)
class ThesisSeed:
    thesis_type: ThesisType
    text: str
    priority: int = 5
    visibility: str = "public"
    related_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioPulse:
    at_fraction: float
    event: str
    thesis: ThesisSeed


@dataclass(frozen=True)
class ScenarioPhase:
    slug: str
    title: str
    location_description: str
    mood: str
    tension: str
    objective: str
    introduced_npcs: tuple[str, ...]
    active_npcs: tuple[str, ...]
    opening_theses: tuple[ThesisSeed, ...]
    pulses: tuple[ScenarioPulse, ...]
    director_note: str


NPCS: dict[str, NpcConcept] = {
    "Sylvia": NpcConcept("Sylvia", "Proud elven occult scholar who stole a forbidden Black-Star Codex", "first expert and morally ambiguous guide", "brilliant, defensive, archaic"),
    "Garrick": NpcConcept("Garrick", "Scarred rogue secretly contracted by the Red Syndicate to steer Eldon into an ambush", "road guide and possible betrayer", "dry, streetwise, watchful"),
    "Kaelen": NpcConcept("Kaelen", "Forest ranger hunting a werewolf who is secretly his younger brother", "wilderness guide with divided loyalty", "quiet, concrete, observant"),
    "Valerius": NpcConcept("Valerius", "Merciful cleric whose famous restoration chalice actually contains slow poison", "healer carrying a dangerous secret", "gentle, formal, doubtful"),
    "Aria": NpcConcept("Aria", "Dawn Order paladin whose holy sword lost its blessing", "principled commander facing public failure", "commanding, rigid, honourable"),
    "Korgan": NpcConcept("Korgan", "Heavy mercenary hired to kill Eldon's family after the expedition", "protector with a delayed assassination contract", "blunt, fearless-looking, transactional"),
    "Eldrin": NpcConcept("Eldrin", "Elder historian who secretly burned the Imperial Library", "translator and guilty keeper of lost history", "learned, distracted, evasive"),
    "Daphne": NpcConcept("Daphne", "Druid carrying a corrupted seed that is poisoning her forest", "nature expert linked to the citadel corruption", "gentle, severe about destruction"),
    "Zephyr": NpcConcept("Zephyr", "Masked assassin carrying the dagger used to kill the King", "infiltrator tied to royal murder", "controlled, precise, sparse"),
    "Rowan": NpcConcept("Rowan", "Nervous alchemist seeking the philosopher's stone to cure his terminal curse", "evidence-driven experimenter under time pressure", "analytical, anxious, careful"),
    "Vesper": NpcConcept("Vesper", "Shadow monk whose monastery worships the forbidden Black Sun", "expert on the prison cult with divided faith", "calm, paradoxical, disciplined"),
    "Seraphina": NpcConcept("Seraphina", "Young acolyte who hears intelligible Void whispers during prayer", "vulnerable witness and possible host", "timid, empathetic, stubborn"),
    "Gideon": NpcConcept("Gideon", "Sea captain who abandoned his crew during the Great Storm", "flood-navigation expert seeking redemption", "jovial, practical, nautical"),
    "Tariq": NpcConcept("Tariq", "Desert scout hiding that the party's only oasis has dried up", "resource and route specialist", "patient, economical, precise"),
    "Morgana": NpcConcept("Morgana", "Swamp healer whose potions use forbidden demonic essence", "curse expert with effective but costly methods", "cynical, warm, riddling"),
    "Brog": NpcConcept("Brog", "Orc warrior seeking vengeance with evidence that also implicates innocents", "physical protector facing a moral test", "hot-tempered, direct, loyal"),
    "Thorin": NpcConcept("Thorin", "Dwarf smith who lost his clan's royal seal through negligence", "forge expert and keeper of ancestral authority", "gruff, honour-bound, craft-minded"),
    "Isabella": NpcConcept("Isabella", "Noble duelist fleeing a marriage to the corrupt Duke financing the Syndicate", "diplomat tied to the conspiracy", "proud, polished, politically alert"),
    "Lyra": NpcConcept("Lyra", "Bard whose lute automatically charms listeners", "resonance expert afraid of manipulating friends", "charming, curious, conflict-averse"),
    "Ignis": NpcConcept("Ignis", "Fire sorcerer who accidentally burned his home village", "unstable power source seeking redemption", "excitable, insecure, eager"),
}


def T(kind: ThesisType, text: str, priority: int = 5, visibility: str = "public", *names: str) -> ThesisSeed:
    return ThesisSeed(kind, text, priority, visibility, tuple(names))


def P(at: float, event: str, thesis: ThesisSeed) -> ScenarioPulse:
    return ScenarioPulse(at, event, thesis)


PHASES: tuple[ScenarioPhase, ...] = (
    ScenarioPhase(
        "rain_bitten_inn", "The Rain-Bitten Inn",
        "A roadside inn rattling under black rain, crowded but watchful.",
        "uneasy shelter", "personal distrust",
        "Obtain a credible route and reason to enter the Obsidian Citadel.",
        ("Sylvia", "Garrick"), ("Sylvia", "Garrick"),
        (T(ThesisType.INTENTION, "Eldon needs a route into the citadel before dawn.", 9), T(ThesisType.TENSION, "Sylvia and Garrick distrust each other's motives.", 7, "public", "Sylvia", "Garrick"), T(ThesisType.UNRESOLVED_BEAT, "The promised courier has not delivered the route map.", 8)),
        (P(.22, "A wounded courier brings half of a citadel route map.", T(ThesisType.UNRESOLVED_BEAT, "Riders on the eastern road took the map's second half.", 8)), P(.52, "A Red Syndicate watcher appears outside.", T(ThesisType.TENSION, "Someone is monitoring Garrick's choices.", 8, "dm", "Garrick")), P(.78, "The river begins to cut both roads.", T(ThesisType.INTENTION, "The group must commit to a route before dawn.", 10))),
        "Finish with a committed route, not complete trust.",
    ),
    ScenarioPhase(
        "blackwood_road", "Blackwood Road",
        "A drowned forest road where hoofprints vanish beneath roots and rainwater.",
        "watchful movement", "predatory",
        "Cross Blackwood and determine who is stalking the party.",
        ("Kaelen", "Valerius"), ("Sylvia", "Garrick", "Kaelen", "Valerius"),
        (T(ThesisType.INTENTION, "Avoid the Syndicate's prepared route through Blackwood.", 9), T(ThesisType.UNRESOLVED_BEAT, "Kaelen tracks prints that become wolf-like.", 7, "public", "Kaelen"), T(ThesisType.SECRET, "Valerius will not allow anyone to drink from his chalice.", 7, "dm", "Valerius")),
        (P(.2, "The party finds a camp abandoned moments earlier.", T(ThesisType.VISUAL_STATE, "Warm ash, cut rope and one bloody boot mark the camp.", 6)), P(.5, "A trapped traveller calls from a suspicious clearing.", T(ThesisType.TENSION, "The rescue may be bait, but the victim is real.", 9)), P(.8, "A werewolf drives off Syndicate scouts and remains nearby.", T(ThesisType.SECRET, "Kaelen recognises the werewolf and hides it.", 9, "dm", "Kaelen"))),
        "Reward observation and social choices; do not reveal Kaelen's secret yet.",
    ),
    ScenarioPhase(
        "ruined_chapel", "The Ruined Dawn Chapel",
        "A roofless chapel where rain crosses cracked mosaics and old blood.",
        "sacred unease", "moral conflict",
        "Open the reliquary passage without desecrating the chapel.",
        ("Aria", "Korgan"), ("Valerius", "Aria", "Korgan", "Kaelen"),
        (T(ThesisType.INTENTION, "The reliquary passage is the safest route to the outer gate.", 9), T(ThesisType.RELATIONSHIP_DYNAMIC, "Aria expects Valerius to prove the relics remain holy.", 7, "public", "Aria", "Valerius"), T(ThesisType.SECRET, "Korgan is ordered to remain close to Eldon.", 8, "dm", "Korgan")),
        (P(.2, "The reliquary responds to an oath rather than a key.", T(ThesisType.INTENTION, "Someone must make a binding promise to open the passage.", 9)), P(.52, "A survivor shows symptoms of nightshade poisoning.", T(ThesisType.TENSION, "Valerius recognises the poison and fears the chalice is involved.", 9, "dm", "Valerius")), P(.8, "Syndicate soldiers arrive demanding Garrick and the map.", T(ThesisType.TENSION, "The confrontation can only be shaped, not avoided.", 10))),
        "Force a moral commitment and a consequence, not another endless door puzzle.",
    ),
    ScenarioPhase(
        "outer_gate", "The Citadel's Outer Gate",
        "A black gate embedded in a cliff, ringed by dead roots and star-shaped sockets.",
        "awe under pressure", "ritual danger",
        "Enter the citadel and learn what the gate was built to contain.",
        ("Eldrin", "Daphne"), ("Sylvia", "Aria", "Eldrin", "Daphne"),
        (T(ThesisType.INTENTION, "Open the gate without feeding it uncontrolled magic.", 9), T(ThesisType.SECRET, "Eldrin omits one line of the warning.", 8, "dm", "Eldrin"), T(ThesisType.VISUAL_STATE, "Dead roots twitch when Daphne approaches.", 7, "public", "Daphne")),
        (P(.22, "The gate reveals three entry prices: blood, memory or starlight.", T(ThesisType.INTENTION, "Choose an entry price with a real cost.", 10)), P(.5, "Daphne's seed reacts to something alive behind the gate.", T(ThesisType.SECRET, "The seed and the imprisoned corruption are related.", 8, "dm", "Daphne")), P(.78, "The gate begins closing permanently.", T(ThesisType.TENSION, "The party has minutes to commit.", 10))),
        "Offer meaningful costs and make entry happen before phase end.",
    ),
    ScenarioPhase(
        "sealed_archive", "The Sealed Archive",
        "A circular archive of black shelves and mechanisms that imitate breathing.",
        "scholarly dread", "quiet surveillance",
        "Recover the index identifying the relic key and its last bearer.",
        ("Zephyr", "Rowan"), ("Garrick", "Eldrin", "Zephyr", "Rowan"),
        (T(ThesisType.INTENTION, "Find the index rather than plunder every secret.", 9), T(ThesisType.TENSION, "Zephyr and Garrick recognise the same criminal signs.", 8, "public", "Zephyr", "Garrick"), T(ThesisType.SECRET, "Rowan's curse worsens near the catalogue engine.", 8, "dm", "Rowan")),
        (P(.2, "The archive offers one answer for one surrendered memory.", T(ThesisType.INTENTION, "Someone must choose which memory the archive may take.", 9)), P(.5, "A regicide record names the weapon but not the killer.", T(ThesisType.SECRET, "Zephyr carries the described weapon.", 9, "dm", "Zephyr")), P(.8, "The index places the relic key in the flooded vaults.", T(ThesisType.UNRESOLVED_BEAT, "The route crosses a failing tide engine.", 9))),
        "Allow one costly discovery and one character revelation; keep moving.",
    ),
    ScenarioPhase(
        "black_sun_cloister", "The Black Sun Cloister",
        "A silent cloister whose mosaics show a sun darker than night.",
        "contained terror", "spiritual intrusion",
        "Cross without allowing the sealed intelligence to recruit a host.",
        ("Vesper", "Seraphina"), ("Vesper", "Seraphina", "Sylvia", "Aria"),
        (T(ThesisType.INTENTION, "Cross without answering the voice behind the mosaics.", 10), T(ThesisType.SECRET, "Vesper understands the forbidden prayers.", 8, "dm", "Vesper"), T(ThesisType.TENSION, "The voice uses memories Seraphina never shared.", 9, "public", "Seraphina")),
        (P(.2, "The voice offers Seraphina relief if she opens one door.", T(ThesisType.TENSION, "Seraphina is tempted by a credible promise of silence.", 9, "character_only", "Seraphina")), P(.5, "Vesper knows a ritual that would expose his order.", T(ThesisType.INTENTION, "Vesper must choose between secrecy and Seraphina.", 9, "dm", "Vesper")), P(.8, "The mosaics address each traveller separately.", T(ThesisType.TENSION, "Everyone hears a different offer and must coordinate.", 10))),
        "Test knowledge isolation with different private offers.",
    ),
    ScenarioPhase(
        "flooded_vault", "The Flooded Vaults",
        "Stone galleries half-filled with black water around an ancient tide engine.",
        "urgent navigation", "rising water",
        "Restart or bypass the tide engine and reach the lower vault.",
        ("Gideon", "Tariq"), ("Gideon", "Tariq", "Kaelen", "Rowan"),
        (T(ThesisType.INTENTION, "Reach the lower vault before the galleries flood.", 10), T(ThesisType.RELATIONSHIP_DYNAMIC, "Gideon and Tariq disagree about the current.", 7, "public", "Gideon", "Tariq"), T(ThesisType.UNRESOLVED_BEAT, "The engine can save the vault or the settlements, not both.", 9)),
        (P(.2, "A maintenance crew is trapped behind a pressure door.", T(ThesisType.TENSION, "Rescue costs time and changes the safe route.", 9)), P(.5, "Tariq confirms the reserve cistern is contaminated.", T(ThesisType.UNRESOLVED_BEAT, "The region needs a new water source.", 8, "public", "Tariq")), P(.8, "The engine begins a one-way emergency cycle.", T(ThesisType.INTENTION, "Choose what the emergency cycle protects.", 10))),
        "Give navigation, rescue and resource choices; no single magic lever.",
    ),
    ScenarioPhase(
        "red_ledger_vault", "The Red Ledger Vault",
        "A dry counting chamber filled with sealed contracts and bone tokens.",
        "accusatory discovery", "alliances breaking",
        "Find who financed the expedition and choose what evidence survives.",
        ("Morgana", "Brog"), ("Morgana", "Brog", "Valerius", "Korgan"),
        (T(ThesisType.INTENTION, "Recover the Syndicate ledger before its ink dies.", 9), T(ThesisType.TENSION, "Brog believes it names his chieftain's killers.", 8, "public", "Brog"), T(ThesisType.SECRET, "Korgan's contract is in the ledger.", 9, "dm", "Korgan")),
        (P(.2, "The ledger links the Duke, Syndicate and a Dawn quartermaster.", T(ThesisType.TENSION, "The evidence harms Isabella's family and Aria's Order.", 9)), P(.5, "A curse erases names one by one.", T(ThesisType.INTENTION, "Only a few entries can be preserved.", 10)), P(.8, "Korgan receives an order to kill Eldon now.", T(ThesisType.TENSION, "Korgan must choose contract or companions.", 10, "dm", "Korgan"))),
        "Expose one long-running secret and force relationship consequences.",
    ),
    ScenarioPhase(
        "dragon_seal_forge", "The Dragon-Seal Forge",
        "A dead forge beneath a nest chamber, surrounded by treaty tablets.",
        "ancestral pressure", "political and physical",
        "Repair the relic key without waking the imprisoned dragon.",
        ("Thorin", "Isabella"), ("Thorin", "Isabella", "Eldrin", "Aria"),
        (T(ThesisType.INTENTION, "Repair the relic key in the dead forge.", 10), T(ThesisType.UNRESOLVED_BEAT, "The missing dwarf seal authorises the forge.", 8, "public", "Thorin"), T(ThesisType.TENSION, "The tablets implicate Isabella's intended husband.", 8, "public", "Isabella")),
        (P(.2, "The clan seal is found among offerings in the dragon nest.", T(ThesisType.TENSION, "Recovering it risks waking the dragon.", 9, "public", "Thorin")), P(.5, "The forge must consume a sworn symbol of authority.", T(ThesisType.INTENTION, "Thorin or Isabella must sacrifice a defining symbol.", 10, "public", "Thorin", "Isabella")), P(.8, "The dragon wakes as the forge reaches full heat.", T(ThesisType.TENSION, "Complete the key, negotiate or flee.", 10))),
        "Resolve the seal and treaty threads; the repaired key must exist.",
    ),
    ScenarioPhase(
        "heart_of_citadel", "The Heart of the Citadel",
        "A mechanism surrounds a suspended black star, humming in incompatible rhythms.",
        "final convergence", "catastrophic choice",
        "Use the relic key and decide whether the citadel is sealed, redirected or destroyed.",
        ("Lyra", "Ignis"), ("Lyra", "Ignis", "Sylvia", "Zephyr", "Vesper"),
        (T(ThesisType.INTENTION, "Every use of the key has a regional cost.", 10), T(ThesisType.MUSIC_MOOD, "Several rhythms compete under the mechanism's hum.", 6), T(ThesisType.TENSION, "Ignis can power the channel and Lyra can stabilise it, but neither is fully controlled.", 9, "public", "Ignis", "Lyra")),
        (P(.2, "The key reveals three irreversible outcomes.", T(ThesisType.INTENTION, "Gather enough truth to choose among reseal, redirect or collapse.", 10)), P(.5, "The Black Star offers private bargains to unresolved companions.", T(ThesisType.TENSION, "Unresolved loyalties threaten ritual stability.", 10)), P(.8, "The mechanism passes the point where inaction is safe.", T(ThesisType.INTENTION, "A final choice and sacrifice are required now.", 10))),
        "Conclude with a decision, sacrifice and aftermath; no new corridor.",
    ),
)


def phase_index_for_turn(turn_number: int, total_turns: int) -> int:
    if turn_number <= 0 or total_turns <= 0:
        raise ValueError("turn_number and total_turns must be positive")
    return min(len(PHASES) - 1, ((turn_number - 1) * len(PHASES)) // total_turns)


def phase_progress(turn_number: int, total_turns: int, phase_index: int) -> float:
    start = (phase_index * total_turns) // len(PHASES) + 1
    end = max(start, ((phase_index + 1) * total_turns) // len(PHASES))
    return min(1.0, max(0.0, (turn_number - start) / max(1, end - start)))