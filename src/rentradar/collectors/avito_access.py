"""Доступ к Авито через мобильный прокси: куки браузера и смена IP.

С мобильного IP Авито не банит сразу, а отдаёт 439 «проверка безопасности» —
JS-челлендж, который curl не проходит. Настоящий Chromium через тот же прокси
проходит его за секунды; дальше curl_cffi с этими куками ходит как обычно.
Если же IP всё-таки помечен («проблема с IP»), дёргаем команду смены IP
(например, режим полёта на телефоне через adb) и прогреваемся заново.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WARM_URL = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg"
# Профиль curl_cffi под куки Chromium: другой TLS-отпечаток с теми же куками палится.
IMPERSONATE = "chrome"
# Чаще не пытаемся: если прогрев/смена IP не помогли, ждём обычную паузу источника.
_MIN_RECOVER_GAP = 120.0


class AvitoAccess:
    def __init__(
        self,
        *,
        proxy: str | None,
        cookies_path: Path,
        rotate_cmd: str = "",
        rotate_wait_sec: float = 15.0,
    ) -> None:
        self.proxy = proxy
        self._path = cookies_path
        self._rotate_cmd = rotate_cmd
        self._rotate_wait = rotate_wait_sec
        self._last_recover = 0.0
        self.cookies: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self.cookies), encoding="utf-8")

    async def warm(self) -> bool:
        """Пройти челлендж настоящим браузером через прокси и забрать куки."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("avito: playwright не установлен — прогрев недоступен")
            return False
        launch: dict = {"args": ["--disable-blink-features=AutomationControlled"]}
        if self.proxy:
            launch["proxy"] = {"server": self.proxy}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch)
                try:
                    ctx = await browser.new_context(
                        locale="ru-RU", viewport={"width": 1366, "height": 800}
                    )
                    page = await ctx.new_page()
                    await page.goto(WARM_URL, wait_until="domcontentloaded", timeout=60_000)
                    for _ in range(8):
                        if await page.locator("[data-marker=item]").count():
                            break
                        await page.wait_for_timeout(5_000)
                    else:
                        logger.warning("avito: прогрев — челлендж не пройден (%s)",
                                       await page.title())
                        return False
                    self.cookies = {
                        c["name"]: c["value"] for c in await ctx.cookies()
                        if "avito" in c.get("domain", "")
                    }
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001 — браузер/сеть: просто не прогрелись
            logger.warning("avito: прогрев упал: %s: %s", type(exc).__name__, exc)
            return False
        self._save()
        logger.info("avito: прогрев браузером ок, кук %d", len(self.cookies))
        return True

    async def rotate_ip(self) -> bool:
        if not self._rotate_cmd:
            return False
        proc = await asyncio.create_subprocess_shell(
            self._rotate_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            logger.warning("avito: смена IP — команда зависла")
            return False
        if proc.returncode != 0:
            logger.warning("avito: смена IP — код %s: %s", proc.returncode,
                           err.decode(errors="replace")[:200])
            return False
        await asyncio.sleep(self._rotate_wait)  # модем переподключается к сети
        logger.info("avito: IP сменён")
        return True

    async def recover(self, *, ip_banned: bool) -> bool:
        """Попробовать вернуть доступ. True — можно повторить запрос."""
        if time.monotonic() - self._last_recover < _MIN_RECOVER_GAP:
            return False
        self._last_recover = time.monotonic()
        if ip_banned and not await self.rotate_ip():
            return False
        return await self.warm()


_access: AvitoAccess | None = None


def configure(access: AvitoAccess | None) -> None:
    global _access
    _access = access


def current() -> AvitoAccess | None:
    return _access
