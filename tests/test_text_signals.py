"""Тесты текстовых сигналов: увод общения с площадки, риелторские фразы."""

from __future__ import annotations

from rentradar.text_signals import looks_like_agent, solicits_offplatform


def test_offplatform_telegram_and_obfuscation() -> None:
    assert solicits_offplatform("Пишите собственнику в тг") is True
    assert solicits_offplatform("напишите в telegram") is True
    # обфускация латиницей: «тлк» вместо «тгк», смешанные буквы
    assert solicits_offplatform("Пишите соBСтвeннику в тлк") is True
    assert solicits_offplatform("вотсап 24/7") is True


def test_offplatform_username_and_phone() -> None:
    assert solicits_offplatform("контакт @nata_777") is True
    assert solicits_offplatform("звоните 8 916 123 45 67") is True


def test_offplatform_clean_description_passes() -> None:
    assert solicits_offplatform("Уютная квартира от собственника, без комиссии") is False
    assert solicits_offplatform("Сдаётся 2-комн, метро рядом, свежий ремонт") is False
    assert solicits_offplatform(None) is False


def test_agent_markers() -> None:
    assert looks_like_agent("Большая база квартир, поможем подобрать") is True
    assert looks_like_agent("Сдаётся квартира собственником") is False


def test_agent_detection_deobfuscates_latin() -> None:
    # Авито-описания обфусцированы латиницей — «бaза квapтир» (a, p латинские).
    from rentradar.text_signals import looks_like_agent

    assert looks_like_agent("У нас бoльшaя бaза квapтир, подберем вариант") is True
    assert looks_like_agent("Сдаётся квapтиpa от собcтвeнника") is False
