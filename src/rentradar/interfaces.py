"""Контракты слоёв конвейера.

Слои общаются только через эти абстракции — это позволяет добавлять площадки,
менять хранилище или способ публикации, не трогая остальную систему.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .config import SearchProfile
from .models import (
    Listing,
    RawListing,
    ScoreBreakdown,
    ScoredListing,
)


class BaseCollector(ABC):
    """Адаптер площадки. Знает, как достать сырые объявления по профилю."""

    #: Машинное имя источника (для логов/маршрутизации).
    source_name: str

    @abstractmethod
    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        """Вернуть сырые объявления, подходящие под профиль (свежие — первыми)."""

    @abstractmethod
    def normalize(self, raw: RawListing) -> Listing | None:
        """Привести сырое объявление к `Listing`. None — если данные битые."""


class MarketStatsProvider(ABC):
    """Источник рыночных медиан для сегмента (нужен скорингу)."""

    @abstractmethod
    async def median_price(self, segment_key: str) -> float | None:
        """Медианная цена по сегменту, либо None если данных недостаточно."""

    @abstractmethod
    async def median_price_per_m2(self, segment_key: str) -> float | None:
        """Медианная ₽/м² по сегменту, либо None."""

    @abstractmethod
    async def sample_size(self, segment_key: str) -> int:
        """Сколько наблюдений в сегменте (для решения о fallback)."""


class ScoringEngine(ABC):
    """Движок «вкусности»: Listing + рыночная статистика → ScoreBreakdown."""

    @abstractmethod
    async def score(self, listing: Listing, profile: SearchProfile) -> ScoreBreakdown:
        """Оценить лот в контексте профиля (его веса/пороги/фрод-правила)."""


class Storage(ABC):
    """Постоянное хранилище объявлений, статистики и журналов публикаций."""

    @abstractmethod
    async def init(self) -> None:
        """Создать схему БД, если её нет."""

    @abstractmethod
    async def upsert_listings(self, listings: Sequence[Listing]) -> list[Listing]:
        """Сохранить объявления; вернуть только НОВЫЕ (по fingerprint) — дедуп."""

    @abstractmethod
    async def was_posted(self, fingerprint: str) -> bool:
        """Постили ли уже этот лот (идемпотентность публикатора)."""

    @abstractmethod
    async def mark_posted(self, fingerprint: str, channel: str) -> None:
        """Зафиксировать факт публикации лота в канал."""


class Publisher(ABC):
    """Публикатор: форматирует и отправляет лот в Telegram."""

    @abstractmethod
    async def publish(
        self, scored: ScoredListing, *, channel: str, use_custom_emoji: bool = False
    ) -> bool:
        """Опубликовать оценённый лот. True — если реально отправлено.

        `use_custom_emoji` включает кастом-эмодзи (для личных чатов, не для канала).
        """

    @abstractmethod
    def render(self, scored: ScoredListing) -> str:
        """Отрендерить текст поста (без отправки) — для dry-run и тестов."""


__all__ = [
    "BaseCollector",
    "MarketStatsProvider",
    "ScoringEngine",
    "Storage",
    "Publisher",
    "Sequence",
]
