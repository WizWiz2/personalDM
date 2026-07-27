from pathlib import Path
import sys
from uuid import uuid4

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from run_realistic_simulation_v2 import (
    PlayerDecision,
    PlayerPolicy,
    SimulationState,
    TraceStore,
    deterministic_fallback_card,
    eldon_card,
    parse_player_decision,
    validate_dm_player_agency,
    validate_russian_narrative,
)
from simulation_scenario import NPCS, PHASES


def test_scenario_introduces_all_npcs_gradually():
    introduced: set[str] = set()
    introduction_sizes = []
    for phase in PHASES:
        introduced.update(phase.introduced_npcs)
        introduction_sizes.append(len(introduced))
        assert set(phase.active_npcs).issubset(introduced)
        assert 2 <= len(phase.active_npcs) <= 5
        assert len(phase.pulses) >= 3
        assert len(phase.opening_theses) >= 3

    assert introduced == set(NPCS)
    assert introduction_sizes == sorted(introduction_sizes)
    assert introduction_sizes[0] < introduction_sizes[-1]


def test_eldon_card_matches_black_rain_campaign_premise():
    card_text = str(eldon_card().model_dump()).casefold()
    assert "чёрн" in card_text
    assert "цен" in card_text
    assert "реликтов" not in card_text
    assert "цитадел" not in card_text


def test_player_parser_canonicalizes_active_npc_name():
    decision = parse_player_decision(
        '{"target":"sylvia","mode":"question","intent":"Я спрашиваю о руне."}',
        ["Sylvia", "Garrick"],
    )
    assert decision.target == "Sylvia"
    assert decision.mode == "question"


def test_player_parser_routes_self_target_to_narrator():
    decision = parse_player_decision(
        '{"target":"Eldon","mode":"action","intent":"Я осматриваю мокрые следы."}',
        ["Сильвия", "Гаррик"],
    )
    assert decision.target == "narrator"
    assert decision.mode == "action"


def test_player_parser_routes_misspelled_self_question_to_active_npc():
    decision = parse_player_decision(
        '{"target":"Aldon","mode":"question","intent":"Что я замечаю в осадке?"}',
        ["Маркус", "Элина"],
    )
    assert decision.target == "Маркус"


def test_player_parser_routes_question_to_active_npc():
    decision = parse_player_decision(
        '{"target":"narrator","mode":"question","intent":"Какие риски вы видите?"}',
        ["Иван", "Альберт"],
    )
    assert decision.target == "Иван"


def test_russian_narrative_rejects_foreign_service_tail():
    valid, error = validate_russian_narrative(
        "Элдон осматривает пол. 以下是翻译内容，请确认是否符合预期。"
    )
    assert valid is False
    assert "non-Russian script" in str(error)


def test_russian_narrative_rejects_glued_words():
    valid, error = validate_russian_narrative(
        "Элдон действует согласно указаниямПод куском доски виден след."
    )
    assert valid is False
    assert error == "text contains glued Russian words"


def test_near_miss_filter_does_not_confuse_common_words_with_elias():
    valid, error = validate_dm_player_agency(
        "Леса вокруг молчат. Голос проводника слышен у тропы.",
        active_npcs=["Элиас Вест"],
    )
    assert valid is True
    assert error is None


def test_near_miss_filter_still_rejects_misspelled_elias():
    valid, error = validate_dm_player_agency(
        "Елиас Вест указывает на след.",
        active_npcs=["Элиас Вест"],
    )
    assert valid is False
    assert error == "DM uses a near-miss active NPC name"


def test_dm_agency_rejects_unsupported_retrospective_assertion():
    valid, error = validate_dm_player_agency(
        "Келвин кивает туда, где Инквизитор ранее указал на аномалию.",
        active_npcs=["Келвин", "Инквизитор Виктор"],
    )
    assert valid is False
    assert error == "DM makes an unsupported retrospective assertion"


def test_dm_agency_allows_prezhde_as_future_sequence():
    valid, error = validate_dm_player_agency(
        "Мы пойдём к склону, но прежде Виктор проверит тропу.",
        active_npcs=["Инквизитор Виктор"],
    )
    assert valid is True
    assert error is None


