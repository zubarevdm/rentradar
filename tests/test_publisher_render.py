"""Тест рендера поста: ключевые поля присутствуют в тексте."""

from __future__ import annotations

from rentradar.models import Listing, ScoreBreakdown, ScoredListing, SignalScore
from rentradar.publisher import TelegramPublisher


def test_render_contains_key_fields(sample_listing: Listing) -> None:
    scored = ScoredListing(listing=sample_listing, score=ScoreBreakdown(total=72.0, signals=[]))
    text = TelegramPublisher(dry_run=True).render(scored)

    assert "55 000 ₽" in text
    assert sample_listing.url in text
    assert "Парк культуры" in text
    assert "Открыть объявление на источнике" in text
    assert "#1комн" in text  # навигационные хэштеги в канале
    # Строку оценки в новом формате не показываем.
    assert "72/100" not in text


def test_render_cta_link(sample_listing: Listing) -> None:
    pub = TelegramPublisher(
        dry_run=True, cta_text="FlatLikeThat — найти", cta_url="https://t.me/x"
    )
    scored = ScoredListing(listing=sample_listing, score=ScoreBreakdown(total=70.0, signals=[]))
    text = pub.render(scored)
    assert '<a href="https://t.me/x">FlatLikeThat — найти</a>' in text
    # Без cta_url строки нет.
    plain = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=sample_listing, score=ScoreBreakdown(total=70.0, signals=[]))
    )
    assert "FlatLikeThat" not in plain


def test_render_omits_tasty_line_without_data(sample_listing: Listing) -> None:
    # Все сигналы — заглушки «нет данных» → строки ✨ быть не должно.
    scored = ScoredListing(
        listing=sample_listing,
        score=ScoreBreakdown(
            total=50.0,
            signals=[
                SignalScore(
                    name="below_market", value=0.5, weight=0.45, detail="нет рыночных данных"
                ),
                SignalScore(
                    name="price_per_m2", value=0.5, weight=0.25, detail="нет данных ₽/м²"
                ),
            ],
        ),
    )
    text = TelegramPublisher(dry_run=True).render(scored)
    assert "✨" not in text
    assert "нет рыночных данных" not in text


def test_move_in_block(sample_listing: Listing) -> None:
    # Пример заказчика: 50 000 + счётчики, комиссия 50%, залог 100% → тотал 125 000.
    scored = ScoredListing(
        listing=sample_listing.model_copy(
            update={
                "price": 50_000,
                "commission_pct": 50,
                "deposit_rub": 50_000,
                "meters_included": False,
            }
        ),
        score=ScoreBreakdown(total=70.0, signals=[]),
    )
    text = TelegramPublisher(dry_run=True).render(scored)
    assert "Заехать: 125 000 ₽" in text
    assert "1-й месяц" in text
    assert "100% залог" in text
    assert "комиссия 50%" in text
    # Счётчики — в строке цены/мес, НЕ в «Заехать» (ежемесячная доплата, не разовая).
    assert "/ мес. + счётчики" in text
    assert "(+ залог)" not in text  # залог только в «Заехать», не в строке цены
    # Разбивка «Заехать» без счётчиков.
    move_in_line = next(ln for ln in text.splitlines() if "Заехать" in ln)
    assert "счётчики" not in move_in_line


def test_utilities_rub_in_price_line(sample_listing: Listing) -> None:
    # Фикс. ЖКУ сверх аренды → «+ N ₽ ЖКУ» в строке цены.
    scored = ScoredListing(
        listing=sample_listing.model_copy(update={"price": 40_000, "utilities_rub": 5_500}),
        score=ScoreBreakdown(total=70.0, signals=[]),
    )
    text = TelegramPublisher(dry_run=True).render(scored)
    assert "+ 5 500 ₽ ЖКУ" in text


def test_move_in_block_omitted_without_terms(sample_listing: Listing) -> None:
    # Нет данных об условиях → ни строки 📋, ни 💰.
    text = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=sample_listing, score=ScoreBreakdown(total=70.0, signals=[]))
    )
    assert "Заехать" not in text
    assert "комиссия" not in text


def test_renovation_line_shown_when_enabled(sample_listing: Listing) -> None:
    scored = ScoredListing(
        listing=sample_listing.model_copy(update={"renovation": "designer", "appeal": 88}),
        score=ScoreBreakdown(total=80.0, signals=[]),
    )
    # Флаг включён → строка ремонта есть.
    on = TelegramPublisher(dry_run=True, show_renovation=True).render(scored)
    assert "Ремонт: дизайнерский" in on
    assert "88/100" in on
    # Флаг выключен → строки нет (значение спрятано).
    off = TelegramPublisher(dry_run=True, show_renovation=False).render(scored)
    assert "Ремонт" not in off


def test_custom_emoji_only_in_personal(sample_listing: Listing) -> None:
    emoji = {"home": "111", "price": "222", "lines": {"3": "333"}}
    scored = ScoredListing(
        listing=sample_listing.model_copy(update={"metro_line_color": "0042a5"}),
        score=ScoreBreakdown(total=80.0, signals=[]),
    )
    pub = TelegramPublisher(dry_run=True, emoji=emoji)

    # Канал (по умолчанию) — без кастома, стандартные эмодзи.
    public = pub.render(scored)
    assert "<tg-emoji" not in public
    assert "🏢" in public

    # Личка — кастом-эмодзи через <tg-emoji>.
    personal = pub.render(scored, use_custom_emoji=True)
    assert '<tg-emoji emoji-id="111">🏢</tg-emoji>' in personal
    assert '<tg-emoji emoji-id="222">💳</tg-emoji>' in personal
    assert '<tg-emoji emoji-id="333">🔵</tg-emoji>' in personal  # синяя линия
