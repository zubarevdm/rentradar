"""Разведка DOM Авито через Playwright (headful, с ретраями).

Авито нестабилен даже через браузер — пробуем несколько раз. Когда страница
отрисовалась, печатаем структуру первой карточки (data-marker'ы, цена, ссылка).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok?s=104"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CARD_JS = r"""
() => {
  const c = document.querySelector("[data-marker=item]");
  if (!c) return null;
  const all = [...c.querySelectorAll("[data-marker]")].map(e => ({
    marker: e.getAttribute("data-marker"),
    tag: e.tagName,
    text: (e.textContent || "").trim().slice(0, 60),
    href: e.getAttribute("href"),
    content: e.getAttribute("content"),
  }));
  const price = c.querySelector("[itemprop=price]");
  return {
    itemId: c.getAttribute("data-item-id") || c.getAttribute("id"),
    priceContent: price ? price.getAttribute("content") : null,
    markers: all.slice(0, 40),
  };
}
"""


async def try_once(p) -> dict | None:  # noqa: ANN001
    browser = await p.chromium.launch(
        headless=False, args=["--disable-blink-features=AutomationControlled"]
    )
    ctx = await browser.new_context(
        user_agent=UA, locale="ru-RU", viewport={"width": 1366, "height": 900}
    )
    page = await ctx.new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector("[data-marker='item']", timeout=18000)
        n = len(await page.query_selector_all("[data-marker='item']"))
        info = await page.evaluate(CARD_JS)
        info["item_count"] = n
        return info
    except Exception:  # noqa: BLE001
        title = await page.title()
        print("  attempt blocked, title:", title)
        return None
    finally:
        await browser.close()


async def main() -> None:
    Path("fixtures/raw").mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        for attempt in range(1, 4):
            print(f"attempt {attempt}...")
            info = await try_once(p)
            if info:
                print("item_count:", info["item_count"], "priceContent:", info["priceContent"])
                print(json.dumps(info["markers"], ensure_ascii=False, indent=1))
                Path("fixtures/raw/avito_card.json").write_text(
                    json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                return
            await asyncio.sleep(3)
    print("Все попытки заблокированы.")


if __name__ == "__main__":
    asyncio.run(main())
