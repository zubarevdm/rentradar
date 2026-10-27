"""Source normalization must not present transport time as walking time."""

import pytest

from rentradar.collectors.cian import CianCollector
from rentradar.collectors.yandex import YandexCollector
from rentradar.models import RawListing, Source
from rentradar.personal.filters import UserFilter, matches


def _cian(metro):
    return CianCollector().normalize(RawListing(
        source=Source.CIAN,
        external_id="metro-source-test",
        url="https://www.cian.ru/rent/flat/123/",
        payload={"bargainTerms": {"price": 50_000}, "roomsCount": 1,
                 "geo": {"undergrounds": metro}},
    ))


def _yandex(metro):
    return YandexCollector().normalize(RawListing(
        source=Source.YANDEX,
        external_id="metro-source-test",
        url="https://realty.yandex.ru/offer/123",
        payload={"price": {"value": 50_000}, "roomsTotal": 1,
                 "location": {"metro": {"name": "Парк культуры", **metro}}},
    ))


@pytest.mark.parametrize("mode", ["transport", None, "unknown"])
def test_cian_transport_and_unknown_modes_do_not_pass_walking_filter(mode):
    listing = _cian([{"name": "Парк культуры", "time": 4, "transportType": mode}])
    assert listing is not None and listing.metro == "Парк культуры"
    assert listing.metro_distance_min is None
    assert not matches(listing, UserFilter(user_id=1, max_metro_min=5))


def test_cian_prefers_confirmed_walking_station():
    listing = _cian([
        {"name": "Первая", "time": 2, "transportType": "transport"},
        {"name": "Парк культуры", "time": 5, "transportType": "walk"},
    ])
    assert listing is not None and listing.metro == "Парк культуры"
    assert listing.metro_distance_min == 5


@pytest.mark.parametrize("minutes,expected", [(0, 0), (4, 4), (5, 5), (5.5, 6), (6, 6),
                                               (None, None), (-1, None), (True, None)])
def test_walking_minutes_preserve_zero_and_reject_unknown_invalid(minutes, expected):
    cian = _cian([{"name": "Парк культуры", "time": minutes, "transportType": "walk"}])
    yandex = _yandex({"metroTransport": "ON_FOOT", "timeToMetro": minutes})
    assert cian is not None and cian.metro_distance_min == expected
    assert yandex is not None and yandex.metro_distance_min == expected


@pytest.mark.parametrize("mode", ["ON_TRANSPORT", None, "unknown"])
def test_yandex_transport_and_unknown_modes_do_not_pass_walking_filter(mode):
    listing = _yandex({"metroTransport": mode, "timeToMetro": 4})
    assert listing is not None and listing.metro == "Парк культуры"
    assert listing.metro_distance_min is None
    assert not matches(listing, UserFilter(user_id=1, max_metro_min=5))


@pytest.mark.parametrize("extra,expected", [
    ({"minTimeToMetro": 4}, None),
    ({"minTimeToMetro": 4, "maxTimeToMetro": 6}, 6),
    ({"timeToMetro": 4, "maxTimeToMetro": 6}, 6),
    ({"timeToMetro": 0, "minTimeToMetro": 5}, 0),
])
def test_yandex_ranges_use_upper_boundary(extra, expected):
    listing = _yandex({"metroTransport": "ON_FOOT", **extra})
    assert listing is not None and listing.metro_distance_min == expected