def test_player_policy_rejects_npc_surname_attached_to_eldon():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="Маргарита Грей",
        mode="question",
        intent="Элдон Вест спрашивает Маргариту Грей: «Что означает этот след?»",
    )
    valid, error = policy.validate(
        decision,
        ["Элиас Вест", "Маргарита Грей"],
    )
    assert valid is False
    assert error == "intent assigns an unsupported surname or title to Eldon"


def test_dm_agency_rejects_laboratory_inside_outdoor_settlement_scene():
    valid, error = validate_dm_player_agency(
        "Виктор указывает в сторону угла лаборатории.",
        "Окраина заброшенного поселения у лесной тропы.",
        ["Инквизитор Виктор"],
    )
    assert valid is False
    assert error == "DM contradicts the outdoor scene location"


def test_russian_narrative_rejects_self_contradictory_repetition():
    valid, error = validate_russian_narrative(
        "В её взгляде — не уверенность, а холодная уверенность."
    )
    assert valid is False
    assert error == "text contains a self-contradictory repeated phrase"


def test_dm_agency_allows_truthful_local_silence_reference():
    valid, error = validate_dm_player_agency(
        "Тарн, который до этого молчал, медленно выпрямляется у стены.",
        active_npcs=["Тарн"],
    )
    assert valid is True
    assert error is None


def test_dm_agency_allows_supported_retrospective_reference():
    valid, error = validate_dm_player_agency(
        "Тарн кладёт траву рядом с кустом, о котором говорил ранее.",
        active_npcs=["Тарн"],
        recent_history=(
            "Тарн указал на куст у края тропы и говорил о тёмной росе."
        ),
    )
    assert valid is True, error
    assert error is None


def test_dm_agency_rejects_science_fiction_genre_drift():
    valid, error = validate_dm_player_agency(
        "Под руинами включается древний плазменный реактор.",
        active_npcs=["Тарн"],
    )
    assert valid is False
    assert error == "DM introduces modern or science-fiction genre drift"


def test_dm_agency_rejects_undeclared_magic_on_ordinary_item():
    valid, error = validate_dm_player_agency(
        "Жезл «Шепот Падших» вспыхнул холодным красным светом.",
        active_npcs=["Верана Грим"],
    )
    assert valid is False
    assert error == "DM invents an undeclared magical item property"


def test_dm_agency_rejects_levitating_ordinary_herbs():
    valid, error = validate_dm_player_agency(
        (
            "Верана вынула из мешочка сухие травы и бросила их в воздух. "
            "Они не упали, а зависли над росой."
        ),
        active_npcs=["Верана Грим"],
    )
    assert valid is False
    assert error == "DM invents an undeclared magical item property"


def test_dm_agency_rejects_magic_air_reaction_around_ordinary_staff():
    valid, error = validate_dm_player_agency(
        "Её обычный посох касается земли, и воздух вокруг дрожит."
    )
    assert valid is False
    assert error == "DM invents an undeclared magical item property"


def test_dm_agency_rejects_player_character_narration():
    valid, error = validate_dm_player_agency(
        "Элдон осторожно идёт между развалинами и осматривает следы.",
        player_mode="dialogue",
    )
    assert valid is False
    assert error == "DM invents an unrequested physical action for Eldon"


def test_dm_agency_rejects_second_person_player_narration():
    valid, error = validate_dm_player_agency(
        "Вы идёте по следам, оставленным дождём.",
        player_mode="dialogue",
    )
    assert valid is False
    assert error == "DM narrates the player character"


