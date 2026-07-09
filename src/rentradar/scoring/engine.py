"""Заглушка движка скоринга (Глава 0).

Реализует контракт `ScoringEngine`, но пока считает только базовый фрод-фильтр
и возвращает нейтральный скор. Полная модель «вкусности» (4 сигнала + медианы)
придёт в Главе 3 — она встанет на это же место без изменения остального кода.
"""

from __future__ import annotations

from ..config import SearchProfile
from ..interfaces import ScoringEngine
from ..models import Listing, ScoreBreakdown, SignalScore


class PlaceholderScoringEngine(ScoringEngine):
    """Временный движок: hard-rules фрод-фильтр + нейтральный скор 50."""

    async def score(self, listing: Listing, profile: SearchProfile) -> ScoreBreakdown:
        fraud = profile.fraud

        if listing.price < fraud.min_price:
            return ScoreBreakdown(
                total=0.0,
                rejected=True,
                reject_reason=f"price < min_price ({listing.price} < {fraud.min_price})",
            )
        if fraud.require_photos and not listing.photos:
            return ScoreBreakdown(
                total=0.0, rejected=True, reject_reason="no photos"
            )

        return ScoreBreakdown(
            total=50.0,
            signals=[
                SignalScore(
                    name="placeholder",
                    value=0.5,
                    weight=1.0,
                    detail="нейтральный скор — движок будет реализован в Главе 3",
                )
            ],
        )
