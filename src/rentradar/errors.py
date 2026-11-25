"""Доменные исключения RentRadar."""

from __future__ import annotations


class CollectorError(Exception):
    """Базовая ошибка слоя сбора."""


class CollectorBlockedError(CollectorError):
    """Площадка ответила антибот-страницей/капчей/редиректом.

    Сигнал гипотезы H4: «минимальный» доступ упёрся в защиту. Оркестратор должен
    поймать это, залогировать и (в Главе 5) уведомить администратора — а не
    падать трейсбеком.
    """


class CollectionSkipped(CollectorError):
    """The source was intentionally not polled."""


class DeliveryUncertain(Exception):
    """The request may have reached Telegram but no acknowledgement arrived."""


class DeliveryDeferred(Exception):
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        super().__init__(f"Telegram retry after {seconds}s")
