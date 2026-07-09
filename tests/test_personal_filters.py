"""Тесты движка матчинга пользовательского фильтра."""

from __future__ import annotations

from datetime import datetime

from rentradar.models import DealType, Listing, PropertyType, Source
from rentradar.personal import UserFilter, matches


def _listing(**over: object) -> Listing:
    base = {
        "source": Source.CIAN,
        "external_id": "1",
        "url": "u",
        "deal_type": DealType.LONG_RENT,
        "property_type": PropertyType.FLAT,
        "price": 55_000,
        "area": 38.0,
        "rooms": 1,
        "city": "Москва",
        "district": "Хамовники",
        "metro": "Парк культуры",
        "metro_distance_min": 7,
        "collected_at": datetime(2026, 6, 21, 12, 0, 0),
    }
    base.update(over)
    return Listing(**base)  # type: ignore[arg-type]


def test_empty_filter_matches_same_city() -> None:
    assert matches(_listing(), UserFilter(user_id=1, city="Москва"))


def test_city_mismatch() -> None:
    assert not matches(_listing(), UserFilter(user_id=1, city="Санкт-Петербург"))


def test_rooms_filter() -> None:
    assert matches(_listing(rooms=1), UserFilter(user_id=1, rooms=[1, 2]))
    assert not matches(_listing(rooms=3), UserFilter(user_id=1, rooms=[1, 2]))


def test_price_range() -> None:
    f = UserFilter(user_id=1, price_min=40_000, price_max=60_000)
    assert matches(_listing(price=55_000), f)
    assert not matches(_listing(price=70_000), f)
    assert not matches(_listing(price=30_000), f)


def test_area_range() -> None:
    f = UserFilter(user_id=1, area_min=30.0, area_max=45.0)
    assert matches(_listing(area=38.0), f)
    assert not matches(_listing(area=60.0), f)
    assert not matches(_listing(area=None), f)


def test_metro_filter_case_insensitive() -> None:
    f = UserFilter(user_id=1, metros=["парк культуры", "Фрунзенская"])
    assert matches(_listing(metro="Парк культуры"), f)
    assert not matches(_listing(metro="Тверская"), f)
    assert not matches(_listing(metro=None), f)


def test_district_filter() -> None:
    f = UserFilter(user_id=1, districts=["Хамовники"])
    assert matches(_listing(district="Хамовники"), f)
    assert not matches(_listing(district="Капотня"), f)


def test_max_metro_minutes() -> None:
    f = UserFilter(user_id=1, max_metro_min=10)
    assert matches(_listing(metro_distance_min=7), f)
    assert not matches(_listing(metro_distance_min=20), f)
    assert not matches(_listing(metro_distance_min=None), f)


def test_combined_filter() -> None:
    f = UserFilter(
        user_id=1,
        rooms=[1],
        price_min=40_000,
        price_max=60_000,
        metros=["Парк культуры"],
        max_metro_min=10,
    )
    assert matches(_listing(), f)
    assert not matches(_listing(price=80_000), f)


def test_renovation_min_filter() -> None:
    f = UserFilter(user_id=1, renovation_min="modern")  # современный и выше
    assert matches(_listing(renovation="modern"), f)
    assert matches(_listing(renovation="designer"), f)
    assert not matches(_listing(renovation="simple"), f)
    assert not matches(_listing(renovation="soviet"), f)
    # Непроанализированный (renovation None) при активном фильтре не проходит.
    assert not matches(_listing(renovation=None), f)


def test_renovation_only_designer() -> None:
    f = UserFilter(user_id=1, renovation_min="designer")
    assert matches(_listing(renovation="designer"), f)
    assert not matches(_listing(renovation="modern"), f)


def test_no_renovation_filter_passes_all() -> None:
    f = UserFilter(user_id=1)  # renovation_min None → не ограничивает
    assert matches(_listing(renovation=None), f)
    assert matches(_listing(renovation="needs_repair"), f)


def test_ignore_renovation_keeps_structural_checks() -> None:
    # ignore_renovation=True: ремонт игнорируем, но метро/цена по-прежнему режут.
    f = UserFilter(user_id=1, metros=["ЦСКА"], renovation_min="designer")
    near = _listing(metro="ЦСКА", renovation=None)
    far = _listing(metro="Тверская", renovation="designer")
    # Без анализа ремонта: лот у ЦСКА структурно подходит (его и надо оценить).
    assert matches(near, f, ignore_renovation=True)
    # Не у ЦСКА — структурно мимо, даже если дизайнерский.
    assert not matches(far, f, ignore_renovation=True)
    # А обычный matches лот у ЦСКА без оценки ремонта не пропустит.
    assert not matches(near, f)


def test_flagged_never_matches() -> None:
    # Скам-оверлей на фото — не шлём никому, даже под пустой фильтр.
    assert not matches(_listing(flagged=True), UserFilter(user_id=1))
    assert matches(_listing(flagged=False), UserFilter(user_id=1))


def test_no_commission_filter() -> None:
    f = UserFilter(user_id=1, no_commission=True)
    assert matches(_listing(commission_pct=0), f)  # без комиссии — проходит
    assert not matches(_listing(commission_pct=50), f)  # есть комиссия — агент
    assert matches(_listing(commission_pct=None), f)  # неизвестна (Авито) + текст чист
    # Риелторская фраза в описании — отсекаем даже при «комиссии 0».
    agent = _listing(commission_pct=0, description="Большая база, подберём варианты под вас")
    assert not matches(agent, f)


def test_looks_like_agent() -> None:
    from rentradar.personal.filters import looks_like_agent

    assert looks_like_agent("У нас большая база квартир") is True
    assert looks_like_agent("Агентство недвижимости, поможем подобрать") is True
    assert looks_like_agent("Сдаётся уютная квартира от собственника, без комиссии") is False
    assert looks_like_agent(None) is False


def test_clamp_interval() -> None:
    assert UserFilter(user_id=1, interval_min=1).clamp_interval() == 5
    assert UserFilter(user_id=1, interval_min=99999).clamp_interval() == 1440
    assert UserFilter(user_id=1, interval_min=30).clamp_interval() == 30
