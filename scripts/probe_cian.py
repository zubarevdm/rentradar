"""Разведочный запрос к внутреннему API Циан.

Цель — увидеть актуальную структуру ответа и сохранить сырую фикстуру, чтобы
писать нормализатор под реальные данные, а не вслепую. Запуск:

    python scripts/probe_cian.py
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

API = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"

# Москва = регион 1. Долгосрочная аренда 1-2 комнатных квартир, 30-70к.
JSON_QUERY = {
    "jsonQuery": {
        "_type": "flatrent",
        "engine_version": {"type": "term", "value": 2},
        "region": {"type": "terms", "value": [1]},
        "for_day": {"type": "term", "value": "!1"},
        "room": {"type": "terms", "value": [1, 2]},
        "price": {"type": "range", "value": {"gte": 30000, "lte": 70000}},
        "page": {"type": "term", "value": 1},
    }
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.cian.ru",
    "Referer": "https://www.cian.ru/",
}


def main() -> None:
    out_dir = Path("fixtures/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        resp = httpx.post(API, json=JSON_QUERY, headers=HEADERS, timeout=25.0)
        print("HTTP", resp.status_code)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — диагностический скрипт
        print("FAILED:", type(exc).__name__, exc)
        return

    print("top-level keys:", list(data.keys()))
    offers = data.get("data", {}).get("offersSerialized", [])
    print("offers count:", len(offers))
    if offers:
        first = offers[0]
        print("offer[0] keys:", sorted(first.keys()))
        path = out_dir / "cian_search_sample.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved:", path)


if __name__ == "__main__":
    main()
