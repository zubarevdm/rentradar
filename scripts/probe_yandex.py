"""Разведка структуры Яндекс Недвижимости.

Данные встроены в HTML страницы поиска как `window.INITIAL_STATE = {...}`.
Скрипт извлекает этот JSON, находит массив офферов и печатает ключи + пример.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def extract_initial_state(html: str) -> dict:
    m = re.search(r"window\.INITIAL_STATE\s*=\s*", html)
    if not m:
        raise SystemExit("INITIAL_STATE не найден")
    start = m.end()
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        c = html[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    raise SystemExit("не удалось сбалансировать скобки")


def find_offer_entities(node: object, path: str = "") -> list | None:
    if isinstance(node, dict):
        for k, v in node.items():
            if (
                k == "entities"
                and isinstance(v, list)
                and v
                and isinstance(v[0], dict)
                and ("offerId" in v[0] or "price" in v[0])
            ):
                print(f"FOUND entities at {path}/{k} (len={len(v)})")
                return v
            r = find_offer_entities(v, f"{path}/{k}")
            if r is not None:
                return r
    elif isinstance(node, list):
        for idx, v in enumerate(node):
            r = find_offer_entities(v, f"{path}[{idx}]")
            if r is not None:
                return r
    return None


def main() -> None:
    html = Path("fixtures/raw/yandex_page.html").read_text(encoding="utf-8")
    data = extract_initial_state(html)
    print("top keys:", list(data.keys()))
    offers = find_offer_entities(data)
    if offers:
        print("offer[0] keys:", sorted(offers[0].keys()))
        out = Path("fixtures/raw/yandex_offers_sample.json")
        out.write_text(json.dumps(offers[:3], ensure_ascii=False, indent=1), encoding="utf-8")
        print("saved:", out)


if __name__ == "__main__":
    main()
