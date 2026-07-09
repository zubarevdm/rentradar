"""Базовый HTTP-коллектор: общий httpx-клиент, вежливый rate-limit, ретраи.

Конкретные адаптеры (Циан и т.д.) наследуются и реализуют `fetch`/`normalize`.
Здесь — только инфраструктура запросов, общая для всех площадок.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import SearchProfile
from ..errors import CollectorBlockedError
from ..interfaces import BaseCollector
from ..models import Listing, RawListing

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class HttpCollector(BaseCollector):
    """Абстрактный коллектор поверх httpx с бэкоффом и троттлингом."""

    source_name = "http"

    def __init__(
        self,
        *,
        min_delay: float = 1.5,
        max_retries: int = 3,
        timeout: float = 20.0,
        proxy: str | None = None,
    ) -> None:
        self._min_delay = min_delay
        self._max_retries = max_retries
        self._timeout = timeout
        # HTTP-прокси (в т.ч. web-unblocker, решающий антибот). None = напрямую.
        self._proxy = proxy or None
        self._last_request_at = 0.0

    async def _get_json(self, client: httpx.AsyncClient, url: str, **kwargs: object) -> dict:
        """GET с ретраями/бэкоффом, возвращает JSON."""
        return await self._request_json(client, "GET", url, **kwargs)

    async def _post_json(self, client: httpx.AsyncClient, url: str, **kwargs: object) -> dict:
        """POST с ретраями/бэкоффом, возвращает JSON."""
        return await self._request_json(client, "POST", url, **kwargs)

    async def _request_json(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs: object
    ) -> dict:
        """Запрос с троттлингом, ретраями и детектом антибот-блокировки.

        Редирект (3xx) на запрос API трактуем как блок (капча/антибот) и кидаем
        `CollectorBlockedError` без ретраев — повторять бессмысленно.
        """
        await self._throttle()
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await client.request(method, url, timeout=self._timeout, **kwargs)  # type: ignore[arg-type]
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    raise CollectorBlockedError(
                        f"{method} {url} → редирект на {location!r} (вероятно капча/антибот)"
                    )
                resp.raise_for_status()
                return resp.json()
            except CollectorBlockedError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                backoff = self._min_delay * (2 ** (attempt - 1))
                logger.warning(
                    "collector %s failed (try %s): %s; sleep %.1fs", method, attempt, exc, backoff
                )
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    async def _throttle(self) -> None:
        loop = asyncio.get_event_loop()
        elapsed = loop.time() - self._last_request_at
        if elapsed < self._min_delay:
            await asyncio.sleep(self._min_delay - elapsed)
        self._last_request_at = loop.time()

    def _client(self, *, follow_redirects: bool = False) -> httpx.AsyncClient:
        # follow_redirects=False по умолчанию: на API-запросах редирект = блок,
        # его нужно поймать, а не «проглотить». proxy=None → прямое соединение.
        return httpx.AsyncClient(
            headers=_DEFAULT_HEADERS, follow_redirects=follow_redirects, proxy=self._proxy
        )

    # Подклассы обязаны реализовать contract BaseCollector:
    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        raise NotImplementedError

    def normalize(self, raw: RawListing) -> Listing | None:
        raise NotImplementedError
