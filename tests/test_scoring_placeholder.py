"""Тесты заглушки скоринга: фрод-фильтр отсекает мусор, валидный лот проходит."""

from __future__ import annotations

import pytest

from rentradar.config import SearchProfile
from rentradar.models import Listing
from rentradar.scoring import PlaceholderScoringEngine


@pytest.fixture
def profile() -> SearchProfile:
    return SearchProfile(name="t", city="Москва")


async def test_valid_listing_scored(sample_listing: Listing, profile: SearchProfile) -> None:
    score = await PlaceholderScoringEngine().score(sample_listing, profile)
    assert score.is_publishable
    assert score.total == 50.0


async def test_rejects_below_min_price(sample_listing: Listing, profile: SearchProfile) -> None:
    cheap = sample_listing.model_copy(update={"price": 1_000})
    score = await PlaceholderScoringEngine().score(cheap, profile)
    assert score.rejected
    assert "min_price" in (score.reject_reason or "")


async def test_rejects_when_no_photos(sample_listing: Listing, profile: SearchProfile) -> None:
    no_photo = sample_listing.model_copy(update={"photos": []})
    score = await PlaceholderScoringEngine().score(no_photo, profile)
    assert score.rejected
    assert score.reject_reason == "no photos"
