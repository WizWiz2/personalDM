from app.services.script_consistency import consistent_script


def test_a_mixed_script_word_takes_the_cast_or_text_spelling() -> None:
    assert consistent_script("Eгор молчит.", ["Егор"]) == "Егор молчит."
    assert consistent_script("К Eгору. Егору холодно.") == "К Егору. Егору холодно."
    assert consistent_script("Wi-Fi, café, Pетров") == "Wi-Fi, café, Pетров"
