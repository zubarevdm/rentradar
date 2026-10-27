"""Current rental-section formats, including a transcription of the user's screenshot."""

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from selectolax.parser import HTMLParser

from rentradar.collectors.avito import _address_parts, parse_detail
from rentradar.models import Listing, Source


def page(terms):
    return f'<html><body><h2>Условия аренды</h2>{terms}<h2>Правила</h2></body></html>'


def test_empty_address_attributes_do_not_abort_collection():
    address = HTMLParser(
        '<div><a data-marker="street_link" title>Тестовая</a>'
        '<span aria-label="Пешком">Метро 5 мин</span></div>'
    ).body
    street, _, metro, minutes = _address_parts(address)
    assert street == "Тестовая" and metro == "Метро" and minutes == 5


@pytest.mark.parametrize("terms,deposit,pct,fixed", [
    ("Залог: 100000 ₽<br>Комиссия: 0 %<br>По счётчикам: оплачивается отдельно"
     "<br>Другие ЖКУ: включено в платёж", 100000, 0, None),
    ("Залог: 50\u00a0000 ₽<br>Комиссия: 50%", 50000, 50, None),
    ("Залог: 50\u202f000 руб.<br>Комиссия: 25\u202f001 ₽", 50000, None, 25001),
    ("Залог: 1 месяц<br>Комиссия: 50 %", 50000, 50, None),
    ("Залог: 2 месячных платежа<br>Комиссия: 0%", 100000, 0, None),
    ("Залог: 1,5 месяца<br>Без комиссии", 75000, 0, None),
    ("Залог в размере одного месячного платежа. Без комиссии", 50000, 0, None),
    ("Без залога. Комиссия: 25 001 руб.", 0, None, 25001),
    ("Залог: 0 ₽. Комиссия: 0", 0, 0, None),
    ("Залог: неизвестен. Комиссия: 50%", None, 50, None),
    ("Залог: от 50 000 ₽. Комиссия: 50%", None, 50, None),
    ("Залог: 1–2 месяца. Комиссия: уточняется", None, None, None),
    ("Залог: 50000 ₽. Комиссия: 50", 50000, None, None),
])
def test_rental_variants(terms, deposit, pct, fixed):
    detail = parse_detail(page(terms), price=50000)
    assert (detail.deposit_rub, detail.commission_pct, detail.commission_rub) == (
        deposit, pct, fixed,
    )


@pytest.mark.parametrize("escaped", [False, True])
def test_nested_embedded_state_and_recommendations(escaped):
    state = {"item": {"rentalTerms": {"params": [
        {"title": "Залог", "value": {"text": "2 месячных платежа"}},
        {"label": "Комиссия", "values": [{"text": "25 001", "unit": "₽"}]},
    ]}}, "recommendations": [{"deposit": 1, "commissionPercent": 99}]}
    content = json.dumps(state, ensure_ascii=True)
    if escaped:
        content = 'window.state = ' + json.dumps(content)
    detail = parse_detail(f'<script>{content}</script>', price=50000)
    assert detail.deposit_rub == 100000
    assert detail.commission_rub == 25001 and detail.commission_pct is None


def test_recommendation_terms_are_not_used_for_current_ad():
    content = json.dumps({"recommendations": [{"deposit": 100, "commissionPercent": 0}]})
    detail = parse_detail(f'<script>{content}</script>', price=50000)
    assert detail.deposit_rub is None and detail.commission_pct is None


def test_recommendation_fragments_in_non_json_script_are_not_used():
    html = (
        '<script>window.state={item: getCurrent(), recommendations: '
        '[{"label":"Залог","value":"50000 ₽"},'
        '{"label":"Комиссия","value":"0 %"}]};</script>'
    )
    detail = parse_detail(html, price=50000)
    assert detail.deposit_rub is None and detail.commission_pct is None


def test_closed_rental_section_and_nonrecurring_costs():
    detail = parse_detail(page(
        '<details><summary>Условия</summary><p>Залог: 50000 ₽</p><p>Комиссия: 50%</p>'
        '<p>Другие ЖКУ: 7000 ₽ отдельно</p></details>'
    ), price=50000)
    assert detail.deposit_rub == 50000 and detail.commission_pct == 50
    assert detail.utilities_rub == 7000 and not detail.upfront_payments


def test_monthly_deposit_without_rent_is_unknown():
    detail = parse_detail(page('Залог: 2 месяца. Без комиссии'))
    assert detail.deposit_rub is None and detail.commission_pct == 0


