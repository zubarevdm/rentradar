"""Regression coverage for rental details, persistence, and the expanded rail catalogue."""
from collections import Counter
from datetime import datetime

import pytest

from rentradar.collectors.avito import parse_detail
from rentradar.models import ScoreBreakdown, ScoredListing, Source
from rentradar.personal import PersonalStore, UserFilter
from rentradar.personal.filters import matches
from rentradar.personal.metro import EDGES, STATIONS, search_stations
from rentradar.publisher import TelegramPublisher
from rentradar.storage import SqlStorage

NOW = datetime(2026, 10, 25, 12)


def rental_html(terms):
    return ('<html><body><h2>Условия аренды</h2>' + terms
            + '<h2>Правила</h2><p>Количество жильцов: 3</p></body></html>')


def listing_detail(sample_listing, terms):
    detail = parse_detail(rental_html(terms), price=60000)
    values = detail._asdict()
    values.pop('photos')
    values['upfront_payments'] = values['upfront_payments'] or {}
    return sample_listing.model_copy(update={'price': 60000, **values})


@pytest.mark.parametrize('terms,deposit,pct,fixed,total', [
    ('Залог: 60 000 ₽<br>Комиссия: 50 %', 60000, 50, None, 150000),
    ('Залог: 60000 ₽<br>Комиссия: 30 001 руб.', 60000, None, 30001, 150001),
    ('Без залога, без комиссии', 0, 0, None, 60000),
    ('Залог: 0 ₽<br>Комиссия: 0', 0, 0, None, 60000),
    ('Залог: 60000 ₽', 60000, None, None, None),
    ('Комиссия: 50%', None, 50, None, None),
    ('', None, None, None, None),
])
def test_rental_conditions(sample_listing, terms, deposit, pct, fixed, total):
    listing = listing_detail(sample_listing, terms)
    assert (listing.deposit_rub, listing.commission_pct, listing.commission_rub) == (
        deposit, pct, fixed
    )
    assert listing.move_in_total == total
    text = TelegramPublisher(dry_run=True).render(ScoredListing(
        listing=listing, score=ScoreBreakdown(total=70, signals=[])
    ))
    if total is None:
        assert 'Известные платежи при заезде:' in text and 'Не указаны:' in text
        assert '<b>При заезде:' not in text


def test_installments_and_mandatory_fees(sample_listing):
    listing = listing_detail(sample_listing,
        'Залог: 60000 ₽ в рассрочку, при заезде 20000 ₽. Комиссия: 50%. '
        'Обязательный сбор при заезде 1500 ₽. ЖКУ включены.')
    assert listing.deposit_rub == 60000 and listing.deposit_due == 20000
    assert listing.move_in_total == 111500
    assert listing.upfront_payments == {'обязательный сбор': 1500}
    unknown = listing_detail(sample_listing,
        'Залог: 60000 ₽ в рассрочку. Комиссия: 0%.')
    assert unknown.deposit_installments and unknown.deposit_due is None
    assert unknown.move_in_total is None
    split = listing_detail(sample_listing,
        'Залог: 60000 ₽, можно разбить на два платежа. Комиссия: 0%.')
    assert split.deposit_installments and split.move_in_total is None


def test_structured_json_rental_conditions(sample_listing):
    detail = parse_detail('''<script type="application/json">{"params":[
        {"label":"Залог", "value":"60000 ₽"},
        {"label":"Комиссия", "value":"50%"},
        {"label":"По счётчикам", "value":"оплачивается отдельно"},
        {"label":"Другие ЖКУ", "value":"включено в платёж"}]}</script>''', price=60000)
    assert (detail.deposit_rub, detail.commission_pct) == (60000, 50)
    assert detail.meters_included is False and detail.utilities_rub == 0


async def test_html_storage_render_chain(tmp_path, sample_listing):
    storage = SqlStorage(f'sqlite+aiosqlite:///{(tmp_path / "terms.db").as_posix()}')
    await storage.init()
    listing = sample_listing.model_copy(update={'price': 60000, 'source': Source.AVITO})
    await storage.upsert_listings([listing])
    detail = parse_detail(rental_html(
        '<p>Залог: 60000 ₽</p><p>Комиссия: 50 %</p>'
        '<p>По счётчикам: оплачивается отдельно</p>'
        '<p>Другие ЖКУ: включено в платёж</p>'
    ), price=listing.price)
    await storage.save_enrichment('avito', listing.external_id, enriched_at=NOW,
                                  **detail._asdict())
    saved = (await storage.recent_listings('Москва', datetime(2020, 1, 1)))[0]
    assert saved.move_in_total == 150000
    assert saved.utilities_rub == 0 and saved.meters_included is False
    text = TelegramPublisher(dry_run=True).render(ScoredListing(
        listing=saved, score=ScoreBreakdown(total=70, signals=[])
    ))
    assert 'При заезде: 150 000 ₽' in text
    assert 'первый месяц 60 000 ₽ + залог 60 000 ₽ + комиссия 30 000 ₽ (50%)' in text
    assert 'ЖКУ включены' in text and 'счётчики' in text
    await storage.dispose()


