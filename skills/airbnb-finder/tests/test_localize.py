from src import localize as lz


def test_convert_text_euro_amounts():
    t = "Цена 360 € почти как у шале (€354), а №2 стоит 1,250 евро. Рейтинг 4.9 не трогать."
    out = lz.convert_text(t, "EUR", 3.431, lz.money_fmt("ILS", "ru"), "ru")
    assert "1\u202f235\u00a0₪" in out and "1\u202f215\u00a0₪" in out and "4\u202f289\u00a0₪" in out
    assert "€" not in out and "евро" not in out and "4.9" in out


def test_needs_translation():
    assert lz.needs_translation("Hair dryer", "ru")
    assert lz.needs_translation("Калуђерске баре", "ru")          # Serbian Cyrillic → Russian
    assert lz.needs_translation("Берите «Log Cabin Luna»: всё отлично, но есть нюанс с дорогой и соседями", "ru")
    assert not lz.needs_translation("Тихая двушка у парка, но без лифта", "ru")
    assert not lz.needs_translation("4.96", "ru")


def test_caption_text_shapes():
    from src.step3_photos import caption_text
    assert caption_text("View") == "View"
    assert caption_text({"__typename": "MediaText", "user": {"localizedStringWithTranslationPreference": "Shadows linger"}}) == "Shadows linger"
    assert caption_text(None) is None and caption_text("  ") is None


def test_needs_translation_latin_inside_russian():
    assert lz.needs_translation("По OSM 5 мест в 10 мин", "ru")
    assert lz.needs_translation("Правило «No pets», pets_allowed=false", "ru")


def test_needs_translation_one_latin_word_in_long_russian_text():
    s = "По главному критерию — лучший после Luna: кабина над озером с видом, других объявлений рядом нет"
    assert lz.needs_translation(s, "ru")
    assert not lz.needs_translation("Отдельная кабина, 0 объявлений Airbnb в 300 м", "ru")


def test_needs_translation_other_languages():
    assert not lz.needs_translation("Quiet flat near the park", "en")
    assert lz.needs_translation("Calçada íngreme até à casa", "en")      # accented Latin: not English
    assert lz.needs_translation("Тихая квартира", "en")                  # other script
    assert lz.needs_translation("Quiet flat", "he")
    assert lz.needs_translation("Quiet flat", "de")                      # same script: the model decides
    assert not lz.needs_translation("4.96 · 12", "de")


def test_convert_text_dollars_in_english():
    out = lz.convert_text("About $1,200 in total, or 300 dollars a night.", "USD", 0.9, lz.money_fmt("EUR", "en"), "en")
    assert "€1,080" in out and "€270" in out and "$" not in out


def test_annotated_quotes_are_translated_even_if_they_look_english(tmp_path, monkeypatch):
    seen = []
    def fake_call(prompt, schema, **kw):
        lines = [l for l in prompt.splitlines() if l.startswith('{"i"')]
        seen.extend(lines)
        import json as j
        return {"items": [{"i": j.loads(l)["i"], "t": "(2026) The access road is fine to drive."} for l in lines]}
    monkeypatch.setattr(lz, "call", fake_call)
    q = "(2026) Der Zufahrtsweg ist gut befahrbar."
    data = {"overview": "", "anchors": [], "places": [], "criteria": [],
            "listings": [{"criteria": {"road": {"note": "Fine road", "quotes": [{"text": q + " [Дорога хорошая]"}]}}}]}
    out = lz._translate_all(data, {}, tmp_path, "en")
    qq = out["listings"][0]["criteria"]["road"]["quotes"][0]
    assert qq["text"] == "(2026) The access road is fine to drive." and qq["orig"] == q
    assert not any("Fine road" in l for l in seen)          # plain English note is left alone
