from app.services.script_consistency import consistent_script, foreign_script_intrusions


def test_a_mixed_script_word_takes_the_cast_or_text_spelling() -> None:
    assert consistent_script("Eгор молчит.", ["Егор"]) == "Егор молчит."
    assert consistent_script("К Eгору. Егору холодно.") == "К Егору. Егору холодно."
    assert consistent_script("Wi-Fi, café, Pетров") == "Wi-Fi, café, Pетров"


def test_foreign_script_garbage_is_reported_not_rewritten() -> None:
    """Playtest 2026-10-12: whole CJK/Hangul noise words must be surfaced for repair."""
    assert foreign_script_intrusions("Фонарь качается. 面 文字 글.") == sorted(
        ["글", "面", "文字"]
    )
    assert foreign_script_intrusions("Обычная русская проза with Latin.") == []
    # The repair vocabulary path stays untouched: intrusions never rewrite the text.
    assert consistent_script("Фонарь качается. 面 文字.", []) == "Фонарь качается. 面 文字."


def test_unassigned_code_points_never_reach_prose():
    assert consistent_script("протирать посуду.\U0005f7d9") == "протирать посуду."