@pytest.mark.parametrize('query,line', [
    ('Котельники', '7'), ('Локомотив', '14'), ('Лобня', 'D1'),
    ('Подольск', 'D2'), ('Ипподром', 'D3'), ('ЧУХЛИНКА', 'D4'),
    ('Бульвар Генерала Карбышева', '17'),
])
async def test_search_save_and_apply_station(tmp_path, sample_listing, query, line):
    station = next(s for s in search_stations(query) if s.line == line)
    storage = SqlStorage(f'sqlite+aiosqlite:///{(tmp_path / "filter.db").as_posix()}')
    await storage.init()
    personal = PersonalStore(storage)
    await personal.upsert_filter(UserFilter(user_id=1, metros=[station.key]))
    _, saved = (await personal.list_filters(1))[0]
    assert saved.metros == [station.key]
    listing = sample_listing.model_copy(update={'metro': station.name, 'metro_line': line})
    assert matches(listing, saved)
    assert not matches(listing.model_copy(update={'metro_line': 'wrong'}), saved)
    await storage.dispose()


def test_catalogue_search_and_topology():
    assert Counter(s.line for s in STATIONS.values()) == {
        '1': 27, '2': 24, '3': 22, '4': 13, '5': 12, '6': 24, '7': 23,
        '8': 8, '8a': 14, '9': 25, '10': 26, '11': 29, '12': 7, '14': 31,
        '15': 8, '16': 11, '17': 5, 'D1': 26, 'D2': 37, 'D3': 39, 'D4': 36,
    }
    # Every catalogue entry is searchable and connected only to its own route.
    for station in STATIONS.values():
        assert station in search_stations(station.name.upper().replace('Ё', 'Е'))
        assert EDGES[station.id]
        assert all(STATIONS[n].line == station.line for n in EDGES[station.id])
    assert {s.line for s in STATIONS.values() if s.line.startswith('D')} == {
        'D1', 'D2', 'D3', 'D4'
    }
    assert len([s for s in STATIONS.values() if s.line == '14']) == 31
    assert next(s for s in search_stations('Каланчевская') if s.line == 'D2').name == (
        'Площадь трёх вокзалов'
    )
    assert next(s for s in search_stations('Тестовская') if s.line == 'D1').name == 'Москва-Сити'
    assert not search_stations('Дмитровская · D1')  # future node must not be offered


def test_ambiguous_station_and_legacy_filter(sample_listing):
    flt = UserFilter(user_id=1, metros=['Перово · D3'])
    assert matches(sample_listing.model_copy(update={'metro':'Перово', 'metro_line':'D3'}), flt)
    assert not matches(sample_listing.model_copy(update={'metro':'Перово', 'metro_line':'8'}), flt)
    assert not matches(sample_listing.model_copy(update={'metro':'Перово'}), flt)
    flt.metros = ['Перово']
    assert matches(sample_listing.model_copy(update={'metro':'Перово'}), flt)


async def test_legacy_terms_migration_once(tmp_path, sample_listing):
    from sqlalchemy import text

    storage = SqlStorage(f'sqlite+aiosqlite:///{(tmp_path / "legacy.db").as_posix()}')
    await storage.init()
    listing = sample_listing.model_copy(update={
        'source': Source.AVITO, 'enriched_at': NOW, 'deposit_rub': 60000,
    })
    await storage.upsert_listings([listing])
    async with storage._engine.begin() as conn:
        await conn.execute(text("DELETE FROM data_versions WHERE name='rental_terms_v3'"))
        for column in ('commission_rub', 'deposit_due_rub', 'deposit_installments',
                       'upfront_payments', 'utilities_included', 'metro_line'):
            await conn.execute(text(f'ALTER TABLE listings DROP COLUMN {column}'))
    await storage.init()
    saved = (await storage.recent_listings('Москва', datetime(2020, 1, 1)))[0]
    assert saved.enriched_at is None and saved.deposit_rub is None
    assert saved.upfront_payments == {} and saved.deposit_installments is False
    await storage.save_enrichment('avito', listing.external_id, enriched_at=NOW,
                                  commission_rub=30001, deposit_due_rub=20000,
                                  deposit_installments=True,
                                  upfront_payments={'оформление': 1500}, utilities_rub=0,
                                  utilities_included=True, meters_included=False)
    await storage.init()  # no requeue or loss on subsequent startup
    saved = (await storage.recent_listings('Москва', datetime(2020, 1, 1)))[0]
    assert saved.enriched_at == NOW
    assert saved.commission_rub == 30001 and saved.deposit_due == 20000
    assert saved.upfront_payments == {'оформление': 1500}
    assert saved.utilities_included is True and saved.meters_included is False
    await storage.dispose()
