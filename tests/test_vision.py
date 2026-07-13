"""Тесты vision-слоя: парсинг ответа модели, выбор фото, паблик-гейт. Без сети."""

from __future__ import annotations

from datetime import datetime

from rentradar.cli import _passes_public_gate
from rentradar.config import Settings
from rentradar.models import Listing, ScoreBreakdown, ScoredListing
from rentradar.publisher.telegram import _select_photos
from rentradar.vision import parse_analysis
from rentradar.vision.analyzer import _is_quota_status


def test_quota_status_classification() -> None:
    assert _is_quota_status(402, "") is True   # payment required
    assert _is_quota_status(429, "") is True   # лимит/квота
    assert _is_quota_status(401, "") is True   # неверный ключ
    assert _is_quota_status(500, "server error") is False  # транзиентная — не алертим
    assert _is_quota_status(200, "ok") is False
    assert _is_quota_status(400, "insufficient_quota") is True  # по тексту


def test_parse_analysis_clean() -> None:
    text = '{"renovation":"designer","has_furniture":true,"appeal":85,"best_photos":[2,0,4]}'
    a = parse_analysis(text, n_photos=5)
    assert a is not None
    assert a.renovation == "designer"
    assert a.has_furniture is True
    assert a.appeal == 85
    assert a.best_photos == [2, 0, 4]


def test_parse_analysis_fenced_and_clamped() -> None:
    # ```json-ограждение``` + индексы вне диапазона / дубли / bool — чистятся.
    text = '```json\n{"renovation":"modern","has_furniture":false,' \
        '"appeal":60,"best_photos":[0,9,9,-1,3,1,2]}\n```'
    a = parse_analysis(text, n_photos=5)
    assert a is not None
    assert a.renovation == "modern"
    assert a.best_photos == [0, 3, 1, 2]  # 9 и -1 убраны, дубли схлопнуты, ≤5


def test_parse_analysis_contact_overlay() -> None:
    a = parse_analysis('{"renovation":"modern","best_photos":[0],"contact_overlay":true}', 3)
    assert a is not None and a.contact_overlay is True
    b = parse_analysis('{"renovation":"modern","best_photos":[0]}', 3)
    assert b is not None and b.contact_overlay is False  # по умолчанию


def test_parse_analysis_invalid_renovation_to_unknown() -> None:
    a = parse_analysis('{"renovation":"luxury","best_photos":[]}', n_photos=3)
    assert a is not None
    assert a.renovation == "unknown"


def test_parse_analysis_garbage_returns_none() -> None:
    assert parse_analysis("это не json вообще", n_photos=3) is None


def test_select_photos_prefers_best(sample_listing: Listing) -> None:
    photos = [f"u{i}" for i in range(6)]
    chosen = sample_listing.model_copy(update={"photos": photos, "best_photos": ["u4", "u1"]})
    assert _select_photos(chosen, max_photos=10) == ["u4", "u1"]
    # Нет отбора → первые фото, с капом по лимиту.
    plain = sample_listing.model_copy(update={"photos": photos, "best_photos": []})
    assert _select_photos(plain, max_photos=3) == ["u0", "u1", "u2"]


def _scored(listing: Listing) -> ScoredListing:
    return ScoredListing(listing=listing, score=ScoreBreakdown(total=80.0, signals=[]))


_AN = datetime(2026, 6, 27, 12, 0, 0)  # метка «проанализировано»


def test_public_gate_requires_analyzed_and_renovation(sample_listing: Listing) -> None:
    s = Settings()  # дефолт: блокируем needs_repair/soviet, требуем analyzed, appeal≥75
    an = {"analyzed_at": _AN, "appeal": 88}  # красивый интерьер, проходит WOW-порог
    soviet = sample_listing.model_copy(update={"renovation": "soviet", **an})
    designer = sample_listing.model_copy(update={"renovation": "designer", **an})
    assert _passes_public_gate(_scored(soviet), s) is False  # убитый ремонт
    assert _passes_public_gate(_scored(designer), s) is True
    # Неанализированный (analyzed_at None) в канал НЕ пускаем — скам не проверен.
    assert _passes_public_gate(_scored(sample_listing), s) is False


def test_public_gate_blocks_flagged(sample_listing: Listing) -> None:
    s = Settings()
    flagged = sample_listing.model_copy(update={"flagged": True, "analyzed_at": _AN})
    assert _passes_public_gate(_scored(flagged), s) is False


def test_public_gate_min_appeal() -> None:
    base = Listing(
        source="cian", external_id="1", url="http://x", price=50000, city="Москва",
        collected_at=_AN, analyzed_at=_AN,
    )
    s = Settings(public_min_appeal=50)
    assert _passes_public_gate(_scored(base.model_copy(update={"appeal": 30})), s) is False
    assert _passes_public_gate(_scored(base.model_copy(update={"appeal": 70})), s) is True
    # appeal неизвестен (нет фото квартиры) → в WOW-канал НЕ пускаем.
    assert _passes_public_gate(_scored(base), s) is False


def test_public_gate_max_price() -> None:
    base = Listing(
        source="cian", external_id="1", url="http://x", price=170_000, city="Москва",
        collected_at=_AN, analyzed_at=_AN, appeal=90,
    )
    s = Settings(public_max_price=100_000)
    assert _passes_public_gate(_scored(base), s) is False  # дороже потолка
    cheap = base.model_copy(update={"price": 60_000})
    assert _passes_public_gate(_scored(cheap), s) is True
    # 0 = потолок выключен.
    assert _passes_public_gate(_scored(base), Settings(public_max_price=0)) is True


def test_parse_analysis_no_interior_clears_best() -> None:
    # Нет фото квартиры (только дом/двор) → best_photos пусто, флаг no_interior.
    a = parse_analysis(
        '{"renovation":"unknown","appeal":0,"best_photos":[0,1,2],"no_interior":true}', 5
    )
    assert a is not None
    assert a.no_interior is True
    assert a.best_photos == []
