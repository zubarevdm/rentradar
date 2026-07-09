"""Анализ фото квартиры дешёвой vision-моделью.

Один вызов на лот разом решает обе задачи:
  • выбирает 3–5 лучших кадров для поста (индексы),
  • классифицирует ремонт (needs_repair…designer), наличие мебели, «красоту».

Клиент — OpenAI-совместимый chat/completions, поэтому один и тот же код работает
с Gemini (слой `/openai/`), OpenRouter и OpenAI — меняется только base_url/model.
Картинки шлём в режиме detail=low: токены/деньги ест именно разрешение, а для
классификации интерьера хватает мелкого превью.

Экономия: анализатор НЕ вызывается напрямую отсюда на каждый лот — оркестратор
гоняет его только на свежих кандидатах и кэширует результат в БД (один лот — один
вызов за всю жизнь). Нет ключа → `build_analyzer` вернёт None, vision выключен.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, field_validator

from ..config import Settings
from ..models import Renovation

logger = logging.getLogger(__name__)


class VisionQuotaError(Exception):
    """Ошибка API, похожая на «кончился баланс / лимит / неверный ключ».
    Оркестратор по ней уведомляет админа, а не молча глотает."""


def _is_quota_status(status: int, body: str) -> bool:
    """Статус/тело ответа указывают на проблему с балансом/квотой/ключом?"""
    if status in (401, 402, 403, 429):
        return True
    low = body.lower()
    return any(
        k in low for k in ("insufficient_quota", "billing", "exceeded your current quota")
    )

# Короткая инструкция: строго JSON, никаких пояснений (экономим выходные токены).
_PROMPT = (
    "Ты оцениваешь фото квартиры для объявления об аренде. "
    "Верни СТРОГО JSON без текста вокруг:\n"
    '{"renovation":"needs_repair|soviet|simple|modern|designer",'
    '"has_furniture":true|false,"appeal":0-100,"best_photos":[индексы 5-7 лучших],'
    '"contact_overlay":true|false}\n'
    "renovation: needs_repair — голые стены/убитое/идёт ремонт; soviet — старый "
    "«бабушкин» ремонт; simple — чистый косметический; modern — свежий евроремонт; "
    "designer — продуманный дизайнерский интерьер.\n"
    "appeal — насколько фото привлекательны для рекламы (0-100).\n"
    "contact_overlay: true, если НА ФОТО есть наложенный текст с призывом связаться вне "
    "площадки (имя+телефон/telegram/вотсап, «пишите собственнику», водяной знак с "
    "контактами) — признак мошенника. Логотип площадки (Avito/Циан) — НЕ contact_overlay.\n"
    "best_photos — индексы 5-7 самых информативных и красивых кадров, лучшее первым "
    "(общий вид комнат, кухня, санузел), без размытых/тёмных/дублей. "
    "Фото даны по порядку, индексация с 0."
)


class PhotoAnalysis(BaseModel):
    """Результат анализа фото. `best_photos` — индексы во ВХОДНОМ списке."""

    renovation: str = Renovation.UNKNOWN.value
    has_furniture: bool | None = None
    appeal: int | None = Field(default=None, ge=0, le=100)
    best_photos: list[int] = Field(default_factory=list)
    contact_overlay: bool = False  # текст-призыв связаться вне площадки на фото

    @field_validator("renovation")
    @classmethod
    def _valid_renovation(cls, v: object) -> str:
        s = str(v).strip().lower()
        valid = {r.value for r in Renovation}
        return s if s in valid else Renovation.UNKNOWN.value


def parse_analysis(text: str, n_photos: int) -> PhotoAnalysis | None:
    """Распарсить ответ модели в `PhotoAnalysis`. Терпим к ```json-ограждениям```.
    Индексы фото чистим: в диапазоне [0, n_photos), без дублей, не больше 5."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:].lstrip() if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    seen: set[int] = set()
    best: list[int] = []
    for i in data.get("best_photos") or []:
        if isinstance(i, bool):  # bool — подкласс int, отсекаем
            continue
        if isinstance(i, int) and 0 <= i < n_photos and i not in seen:
            seen.add(i)
            best.append(i)
        if len(best) >= 7:
            break
    data["best_photos"] = best
    try:
        return PhotoAnalysis.model_validate(data)
    except (ValueError, TypeError):
        return None


class PhotoAnalyzer(Protocol):
    async def analyze(self, photo_urls: list[str]) -> PhotoAnalysis | None: ...


class LLMPhotoAnalyzer:
    """Анализатор поверх OpenAI-совместимого chat/completions (Gemini/OpenRouter/OpenAI)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        max_images: int = 12,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") + "/"
        self._model = model
        self._max_images = max(1, max_images)
        self._timeout = timeout

    async def analyze(self, photo_urls: list[str]) -> PhotoAnalysis | None:
        urls = [u for u in photo_urls if u][: self._max_images]
        if not urls:
            return None
        # Ловим ЛЮБЫЕ ошибки, кроме квоты: битый URL фото (httpx.InvalidURL — не
        # HTTPError!), кривой ответ и т.п. должны дать None, а не убить весь
        # vision-проход — иначе «ядовитый» лот в голове FIFO-очереди клинит анализ
        # навсегда (канал при require_analyzed при этом молча голодает).
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                images = await self._fetch_images(client, urls)
                if not images:
                    return None
                content = await self._call(client, images)
        except VisionQuotaError:
            raise  # пусть оркестратор уведомит админа
        except Exception as exc:  # noqa: BLE001
            logger.warning("vision-вызов не удался: %s: %s", type(exc).__name__, exc)
            return None
        return parse_analysis(content, len(urls))

    async def _fetch_images(
        self, client: httpx.AsyncClient, urls: list[str]
    ) -> list[str]:
        """Скачать фото и завернуть в data-URI (base64). Битые молча пропускаем."""
        out: list[str] = []
        for url in urls:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception:  # noqa: BLE001 — битый URL/сеть: фото пропускаем
                continue
            mime = resp.headers.get("content-type", "").split(";")[0] or _mime_from_url(url)
            if not mime.startswith("image/"):
                mime = _mime_from_url(url)
            b64 = base64.b64encode(resp.content).decode("ascii")
            out.append(f"data:{mime};base64,{b64}")
        return out

    async def _call(self, client: httpx.AsyncClient, images: list[str]) -> str:
        parts: list[dict] = [{"type": "text", "text": _PROMPT}]
        parts += [
            {"type": "image_url", "image_url": {"url": data_uri, "detail": "low"}}
            for data_uri in images
        ]
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": parts}],
            "temperature": 0,
            # 200 не хватало: JSON с 7 индексами фото иногда обрезался на середине
            # → parse_analysis возвращал None («не удалось проанализировать»).
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
        }
        url = self._base_url + "chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 500:  # транзиентный сбой провайдера — один повтор
            await asyncio.sleep(2)
            resp = await client.post(url, headers=headers, json=body)
        if _is_quota_status(resp.status_code, resp.text):
            raise VisionQuotaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def build_analyzer(settings: Settings) -> PhotoAnalyzer | None:
    """Собрать анализатор из настроек. Нет ключа → None (vision выключен)."""
    if not settings.vision_api_key:
        return None
    return LLMPhotoAnalyzer(
        api_key=settings.vision_api_key,
        base_url=settings.vision_base_url,
        model=settings.vision_model,
        max_images=settings.vision_max_images,
    )


def _mime_from_url(url: str) -> str:
    low = url.lower()
    if ".png" in low:
        return "image/png"
    if ".webp" in low:
        return "image/webp"
    return "image/jpeg"
