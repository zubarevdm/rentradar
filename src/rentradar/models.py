"""Доменные модели RentRadar.

Здесь живут чистые данные конвейера, независимые от площадок и от способа
хранения. Любой `Collector` обязан вернуть `RawListing`, нормализатор приводит
его к каноническому `Listing`, скоринг навешивает `ScoreBreakdown`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Source(StrEnum):
    """Источник объявления (площадка)."""

    CIAN = "cian"
    YANDEX = "yandex"
    AVITO = "avito"


class DealType(StrEnum):
    """Тип сделки."""

    LONG_RENT = "long_rent"
    SHORT_RENT = "short_rent"
    SALE = "sale"


class PropertyType(StrEnum):
    """Тип объекта."""

    FLAT = "flat"
    STUDIO = "studio"
    ROOM = "room"
    HOUSE = "house"
    UNKNOWN = "unknown"


class Renovation(StrEnum):
    """Состояние ремонта (по оценке vision-нейросети), от худшего к лучшему."""

    NEEDS_REPAIR = "needs_repair"  # голые стены, убитое, идёт ремонт
    SOVIET = "soviet"  # старый «бабушкин» ремонт
    SIMPLE = "simple"  # чистый косметический
    MODERN = "modern"  # свежий евроремонт
    DESIGNER = "designer"  # продуманный дизайнерский интерьер
    UNKNOWN = "unknown"  # не удалось определить

    @property
    def rank(self) -> int:
        """Порядковый ранг для сравнения «не хуже чем» (unknown = -1)."""
        order = ["needs_repair", "soviet", "simple", "modern", "designer"]
        return order.index(self.value) if self.value in order else -1


class RawListing(BaseModel):
    """Сырое объявление как его отдал `Collector`.

    Поля намеренно «рыхлые» (часть может отсутствовать) — нормализатор отвечает
    за приведение к строгому `Listing`. `payload` хранит исходный словарь для
    отладки и для повторного маппинга при изменении парсера.
    """

    model_config = ConfigDict(frozen=True)

    source: Source
    external_id: str
    url: str
    payload: dict = Field(default_factory=dict)


class Listing(BaseModel):
    """Каноническое объявление после нормализации.

    Это единственная форма, с которой работают дедуп, скоринг и публикатор.
    """

    model_config = ConfigDict(frozen=True)

    source: Source
    external_id: str
    url: str

    deal_type: DealType = DealType.LONG_RENT
    property_type: PropertyType = PropertyType.UNKNOWN

    price: int = Field(gt=0, description="Цена в рублях за месяц (для аренды)")
    area: float | None = Field(default=None, gt=0, description="Площадь, м²")
    rooms: int | None = Field(default=None, ge=0, description="Комнат (0 = студия)")

    city: str
    district: str | None = None
    address: str | None = None
    metro: str | None = None
    metro_distance_min: int | None = Field(default=None, ge=0)
    metro_line_color: str | None = None  # hex цвета линии (для кружка в посте)

    lat: float | None = None
    lon: float | None = None

    floor: int | None = Field(default=None, ge=0)
    floors_total: int | None = Field(default=None, ge=0)
    building_material: str | None = None
    description: str | None = None  # текст объявления (для фильтра риелторских фраз)

    # Условия заезда (для «суммы на заезд» в посте).
    commission_pct: int | None = Field(default=None, ge=0, description="Комиссия с клиента, %")
    deposit_rub: int | None = Field(default=None, ge=0, description="Залог, ₽")
    meters_included: bool | None = None  # счётчики/коммуналка включены в цену?

    # Анализ фото нейросетью (vision) — заполняется отдельным проходом, кэшируется.
    renovation: str | None = None  # значение Renovation
    has_furniture: bool | None = None
    appeal: int | None = Field(default=None, ge=0, le=100)  # визуальная привлекательность
    best_photos: list[str] = Field(default_factory=list)  # отобранные URL для поста
    flagged: bool = False  # скам: текст-призыв связаться вне площадки на фото
    analyzed_at: datetime | None = None
    # Дообогащение с детальной страницы (Авито: описание/залог/комиссия/полноразмер-
    # фото). None = либо всё было в выдаче (Циан/Яндекс), либо ещё не дошла очередь.
    enriched_at: datetime | None = None

    photos: list[str] = Field(default_factory=list)
    contact_hash: str | None = None

    published_at: datetime | None = None
    collected_at: datetime

    @field_validator("city", "district", "address", "metro")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @property
    def price_per_m2(self) -> float | None:
        """Цена за квадратный метр, ₽/м² (None если площадь неизвестна)."""
        if self.area:
            return self.price / self.area
        return None

    @property
    def deposit_pct(self) -> int | None:
        """Залог как % от месячной цены (None если залог неизвестен)."""
        if self.deposit_rub is None or self.price <= 0:
            return None
        return round(self.deposit_rub / self.price * 100)

    @property
    def move_in_total(self) -> int | None:
        """Сумма, чтобы заехать: первый месяц + комиссия + залог.

        Считается только когда известны И комиссия, И залог — иначе цифра врала бы
        (например, у Яндекса залог обычно не указан).
        """
        if self.commission_pct is None or self.deposit_rub is None:
            return None
        commission = round(self.price * self.commission_pct / 100)
        return self.price + commission + self.deposit_rub

    @property
    def fingerprint(self) -> str:
        """Стабильный отпечаток для дедупа.

        Считается по нормализованному адресу/гео + комнатность + бакет площади +
        бакет цены, чтобы один и тот же объект (в т.ч. с разных площадок и при
        мелких правках цены) схлопывался в один ключ.
        """
        addr = _normalize_text(self.address or self.district or "")
        area_bucket = int(self.area // 5) if self.area else -1  # шаг 5 м²
        price_bucket = self.price // 1000  # шаг 1000 ₽
        rooms = self.rooms if self.rooms is not None else -1
        key = f"{self.city.lower()}|{addr}|{rooms}|{area_bucket}|{price_bucket}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    @property
    def content_key(self) -> str:
        """Отпечаток объекта БЕЗ учёта цены — для дедупа «той же квартиры».

        Одну квартиру часто перевыкладывают (другой id, другая цена). Этот ключ
        (адрес + комнаты + бакет площади) схлопывает такие повторы, чтобы один
        объект не попадал в канал дважды.
        """
        addr = _normalize_text(self.address or self.district or "")
        area_bucket = int(self.area // 5) if self.area else -1
        rooms = self.rooms if self.rooms is not None else -1
        key = f"{self.city.lower()}|{addr}|{rooms}|{area_bucket}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    @property
    def segment_key(self) -> str:
        """Точный (самый узкий) сегмент: город+район+комнаты+бакет площади."""
        return self.segment_keys[0]

    @property
    def segment_keys(self) -> list[str]:
        """Сегменты от точного к грубому — для иерархического fallback медиан.

        На MVP-объёмах точный сегмент почти всегда пуст (мало наблюдений), поэтому
        скоринг спускается к более грубому, у которого медиана уже статистически
        значима: точный → район+комнаты → город+комнаты.
        """
        return build_segment_keys(self.city, self.district, self.rooms, self.area)


class SignalScore(BaseModel):
    """Один нормированный сигнал «вкусности» с пояснением."""

    name: str
    value: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0)
    detail: str = ""


class ScoreBreakdown(BaseModel):
    """Результат скоринга: итог + разложение по сигналам + флаги фрод-фильтра.

    `total` в шкале [0..100]. `rejected=True` означает, что лот отсечён жёсткими
    правилами (скам/мусор) и публиковаться не должен.
    """

    total: float = Field(ge=0.0, le=100.0)
    signals: list[SignalScore] = Field(default_factory=list)
    rejected: bool = False
    reject_reason: str | None = None
    flags: list[str] = Field(default_factory=list)

    @property
    def is_publishable(self) -> bool:
        return not self.rejected


class ScoredListing(BaseModel):
    """Объявление вместе с его оценкой — то, что идёт в публикатор."""

    model_config = ConfigDict(frozen=True)

    listing: Listing
    score: ScoreBreakdown


def build_segment_keys(
    city: str, district: str | None, rooms: int | None, area: float | None
) -> list[str]:
    """Построить иерархию сегментных ключей от точного к грубому.

    Общая функция для `Listing.segment_keys` и пересчёта медиан в хранилище —
    чтобы ключи строились одинаково в обоих местах.
    """
    c = city.lower()
    r = rooms if rooms is not None else -1
    d = _normalize_text(district or "")
    area_bucket = int(area // 10) if area else -1  # шаг 10 м²
    fine = f"{c}|{d}|{r}|{area_bucket}"
    mid = f"{c}|{d}|{r}"
    coarse = f"{c}|{r}"
    # dict.fromkeys убирает дубли (если район пуст, fine/mid могут совпасть),
    # сохраняя порядок от точного к грубому.
    return list(dict.fromkeys([fine, mid, coarse]))


# Приоритет площадок при равной цене одной и той же квартиры (кросс-дедуп).
SOURCE_PRIORITY = {"avito": 0, "cian": 1, "yandex": 2}


def source_rank(source_value: str, price: int) -> tuple[int, int]:
    """Ключ выбора лучшей площадки для одной квартиры: дешевле, затем приоритет."""
    return (price, SOURCE_PRIORITY.get(source_value, 9))


def dedupe_cross_source(listings: Iterable[Listing]) -> list[Listing]:
    """Схлопнуть одну квартиру, выставленную на нескольких площадках, в одну.

    Оставляем где ДЕШЕВЛЕ; при равной цене — приоритет Avito > Cian > Yandex.
    Ключ схлопывания — `content_key` (адрес+комнаты+площадь, без цены и источника).
    Порядок первого появления ключа сохраняется — важно для «сначала свежие»."""
    order: list[str] = []
    best: dict[str, Listing] = {}
    for lst in listings:
        key = lst.content_key
        if key not in best:
            order.append(key)
            best[key] = lst
            continue
        cur = best[key]
        if source_rank(lst.source.value, lst.price) < source_rank(cur.source.value, cur.price):
            best[key] = lst
    return [best[key] for key in order]


def _normalize_text(text: str) -> str:
    """Грубая нормализация строки для отпечатков: нижний регистр, схлопывание
    пробелов, удаление пунктуации. Намеренно простая — без морфологии."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()