def test_dm_agency_allows_second_person_inside_npc_direct_speech():
    valid, error = validate_dm_player_agency(
        "Тарн отвечает:\n\n— Вы станете частью гниения, если пойдёте дальше.",
        player_mode="dialogue",
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_player_emotion_in_gaze():
    valid, error = validate_dm_player_agency(
        "На кирпиче видны царапины. В его взгляде появляется новая уверенность."
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_dm_agency_rejects_incoherent_eating_simile():
    valid, error = validate_dm_player_agency(
        "Земля начнёт есть вас, как еду в котле."
    )
    assert valid is False
    assert error == "DM uses incoherent figurative language"


def test_dm_agency_rejects_modern_pseudoscientific_fantasy_jargon():
    valid, error = validate_dm_player_agency(
        "Это осаждение из перенасыщенного раствора у эпицентра экссудации."
    )
    assert valid is False
    assert error == "DM introduces modern or science-fiction genre drift"


def test_dm_agency_rejects_undeclared_npc_flask():
    valid, error = validate_dm_player_agency(
        "Верана достала из своих запасов стеклянную колбу и идёт к стене."
    )
    assert valid is False
    assert error == "DM invents an undeclared inventory item"


def test_dm_agency_rejects_undeclared_npc_mortar():
    valid, error = validate_dm_player_agency(
        "Верана достала из складок одежды медную ступку."
    )
    assert valid is False
    assert error == "DM invents an undeclared inventory item"


def test_dm_agency_rejects_replayed_diary_arrival():
    valid, error = validate_dm_player_agency(
        "Из окна снова вылетает кожаный дневник и падает в лужу.",
        recent_history="Ветер выбил из лавки кожаный дневник; он упал в чёрную лужу.",
    )
    assert valid is False
    assert error == "DM repeats an already resolved scene event"


def test_dm_agency_rejects_replayed_diary_called_a_book():
    valid, error = validate_dm_player_agency(
        "Из окна вылетела небольшая книжка и упала в лужу.",
        recent_history="Ветер выбил из лавки кожаный дневник; он упал в чёрную лужу.",
    )
    assert valid is False
    assert error == "DM repeats an already resolved scene event"


def test_dm_agency_rejects_verana_secret_leaked_by_tarn():
    valid, error = validate_dm_player_agency(
        "Тарн говорит: «Сначала люди теряют имена своих близких».",
        recent_history="Верана осмотрела чёрную росу.",
    )
    assert valid is False
    assert error == "DM leaks Verana's secret through the wrong NPC"


def test_dm_agency_rejects_verana_secret_leaked_as_names_washed_away():
    valid, error = validate_dm_player_agency(
        "Тарн говорит: «Сначала смоет ваши имена, затем — память о доме».",
        recent_history="Верана осмотрела чёрную росу.",
    )
    assert valid is False
    assert error == "DM leaks Verana's secret through the wrong NPC"


def test_dm_agency_rejects_tarn_secret_leak_when_verana_is_elsewhere():
    valid, error = validate_dm_player_agency(
        "Верана изучает росу.\n\nТарн кричит: «Дождь смоет наши имена!»",
    )
    assert valid is False
    assert error == "DM leaks Verana's secret through the wrong NPC"


def test_dm_agency_rejects_verana_memory_motif_leaked_by_tarn():
    valid, error = validate_dm_player_agency(
        "Тарн говорит: «Эта вода — память о том, что умерло без имени».",
    )
    assert valid is False
    assert error == "DM leaks Verana's secret through the wrong NPC"


def test_dm_agency_rejects_personal_cost_paid_before_finale():
    valid, error = validate_dm_player_agency(
        "Верана говорит: «Цена за это знание уже заплачена мной самой».",
    )
    assert valid is False
    assert error == "DM pays the personal cost before the terminal finale"


def test_dm_agency_allows_personal_cost_paid_in_terminal_finale():
    valid, error = validate_dm_player_agency(
        "Верана говорит: «Цена за это знание уже заплачена мной самой».",
        allow_paid_cost=True,
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_future_loss_of_veranas_own_name():
    valid, error = validate_dm_player_agency(
        "Верана говорит: «Я утрачу своё имя и лицо наставника».",
    )
    assert valid is False
    assert error == "DM distorts the defined personal cost"


def test_dm_agency_does_not_treat_later_npc_speech_as_talking_staff():
    valid, error = validate_dm_player_agency(
        "Верана сжала корневой посох. Когда она наконец заговорила, голос дрогнул.",
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_second_person_body_narration():
    valid, error = validate_dm_player_agency(
        "Ваши глаза выхватывают нишу рядом с вашим коленом.",
        player_mode="action",
    )
    assert valid is False
    assert error == "DM narrates the player character"


def test_dm_agency_allows_verana_to_reveal_her_own_cost():
    valid, error = validate_dm_player_agency(
        "Верана говорит: «Я навсегда потеряю последнее воспоминание о наставнике».",
        recent_history="Тарн молчал.",
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_turn_bookkeeping_prose():
    valid, error = validate_dm_player_agency(
        "После двенадцатого хода ситуация изменилась: в сцене появилось свидетельство."
    )
    assert valid is False
    assert error == "DM exposes turn bookkeeping in narrative prose"


def test_dm_agency_allows_idiom_ranshe_vremeni():
    valid, error = validate_dm_player_agency(
        "Стоять на месте — значит умереть раньше времени.",
        recent_history="",
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_forest_drift_from_settlement():
    valid, error = validate_dm_player_agency(
        "Тарн просит остановиться у края леса.",
        location_description="Улица полуразрушенного поселения между домами и лавками.",
    )
    assert valid is False
    assert error == "DM moves the settlement scene into undeclared wilderness"


def test_dm_agency_allows_oak_beam_in_settlement():
    valid, error = validate_dm_player_agency(
        "Балки, некогда бывшие крепким дубом, превратились в труху.",
        location_description="Улица полуразрушенного поселения между домами и лавками.",
    )
    assert valid is True
    assert error is None


def test_player_fallback_targets_diary_once_it_is_active():
    policy = PlayerPolicy()
    decision = policy.fallback(
        ["Верана Грим", "Тарн"],
        "action",
        "Извлечь дневник",
        "Дневник упал в чёрную лужу.",
        ["Кожаный дневник не гниёт в чёрной росе."],
        6,
    )
    assert decision.mode == "action"
    assert "вытянуть дневник" in decision.intent


def test_player_fallback_asks_verana_for_exact_personal_cost():
    policy = PlayerPolicy()
    decision = policy.fallback(
        ["Верана Грим", "Тарн"],
        "dialogue",
        "Понять цену",
        "На стене видна старая печать.",
        ["Печать связана с обрядом забвения."],
        8,
    )
    assert decision.target == "Верана Грим"
    assert decision.mode == "dialogue"
    assert "точную цену" in decision.intent


def test_player_fallback_prefers_verana_for_risk_dialogue():
    policy = PlayerPolicy()
    policy.target_counts["верана грим"] = 2
    decision = policy.fallback(
        ["Верана Грим", "Тарн"],
        "dialogue",
        "Понять источник дождя",
        "На мостовой виден чёрный осадок.",
        [],
        3,
    )
    assert decision.target == "Верана Грим"
    assert "точную будущую цену" in decision.intent


def test_dm_agency_rejects_deciding_the_next_step_for_player():
    valid, error = validate_dm_player_agency(
        "После слов Вераны их следующий шаг был определён.",
        active_npcs=["Верана Грим"],
    )
    assert valid is False
    assert error == "DM decides the player's next step"


def test_dm_agency_rejects_eldon_making_an_unprompted_conclusion():
    valid, error = validate_dm_player_agency(
        "Элдон осматривает лужу. Он замечает пузырьки и делает быстрый вывод.",
        active_npcs=["Элиас Вест"],
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_dm_agency_rejects_present_tense_direct_speech_for_eldon():
    valid, error = validate_dm_player_agency(
        (
            "Элдон осматривает следы у башни.\n\n"
            "«Нужно идти вверх», — говорит он сухо."
        ),
        active_npcs=["Верана Грим"],
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_rejects_dash_direct_speech_attributed_to_eldon():
    valid, error = validate_dm_player_agency(
        "— Осадок ведёт к кузнице, — произнёс Элдон.",
        player_mode="action",
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_rejects_physical_action_after_player_question():
    valid, error = validate_dm_player_agency(
        "Элдон, прислонившись к разрушенной статуе, кивает.",
        active_npcs=["Верана Грим"],
        player_mode="question",
    )
    assert valid is False
    assert error == "DM invents an unrequested physical action for Eldon"


def test_dm_agency_rejects_gerund_action_after_player_question():
    valid, error = validate_dm_player_agency(
        "Элдон же, кивнув, уже начинает двигаться вперёд.",
        active_npcs=["Верана Грим"],
        player_mode="question",
    )
    assert valid is False
    assert error == "DM invents an unrequested physical action for Eldon"


def test_dm_agency_rejects_any_started_activity_after_player_question():
    valid, error = validate_dm_player_agency(
        "Элдон молча начинает собирать образцы росы.",
        active_npcs=["Верана Грим"],
        player_mode="question",
    )
    assert valid is False
    assert error == "DM invents an unrequested physical action for Eldon"


def test_dm_agency_rejects_eldon_understanding_gerund():
    valid, error = validate_dm_player_agency(
        "Элдон осматривает символы и кивает, понимая их истинный смысл.",
        active_npcs=["Верана Грим"],
        player_mode="action",
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_dm_agency_rejects_eldon_quote_without_known_speech_verb():
    valid, error = validate_dm_player_agency(
        (
            "Элдон сравнивает два пятна росы.\n\n"
            "Он замер, слушая ветер. «Здесь сильнее», — пробормотал он."
        ),
        active_npcs=["Тарн"],
        player_mode="action",
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_rejects_inconsistent_player_name():
    valid, error = validate_dm_player_agency(
        "Верана поворачивается к Эльдону.",
        active_npcs=["Верана Грим"],
    )
    assert valid is False
    assert error == "DM uses an inconsistent player name"


def test_dm_agency_rejects_undeclared_analyzer_for_eldon():
    valid, error = validate_dm_player_agency(
        "Элдон кладёт образец в свой портативный анализатор среды.",
        active_npcs=["Элиас Вест"],
    )
    assert valid is False
    assert error == "DM introduces modern or science-fiction genre drift"


def test_dm_agency_rejects_declared_npc_analyzer_as_genre_drift():
    valid, error = validate_dm_player_agency(
        (
            "Элдон щурится, всматриваясь в серое марево.\n\n"
            "Элиас Вест перехватил свой анализатор среды; прибор тревожно запищал."
        ),
        active_npcs=["Элиас Вест"],
    )
    assert valid is False
    assert error == "DM introduces modern or science-fiction genre drift"


def test_dm_agency_rejects_consequences_report_heading():
    valid, error = validate_dm_player_agency(
        "Виктор указывает на склон.\n\n**Последствия:** группа меняет путь.",
        active_npcs=["Инквизитор Виктор"],
    )
    assert valid is False
    assert error == "DM exposes control-plane or benchmark prose"


def test_player_policy_rejects_undeclared_map():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="narrator",
        mode="action",
        intent="Я отмечаю на карте два направления и выбираю путь.",
    )
    valid, error = policy.validate(decision, ["Келвин"])
    assert valid is False
    assert error == "intent invents an undeclared inventory item"


def test_player_policy_rejects_invented_future_npc_reactions():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="narrator",
        mode="action",
        intent=(
            "Элдон изучает лица спутников. Он знает, что собеседники будут "
            "искать подтверждение его догадке."
        ),
    )
    valid, error = policy.validate(decision, ["Верана Грим", "Тарн"])
    assert valid is False
    assert error == "intent invents future NPC reactions"


def test_dm_agency_allows_npc_to_mention_eldon_then_speak():
    valid, error = validate_dm_player_agency(
        (
            "Элиас поморщился, когда Элдон сделал шаг вперёд.\n\n"
            "— Потрясающая стратегия, Элдон, — сухо сказал Элиас."
        ),
        active_npcs=["Элиас Вест"],
    )
    assert valid is True
    assert error is None


def test_dm_agency_allows_eldon_to_notice_an_observable_result():
    valid, error = validate_dm_player_agency(
        "Элдон заметил, что капли падают под углом к ветру.",
        active_npcs=["Элиас Вест"],
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_eldon_attitude_decided_by_dm():
    valid, error = validate_dm_player_agency(
        "Элдон игнорирует мистические рассуждения и смотрит на склон.",
        active_npcs=["Келвин"],
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_player_policy_rejects_invented_prior_contact_result():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="Келвин",
        mode="dialogue",
        intent=(
            "Я показываю Келвину осадок, оставшийся на руках "
            "после прикосновения к южному склону."
        ),
    )
    valid, error = policy.validate(decision, ["Келвин"])
    assert valid is False
    assert error == "intent invents an unsupported prior outcome"


def test_player_policy_rejects_undeclared_bandage():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="narrator",
        mode="action",
        intent="Я обтираю пятно фланелевой повязкой.",
    )
    valid, error = policy.validate(decision, ["Келвин"])
    assert valid is False
    assert error == "intent invents an undeclared inventory item"


def test_russian_narrative_rejects_mixed_script_name():
    valid, error = validate_russian_narrative(
        "Элиason, расскажи об алхимическом резонансе."
    )
    assert valid is False
    assert "mixed-script token" in str(error)


def test_russian_narrative_rejects_broken_repeated_word():
    valid, error = validate_russian_narrative(
        "Элдон направляет дорожный фона...фонарь на край поляны."
    )
    assert valid is False
    assert error == "text contains a broken repeated word"


def test_russian_narrative_rejects_broken_repeated_phrase():
    valid, error = validate_russian_narrative(
        "Как измерить этот духов...измерить этот духовный риск?"
    )
    assert valid is False
    assert error == "text contains a broken repeated phrase"


def test_russian_narrative_rejects_word_fragments_around_ellipsis():
    valid, error = validate_russian_narrative(
        "Его пальцы двигаются с необычной осторо ...на кристаллы."
    )
    assert valid is False
    assert error == "text contains a broken word around an ellipsis"


def test_russian_narrative_rejects_missing_sentence_boundary():
    valid, error = validate_russian_narrative(
        "Он опустился на колени, игнорируя сырость проникающую через ткань Он медленно поднялся."
    )
    assert valid is False
    assert error == "text contains a missing sentence boundary"


def test_dm_agency_rejects_invented_eldon_dialogue():
    valid, error = validate_dm_player_agency(
        "Элдон посмотрел на Киру. «Затруднилась ответить», — повторил он тихо."
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_rejects_quote_then_eldon_speech_tag():
    valid, error = validate_dm_player_agency(
        '"Риск духовный", — повторил Элдон, понижая голос.'
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_rejects_eldon_constatation_tag():
    valid, error = validate_dm_player_agency(
        "«Следов много, но они противоречат друг другу», — констатирует Элдон."
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_rejects_near_miss_active_npc_name():
    valid, error = validate_dm_player_agency(
        "Элдон переводит взгляд на Мэгэрет, ожидающую у края поляны.",
        "Окраина поселения после чёрного дождя.",
        ["Маргарита Грей", "Элиас Вест"],
    )
    assert valid is False
    assert error == "DM uses a near-miss active NPC name"


def test_dm_agency_rejects_control_plane_prose():
    valid, error = validate_dm_player_agency(
        "Твой запрос принят. **Конкретное последствие:** память меркнет. "
        "Напряжение остается высоким: ———."
    )
    assert valid is False
    assert error == "DM exposes control-plane or benchmark prose"


def test_dm_agency_rejects_player_placeholder():
    valid, error = validate_dm_player_agency(
        "Виктор останавливает взгляд на игроке и указывает за спину игрока."
    )
    assert valid is False
    assert error == "DM exposes control-plane or benchmark prose"


def test_dm_agency_rejects_second_person_interiority():
    valid, error = validate_dm_player_agency(
        "Ты касаешься кристалла. Когда ты концентрируешься, "
        "тебе кажется, что твой взгляд видит внутренний ритм."
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_dm_agency_rejects_numbered_report_prose():
    valid, error = validate_dm_player_agency(
        "Кристалл трескается.\n\n1. **Кира** вздрагивает.\n2. Элиас отступает."
    )
    assert valid is False
    assert error == "DM uses report-style numbered prose"


def test_dm_agency_rejects_indoor_location_during_field_scene():
    valid, error = validate_dm_player_agency(
        "Элиас окинул взглядом всё помещение и подошёл к кристаллу.",
        "Поляна вдали от лесной тропы, влажная земля пахнет озоном.",
    )
    assert valid is False
    assert error == "DM contradicts the outdoor scene location"


def test_dm_agency_rejects_outdoor_tent_windowsill():
    valid, error = validate_dm_player_agency(
        "Элиас опёрся рукой о подоконник временной палатки.",
        "Окраина поселения, влажная земля и открытое поле.",
    )
    assert valid is False
    assert error == "DM contradicts the outdoor scene location"


def test_dm_agency_rejects_dash_dialogue_continuing_eldon_subject():
    valid, error = validate_dm_player_agency(
        "Элдон заканчивает обход периметра.\n\n"
        "Он поворачивается к Кире и поднимает руку.\n\n"
        "— Кира, следи за лесом, — говорит он."
    )
    assert valid is False
    assert error == "DM invents direct speech for Eldon"


def test_dm_agency_allows_npc_dialogue_about_eldon():
    valid, error = validate_dm_player_agency(
        "Кира посмотрела на Элдона. «Нам нужен образец», — сказала она."
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_invented_eldon_interiority():
    valid, error = validate_dm_player_agency(
        "Элдон смотрит на кристаллы.\n\n"
        "Его взгляд выдаёт глубокое желание понять тайну; он как будто ждёт подтверждения."
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_dm_agency_rejects_eldon_attention_and_internal_monologue():
    valid, error = validate_dm_player_agency(
        "Элдон закончил измерения. Его внимание было приковано к двум точкам. "
        "Он не ждал ответа. *Я собрал всё, что можно измерить.*"
    )
    assert valid is False
    assert error == "DM invents Eldon's internal state"


def test_dm_agency_allows_npc_interiority_after_eldon():
    valid, error = validate_dm_player_agency(
        "Элдон кладёт кристалл на землю.\n\n"
        "Элиас думает о возможной природе образца и боится ошибиться."
    )
    assert valid is True
    assert error is None


def test_dm_agency_rejects_invented_inventory_item():
    valid, error = validate_dm_player_agency(
        "Элдон достаёт из инвентаря несколько толстых брёвен."
    )
    assert valid is False
    assert error == "DM invents an undeclared inventory item"


def test_player_policy_rejects_target_addressee_conflict():
    policy = PlayerPolicy()
    valid, error = policy.validate(
        PlayerDecision(
            target="Мастер Элиас Вейн",
            mode="question",
            intent="Элдон поворачивается к Каэтрин и задаёт ей вопрос о кристаллах.",
        ),
        ["Мастер Элиас Вейн", "Каэтрин"],
    )
    assert valid is False
    assert "but target is" in str(error)


def test_player_policy_rejects_inflected_addressee_conflict():
    policy = PlayerPolicy()
    valid, error = policy.validate(
        PlayerDecision(
            target="Мастер Элиас Вейн",
            mode="question",
            intent="Элдон поворачивается к Кире и спрашивает её об увядших цветах.",
        ),
        ["Мастер Элиас Вейн", "Кира"],
    )
    assert valid is False
    assert "but target is" in str(error)


def test_player_parser_repairs_schema_placeholder_target():
    decision = parse_player_decision(
        '{"target":"narrator|ActiveNpc","mode":"question","intent":"Что изменилось?"}',
        ["Иван", "Альберт"],
    )
    assert decision.target == "Иван"


def test_player_parser_resolves_transliterated_active_npc_slug():
    decision = parse_player_decision(
        '{"target":"master_elias_weyn","mode":"question","intent":"Элдон задаёт Элиасу вопрос о кристаллах."}',
        ["Мастер Элиас Вейн", "Кира"],
    )
    assert decision.target == "Мастер Элиас Вейн"


def test_player_policy_detects_russian_repetition():
    policy = PlayerPolicy()
    first = PlayerDecision(
        target="narrator",
        mode="action",
        intent="Я осматриваю крепления ворот и ищу следы недавнего износа.",
    )
    valid, _ = policy.validate(first, ["Sylvia"])
    assert valid is True
    policy.remember(first)

    valid, error = policy.validate(first, ["Sylvia"])
    assert valid is False
    assert "repeats" in error
    assert policy.repeated_actions == 1


def test_player_policy_rejects_russian_declared_outcome():
    policy = PlayerPolicy()
    outcome = PlayerDecision(
        target="narrator",
        mode="action",
        intent="Я успешно открываю ворота и обнаруживаю тайный проход.",
    )
    valid, error = policy.validate(outcome, ["Sylvia"])
    assert valid is False
    assert "outcome" in error
    assert policy.rejected_outcomes == 1


def test_player_policy_rejects_npc_as_acting_player():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="Элиас Вест",
        mode="question",
        intent=(
            "Элиас Вест задаёт Маргарите вопрос о природе чёрного дождя?"
        ),
    )
    valid, error = policy.validate(
        decision,
        ["Элиас Вест", "Маргарита Грей"],
    )
    assert valid is False
    assert error == "intent does not identify Eldon as the acting player"


def test_contextual_fallbacks_do_not_repeat():
    policy = PlayerPolicy()
    fingerprints = []
    for turn in range(1, 7):
        decision = policy.fallback(
            ["Sylvia", "Garrick"],
            policy.preferred_mode(turn),
            "Выбрать путь в цитадель.",
            "Гаррик заметил свежие следы у западного оврага.",
            ["Карта неполна.", "До рассвета остаётся мало времени."],
            turn,
        )
        valid, error = policy.validate(decision, ["Sylvia", "Garrick"])
        assert valid is True, error
        policy.remember(decision)
        fingerprints.append(policy.fingerprint(decision.intent))

    assert len(set(fingerprints)) == len(fingerprints)
    assert policy.fallbacks == 6


def test_fallback_respects_requested_mode():
    policy = PlayerPolicy()
    decision = policy.fallback(
        ["Кира", "Мастер Элиас Вейн"],
        "action",
        "Исследовать поляну.",
        "",
        [],
        1,
    )
    assert decision.mode == "action"
    assert decision.target == "narrator"


def test_player_policy_rejects_inactive_target():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="Thorin",
        mode="dialogue",
        intent="Я спрашиваю Торина о печати.",
    )
    valid, error = policy.validate(decision, ["Sylvia", "Garrick"])
    assert valid is False
    assert "not active" in error


def test_trace_store_upserts_logical_turn(tmp_path):
    path = tmp_path / "trace.jsonl"
    store = TraceStore(path)
    store.upsert({"turn": 4, "dm": "старый ответ"})
    store.upsert({"turn": 4, "dm": "исправленный ответ"})
    store.upsert({"turn": 5, "dm": "следующий ответ"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    restored = TraceStore(path)
    assert restored.records[4]["dm"] == "исправленный ответ"
    assert sorted(restored.records) == [4, 5]


def test_simulation_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = SimulationState(
        run_id="run-1",
        campaign_id=str(uuid4()),
        logical_turn=17,
        phase_index=2,
        phase_turn=5,
        injected_pulses=[0, 1],
        confirmed_pulses=[0],
    )
    state.save(path)

    restored = SimulationState.load(path)
    assert restored is not None
    assert restored.run_id == "run-1"
    assert restored.logical_turn == 17
    assert restored.injected_pulses == [0, 1]
    assert restored.confirmed_pulses == [0]


def test_fallback_cards_are_distinct_and_owner_specific():
    location_id = uuid4()
    sylvia = deterministic_fallback_card(NPCS["Sylvia"], location_id)
    garrick = deterministic_fallback_card(NPCS["Garrick"], location_id)

    assert sylvia.voice != garrick.voice
    assert sylvia.personality != garrick.personality
    assert sylvia.equipment != garrick.equipment
    assert all("Sylvia" in item for item in sylvia.equipment)
    assert all("Garrick" in item for item in garrick.equipment)
    assert sylvia.visual_profile["fallback"] is True
