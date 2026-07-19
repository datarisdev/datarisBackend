from app.services.telemetry.aerial_copilot import _compact_prose


def test_compact_prose_limits_sentence_count_and_normalizes_spacing():
    result = _compact_prose(
        "Primera conclusión.   Segunda conclusión. Tercera conclusión que no debe mostrarse.",
        max_sentences=2,
        max_chars=200,
    )

    assert result == "Primera conclusión. Segunda conclusión."


def test_compact_prose_truncates_at_word_boundary():
    result = _compact_prose(
        "Una recomendación ejecutiva deliberadamente extensa para validar el límite configurado.",
        max_sentences=1,
        max_chars=45,
    )

    assert result.endswith("…")
    assert len(result) <= 45
    assert not result.endswith(" …")
