"""Разведка Авито через Playwright.

Открываем страницу поиска в headless-браузере, перехватываем XHR-ответы и ищем
тот, что содержит массив объявлений. Печатаем URL эндпоинта и ключи оффера,
сохраняем сырой JSON для построения нормализатора.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok?pmin=30000&pmax=70000&s=104"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def main() -> None:
    out = Path("fixtures/raw")
    out.mkdir(parents=True, exist_ok=True)
    captured: list[tuple[str, object]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent=UA, locale="ru-RU", viewport={"width": 1366, "height": 900}
        )
        page = await ctx.new_page()

        async def on_response(resp):  # noqa: ANN001
            ct = resp.headers.get("content-type", "")
            if "application/json" not in ct:
                return
            try:
                data = await resp.json()
            except Exception:  # noqa: BLE001
                return
            text = json.dumps(data, ensure_ascii=False)
            if '"items"' in text or '"catalog"' in text or '"priceDetailed"' in text:
                captured.append((resp.url, data))

        page.on("response", on_response)

        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector("[data-marker='item']", timeout=20000)
            print("listings rendered: YES")
        except Exception:  # noqa: BLE001
            print("listings rendered: NO (возможно капча/блок)")

        title = await page.title()
        body = (await page.content()).lower()
        print("page title:", title)
        markers = ("доступ ограничен", "captcha", "robot")
        print("blocked markers:", [m for m in markers if m in body])

        await browser.close()

    print("captured JSON responses:", len(captured))
    for url, data in captured:
        items = _find_items(data)
        print(" url:", url[:90], "| items:", None if items is None else len(items))
        if items:
            print("  offer keys:", sorted(items[0].keys())[:40])
            (out / "avito_items_sample.json").write_text(
                json.dumps(items[:3], ensure_ascii=False, indent=1), encoding="utf-8"
            )
            print("  saved fixtures/raw/avito_items_sample.json")
            break


def _find_items(node: object) -> list | None:
    if isinstance(node, dict):
        for k, v in node.items():
            if (
                k in ("items", "catalog")
                and isinstance(v, list)
                and v
                and isinstance(v[0], dict)
                and ("id" in v[0] or "priceDetailed" in v[0])
            ):
                return v
            r = _find_items(v)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_items(v)
            if r is not None:
                return r
    return None


if __name__ == "__main__":
    asyncio.run(main())