def test_explicit_rent_advance():
    detail = parse_detail(page(
        'Без залога. Без комиссии. Оплата за первый и последний месяц при заезде.'
    ), price=50000)
    assert detail.upfront_payments == {"арендный аванс за последний месяц": 50000}


@pytest.mark.parametrize("terms,due,total", [
    (
        "Залог: 60000 ₽. Комиссия: 50%. Обязательный сбор при заезде 1500 ₽.",
        None, 136500,
    ),
    (
        "Залог: 60000 ₽ в рассрочку. Комиссия: 0%. "
        "Обязательный сбор при заезде 1500 ₽.", None, None,
    ),
    (
        "Залог: 60000 ₽ в рассрочку, при заезде 20000 ₽. Комиссия: 50%. "
        "Обязательный сбор при заезде 1500 ₽.", 20000, 96500,
    ),
])
def test_entry_fee_cannot_become_deposit_installment(terms, due, total):
    detail = parse_detail(page(terms), price=50000)
    listing = Listing(
        source=Source.AVITO, external_id="test", url="https://example.com/test",
        price=50000, city="Москва", collected_at=datetime(2026, 10, 26),
        deposit_rub=detail.deposit_rub, deposit_due_rub=detail.deposit_due_rub,
        deposit_installments=detail.deposit_installments,
        commission_pct=detail.commission_pct, commission_rub=detail.commission_rub,
        upfront_payments=detail.upfront_payments or {},
    )
    assert detail.deposit_due_rub == due
    assert detail.upfront_payments == {"обязательный сбор": 1500}
    assert listing.move_in_total == total


def test_explicitly_unknown_terms_do_not_inherit_description_zero():
    detail = parse_detail(page(
        "Залог: уточняется. Комиссия: уточняется."
    ).replace("</body>",
        '<div data-marker="item-view/item-description">'
        "Без залога, без комиссии.</div></body>"), price=50000)
    assert detail.deposit_rub is None and detail.commission_pct is None


def test_discussed_advance_does_not_create_false_total():
    detail = parse_detail(page(
        "Залог: 50000 ₽. Комиссия: 0%. "
        "Оплата за первый и последний месяц обсуждается после просмотра."
    ), price=50000)
    assert detail.upfront_payments == {"арендный аванс за последний месяц": None}


async def test_fetch_detail_does_not_accept_200_challenge(monkeypatch):
    from rentradar.collectors import avito

    fetch = AsyncMock(return_value=(
        "<html><title>Проверка безопасности</title>"
        "<body><h1>Подтвердите, что вы не робот</h1></body></html>"
    ))
    monkeypatch.setattr(avito, "_avito_fetch_html", fetch)
    assert await avito.fetch_detail("https://www.avito.ru/test", price=50000) is None
    fetch.assert_awaited_once()


async def test_fetch_detail_accepts_structured_only_rental_terms(monkeypatch):
    from rentradar.collectors import avito

    html = '<script type="application/json">' + json.dumps({
        "item": {"params": [
            {"label": "Залог", "value": "50000 ₽"},
            {"label": "Комиссия", "value": "50%"},
        ]}
    }, ensure_ascii=False) + "</script>"
    monkeypatch.setattr(avito, "_avito_fetch_html", AsyncMock(return_value=html))
    detail = await avito.fetch_detail("https://www.avito.ru/test", price=50000)
    assert detail is not None
    assert detail.deposit_rub == 50000 and detail.commission_pct == 50


async def test_fetch_detail_rejects_recommendations_only(monkeypatch):
    from rentradar.collectors import avito

    html = '<script type="application/json">' + json.dumps({
        "recommendations": [{"label": "Залог", "value": "50000 ₽"}],
    }, ensure_ascii=False) + "</script>"
    monkeypatch.setattr(avito, "_avito_fetch_html", AsyncMock(return_value=html))
    assert await avito.fetch_detail("https://www.avito.ru/test", price=50000) is None


@pytest.mark.parametrize("duration,expected", [
    ('4 минуты пешком', 4), ('до 5 минут пешком', 5),
    ('4–5 минут пешком', 5), ('6 минут пешком', 6),
    ('от 5 минут пешком', None), ('4 минуты на транспорте', None),
    ('4 минуты', None),
    ('<span aria-label="Пешком">5 минут</span>', 5),
])
def test_avito_only_confirmed_walking_duration(duration, expected):
    tree = HTMLParser(
        '<div data-marker="item-address"><span data-marker="street_link">улица X</span>'
        '<span data-marker="house_link">1</span>Тестовая ' + duration + '</div>'
    )
    _, _, station, minutes = _address_parts(tree.css_first('div'))
    assert station == "Тестовая" and minutes == expected
