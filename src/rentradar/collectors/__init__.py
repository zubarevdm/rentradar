"""Адаптеры площадок."""

from .avito import AvitoCollector
from .base import HttpCollector
from .cian import CianCollector
from .yandex import YandexCollector

__all__ = ["AvitoCollector", "CianCollector", "HttpCollector", "YandexCollector"]
