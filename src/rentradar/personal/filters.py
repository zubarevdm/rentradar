"""Пользовательский фильтр и движок матчинга.

`UserFilter` — критерии подписчика. `matches(listing, filter)` — чистая функция
«подходит ли лот под фильтр». Никаких сетевых вызовов: матчинг идёт по уже
собранным в БД лотам.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..models import Listing, Renovation, _normalize_text
from ..text_signals import looks_like_agent

# Допустимые интервалы опроса (минуты) — от частого (платно) к редкому.
MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 1440


class UserFilter(BaseModel):
    """Критерии поиска одного подписчика."""

    user_id: int
    name: str = "Мой поиск"
    city: str = "Москва"

    rooms: list[int] = Field(default_factory=list)  # пусто = любые
    price_min: int | None = None
    price_max: int | None = None
    area_min: float | None = None
    area_max: float | None = None

    metros: list[str] = Field(default_factory=list)  # станции метро (имена)
    districts: list[str] = Field(default_factory=list)  # районы
    max_metro_min: int | None = None  # максимум минут пешком до метро
    renovation_min: str | None = None  # мин. уровень ремонта (значение Renovation)
    no_commission: bool = False  # только без посредников (комиссия 0 + без риелт. фраз)

    interval_min: int = 30
    active: bool = True
    # Показывать ли то, что уже было на рынке (бэклог). True = все подходящие
    # (свежайшие сразу + старое досылаем постепенно); False = только новые
    # (пара свежих на старте, дальше лишь то, что появляется).
    include_backlog: bool = True
    # Когда фильтр проверяли в прошлый раз. None = ещё ни разу (холодный старт).
    # Используется диспетчером, чтобы отличить «свежие» лоты от старого бэклога.
    last_checked_at: datetime | None = None

    def clamp_interval(self) -> int:
        return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, self.interval_min))


def matches(listing: Listing, flt: UserFilter, *, ignore_renovation: bool = False) -> bool:
    """Подходит ли лот под фильтр пользователя. Пустые критерии не ограничивают.

    `ignore_renovation=True` — проверять всё, КРОМЕ уровня ремонта. Нужно, чтобы
    решить «стоит ли тратить нейронку на этот лот для данного пользователя»:
    структурные критерии (метро/цена/комнаты) дёшевы и фильтруют до vision."""
    if listing.flagged:  # скам-оверлей на фото — не шлём никому
        return False
    if _norm(listing.city) != _norm(flt.city):
        return False

    if flt.rooms and (listing.rooms is None or listing.rooms not in flt.rooms):
        return False

    if flt.price_min is not None and listing.price < flt.price_min:
        return False
    if flt.price_max is not None and listing.price > flt.price_max:
        return False

    if flt.area_min is not None and (listing.area is None or listing.area < flt.area_min):
        return False
    if flt.area_max is not None and (listing.area is None or listing.area > flt.area_max):
        return False

    if flt.metros:
        wanted = {_norm(m) for m in flt.metros}
        if listing.metro is None or _norm(listing.metro) not in wanted:
            return False

    if flt.districts:
        wanted = {_norm(d) for d in flt.districts}
        if listing.district is None or _norm(listing.district) not in wanted:
            return False

    if flt.max_metro_min is not None and (
        listing.metro_distance_min is None or listing.metro_distance_min > flt.max_metro_min
    ):
        return False

    # Ремонт не ниже выбранного. Непроанализированные (renovation is None) при
    # активном фильтре по ремонту НЕ проходят — нельзя подтвердить уровень.
    if not ignore_renovation and flt.renovation_min:
        if listing.renovation is None:
            return False
        if Renovation(listing.renovation).rank < Renovation(flt.renovation_min).rank:
            return False

    # Без посредников: явная комиссия > 0 = агент; риелторские фразы в описании —
    # тоже агент (даже если комиссия «0»). Неизвестная комиссия (None) допускается,
    # если текст чистый — иначе отсеяли бы весь Авито (у него комиссии нет в данных).
    if flt.no_commission:
        if listing.commission_pct is not None and listing.commission_pct > 0:
            return False
        if looks_like_agent(listing.description):
            return False

    return True


def _norm(text: str | None) -> str:
    return _normalize_text(text or "")
