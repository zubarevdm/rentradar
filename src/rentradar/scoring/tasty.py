"""Движок «вкусности» TastyScore (Глава 3).

Считает 5 нормированных сигналов и взвешенную сумму [0..100]:
  1. below_market  — насколько цена ниже медианы сегмента;
  2. price_per_m2  — насколько ₽/м² ниже медианы сегмента;
  3. location      — близость к метро (инфраструктура);
  4. freshness     — свежесть объявления (экспоненциальное затухание);
  5. condition     — состояние/красота интерьера по vision-оценке (appeal).

Жёсткие правила (фрод-фильтр) отсекают мусор ДО взвешивания; anti-skam guard
отсекает «слишком хорошо, чтобы быть правдой» (экстремальный дисконт = вероятная
приманка мошенника). Если рыночных данных по сегменту мало — рыночные сигналы
получают нейтральные 0.5 с флагом, чтобы холодный старт не выдавал ложные «топы».
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from ..config import SearchProfile
from ..interfaces import MarketStatsProvider, ScoringEngine
from ..models import Listing, ScoreBreakdown, SignalScore
from ..text_signals import solicits_offplatform
from .districts import district_weight

# «Мёртвая зона»: дисконт ниже этого — рыночный шум, не сделка (сигнал = 0).
MIN_DISCOUNT = 0.10
# Дисконт, при котором сигнал «ниже рынка» достигает максимума (35% ниже медианы).
FULL_DISCOUNT = 0.35
# Минимум наблюдений в сегменте, чтобы доверять медиане.
MIN_SAMPLE = 5
# Метро в пределах этого времени пешком — максимум по локации; дальше — затухание.
METRO_BEST_MIN = 5
METRO_FAR_MIN = 25
# Период полураспада свежести (часы): через сутки сигнал ~0.5.
FRESHNESS_TAU_H = 24.0


class TastyScoringEngine(ScoringEngine):
    def __init__(self, market: MarketStatsProvider) -> None:
        self._market = market

    async def _resolve_segment(self, listing: Listing) -> tuple[str | None, bool]:
        """Найти самый узкий сегмент с достаточной выборкой.

        Возвращает (ключ, is_coarse). is_coarse=True, если пришлось спуститься до
        самого грубого уровня (город+комнаты, без района) — там сравнение
        ненадёжно (дешёвый район выглядит «недооценкой» против медианы по городу).
        """
        keys = listing.segment_keys
        for key in keys:
            if await self._market.sample_size(key) >= MIN_SAMPLE:
                return key, (key == keys[-1] and len(keys) > 1)
        return None, False

    async def score(self, listing: Listing, profile: SearchProfile) -> ScoreBreakdown:
        # ── фрод-фильтр (hard rules) ─────────────────────────────────────
        fraud = profile.fraud
        if listing.price < fraud.min_price:
            return _reject(f"цена < min_price ({listing.price} < {fraud.min_price})")
        if fraud.require_photos and not listing.photos:
            return _reject("нет фото")
        # Увод общения с площадки в описании (телеграм/вотсап/телефон) — не постим.
        if solicits_offplatform(listing.description):
            return _reject("увод с площадки в описании", flags=["offplatform"])

        flags: list[str] = []
        segment, is_coarse = await self._resolve_segment(listing)
        has_market = segment is not None
        median_price = await self._market.median_price(segment) if has_market else None
        median_ppm = await self._market.median_price_per_m2(segment) if has_market else None

        # Демпфер для грубого (city-wide) сегмента: дешёвый район ≠ выгодная
        # сделка. Умножаем дисконт на престижность района, чтобы окраины не
        # выглядели «недооценёнными» против медианы по всей Москве.
        damp = 1.0
        if is_coarse:
            flags.append("coarse_market")
            # Известный район → его вес; район неизвестен (часто у Яндекса) →
            # нейтрально-консервативный 0.6, иначе окраины без района выглядели бы
            # «недооценёнными» против медианы по всему городу.
            dw = district_weight(listing.city, listing.district)
            damp = dw if dw is not None else 0.6

        # ── anti-skam guard: экстремальный дисконт = вероятная приманка ───
        # Только на надёжном (не грубом) сегменте — иначе дешёвый район дал бы
        # ложный «скам».
        if median_price and not is_coarse:
            discount = (median_price - listing.price) / median_price
            if discount > fraud.suspicious_discount:
                return _reject(
                    f"подозрительный дисконт {discount:.0%} (> {fraud.suspicious_discount:.0%})",
                    flags=["suspicious_discount"],
                )

        # ── сигналы ──────────────────────────────────────────────────────
        weights = profile.weights
        signals: list[SignalScore] = []

        # 1. ниже рынка
        if median_price:
            discount = (median_price - listing.price) / median_price * damp
            val = _discount_signal(discount)
            detail = f"{discount:+.0%} к рынку" if discount > 0 else "по рынку"
        else:
            val, detail = 0.5, "нет рыночных данных"
            flags.append("no_market_price")
        signals.append(
            SignalScore(name="below_market", value=val, weight=weights.below_market, detail=detail)
        )

        # 2. цена за м²
        ppm = listing.price_per_m2
        if median_ppm and ppm:
            disc_ppm = (median_ppm - ppm) / median_ppm * damp
            val = _discount_signal(disc_ppm)
            detail = f"{disc_ppm:+.0%} к ₽/м²"
        else:
            val, detail = 0.5, "нет данных ₽/м²"
            if not has_market:
                flags.append("no_market_ppm")
        signals.append(
            SignalScore(name="price_per_m2", value=val, weight=weights.price_per_m2, detail=detail)
        )

        # 3. локация (метро)
        loc_val, loc_detail = _location_signal(listing)
        signals.append(
            SignalScore(
                name="location", value=loc_val, weight=weights.location, detail=loc_detail
            )
        )

        # 4. свежесть
        fr_val, fr_detail = _freshness_signal(listing)
        signals.append(
            SignalScore(
                name="freshness", value=fr_val, weight=weights.freshness, detail=fr_detail
            )
        )

        # 5. состояние/красота (vision-оценка appeal). Нет оценки → нейтральные 0.5.
        if listing.appeal is not None:
            cond_val = _clamp(listing.appeal / 100.0)
            cond_detail = f"интерьер {listing.appeal}/100"
        else:
            cond_val, cond_detail = 0.5, "интерьер не оценён"
            flags.append("no_appeal")
        signals.append(
            SignalScore(
                name="condition", value=cond_val, weight=weights.condition, detail=cond_detail
            )
        )

        total = _weighted_total(signals)
        return ScoreBreakdown(total=total, signals=signals, flags=flags)


# ── helpers ──────────────────────────────────────────────────────────────
def _reject(reason: str, *, flags: list[str] | None = None) -> ScoreBreakdown:
    return ScoreBreakdown(total=0.0, rejected=True, reject_reason=reason, flags=flags or [])


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _discount_signal(discount: float) -> float:
    """Дисконт → сигнал [0..1] со строгой кривой.

    Дисконт ниже MIN_DISCOUNT — рыночный шум (0). Максимум достигается только
    при FULL_DISCOUNT (40% ниже медианы). Между ними — линейный рост. Это держит
    высокие баллы редкими: «по рынку» больше не выглядит «вкусным».
    """
    if discount <= MIN_DISCOUNT:
        return 0.0
    return _clamp((discount - MIN_DISCOUNT) / (FULL_DISCOUNT - MIN_DISCOUNT))


def _location_signal(listing: Listing) -> tuple[float, str]:
    """Локация = смесь близости к метро и престижности района.

    Если есть оба фактора — взвешенно; если один — он и решает; если ничего —
    нейтрально-низко.
    """
    metro_val = _metro_component(listing.metro_distance_min, listing.metro)
    distr_val = district_weight(listing.city, listing.district)

    if metro_val is not None and distr_val is not None:
        val = 0.55 * metro_val + 0.45 * distr_val
    elif metro_val is not None:
        val = metro_val
    elif distr_val is not None:
        val = distr_val
    else:
        val = 0.3

    parts: list[str] = []
    if listing.metro:
        if listing.metro_distance_min is not None:
            parts.append(f"м. {listing.metro} ({listing.metro_distance_min} мин)")
        else:
            parts.append(f"м. {listing.metro}")
    if listing.district:
        parts.append(listing.district)
    return _clamp(val), ", ".join(parts) or "локация неизвестна"


def _metro_component(dist: int | None, metro: str | None) -> float | None:
    if dist is None:
        return 0.5 if metro else None
    if dist <= METRO_BEST_MIN:
        return 1.0
    if dist >= METRO_FAR_MIN:
        return 0.2
    return _clamp(1.0 - 0.8 * (dist - METRO_BEST_MIN) / (METRO_FAR_MIN - METRO_BEST_MIN))


def _freshness_signal(listing: Listing) -> tuple[float, str]:
    if listing.published_at is None:
        return 0.5, "дата неизвестна"
    now = datetime.now(UTC).replace(tzinfo=None)
    age_h = max(0.0, (now - listing.published_at).total_seconds() / 3600.0)
    val = math.exp(-age_h / FRESHNESS_TAU_H)
    if age_h < 1:
        detail = "только что"
    elif age_h < 24:
        detail = f"{int(age_h)} ч назад"
    else:
        detail = f"{int(age_h // 24)} дн назад"
    return _clamp(val), detail


def _weighted_total(signals: list[SignalScore]) -> float:
    weight_sum = sum(s.weight for s in signals)
    if weight_sum <= 0:
        return 0.0
    weighted = sum(s.value * s.weight for s in signals)
    return round(100.0 * weighted / weight_sum, 1)
