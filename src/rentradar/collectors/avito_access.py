"""Доступ к Авито через мобильный прокси: доверенная сессия браузера и смена IP.

Авито пускает не «чистых» новичков, а сессии с историей: с того же мобильного IP
браузер телефона с давними куками проходит, а инкогнито получает «проблему с IP».
Поэтому держим ОДИН постоянный профиль Camoufox (Firefox против антиботов, с
целостным отпечатком ПК): он проходит «проверку браузера» и копит доверие. Частый
опрос выдачи идёт быстрым curl_cffi с куками и User-Agent этого профиля. При
блоке — прогрев тем же профилем; при «проблеме с IP» — ещё и смена IP (режим
полёта на телефоне через adb).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WARM_URL = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg"
# TLS-профиль curl_cffi под браузер профиля (Camoufox = Firefox).
IMPERSONATE = "firefox135"
# Чаще не пытаемся: если прогрев/смена IP не помогли, ждём обычную паузу источника.
_MIN_RECOVER_GAP = 120.0
# Куки прогрева обычно живут ~15 мин. Если блок пришёл раньше — подозреваем IP: меняем его.
_WARM_HOLD_SEC = 600.0


class AvitoAccess:
    def __init__(
        self,
        *,
        proxy: str | None,
        cookies_path: Path,
        profile_dir: Path | None = None,
        rotate_cmd: str = "",
        rotate_wait_sec: float = 15.0,
    ) -> None:
        self.proxy = proxy
        self._path = cookies_path
        self._profile = profile_dir or cookies_path.with_name("avito_browser_profile")
        self._rotate_cmd = rotate_cmd
        self._rotate_wait = rotate_wait_sec
        self._last_recover = 0.0
        self._last_warm_ok = 0.0
        self.user_agent = ""
        self.cookies: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.user_agent = data.get("ua", "")
        self.cookies = data.get("cookies", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"ua": self.user_agent, "cookies": self.cookies}), encoding="utf-8"
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent} if self.user_agent else {}

    async def warm(self) -> bool:
        """Пройти «проверку браузера» постоянным профилем Camoufox и забрать куки."""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            logger.warning("avito: camoufox не установлен — прогрев недоступен")
            return False
        options: dict = {
            "headless": "virtual",  # окно в виртуальном дисплее (Xvfb), не headless
            "os": "windows",
            "locale": "ru-RU",
            "humanize": True,
            "persistent_context": True,
            "user_data_dir": str(self._profile),
            "i_know_what_im_doing": True,  # Windows-отпечаток на Linux-сервере — намеренно
        }
        if self.proxy:
            options |= {"proxy": {"server": self.proxy}, "geoip": True}
        try:
            async with AsyncCamoufox(**options) as ctx:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(WARM_URL, wait_until="domcontentloaded", timeout=60_000)
                for _ in range(12):
                    if await page.locator("[data-marker=item]").count():
                        break
                    await page.wait_for_timeout(5_000)
                else:
                    logger.warning("avito: прогрев — проверка не пройдена (%s)",
                                   await page.title())
                    return False
                self.user_agent = await page.evaluate("navigator.userAgent")
                self.cookies = {
                    c["name"]: c["value"] for c in await ctx.cookies()
                    if "avito" in c.get("domain", "")
                }
        except Exception as exc:  # noqa: BLE001 — браузер/сеть: просто не прогрелись
            logger.warning("avito: прогрев упал: %s: %s", type(exc).__name__, exc)
            return False
        self._save()
        self._last_warm_ok = time.monotonic()
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

    async def recover(self, *, ip_banned: bool, retry: bool = False) -> bool:
        """Попробовать вернуть доступ. True — можно повторить запрос.

        retry — повтор в том же запросе после неудачного прогрева: паузу не ждём
        и сразу меняем IP. Блок вскоре после удачного прогрева тоже считаем
        проблемой IP: свежие куки не держатся — прогревать на нём бесполезно."""
        now = time.monotonic()
        if not retry and now - self._last_recover < _MIN_RECOVER_GAP:
            return False
        self._last_recover = now
        held = now - self._last_warm_ok
        rotate = ip_banned or retry or (self._last_warm_ok and held < _WARM_HOLD_SEC)
        if rotate:
            if not ip_banned and not retry:
                logger.info("avito: блок через %.0f с после прогрева — меняем IP", held)
            if not await self.rotate_ip() and (ip_banned or retry):
                return False
        return await self.warm()


async def _run_ok(cmd: str, timeout: float = 30.0) -> bool:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout) == 0
    except TimeoutError:
        proc.kill()
        return False


async def _proxy_ok(proxy: str) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=20) as client:
            return (await client.get("https://api.ipify.org")).status_code == 200
    except httpx.HTTPError:
        return False


class PhoneMonitor:
    """Здоровье телефона-прокси: сам прокси и управление (adb для смены IP).

    Сбой засчитываем после `fail_after` подряд проверок — смена IP рвёт связь
    на полминуты, это не повод будить админа. Сообщаем смену состояния и, пока
    часть лежит, напоминаем раз в `remind_sec` (one-shot алерт легко пропустить).
    """

    def __init__(
        self, *, proxy: str, control_cmd: str = "", fail_after: int = 3,
        remind_sec: float = 7200.0,
    ) -> None:
        self._checks = {"proxy": lambda: _proxy_ok(proxy)}
        if control_cmd:
            self._checks["control"] = lambda: _run_ok(control_cmd)
        self._fail_after = fail_after
        self._fails = dict.fromkeys(self._checks, 0)
        self._down: dict[str, float] = {}  # часть -> когда последний раз сообщили
        self._remind = remind_sec

    async def check(self) -> list[tuple[str, bool]]:
        """[(часть, теперь_работает)] — смены состояния и напоминания о лежащих."""
        changes: list[tuple[str, bool]] = []
        now = time.monotonic()
        for name, probe in self._checks.items():
            if await probe():
                self._fails[name] = 0
                if self._down.pop(name, None) is not None:
                    changes.append((name, True))
                continue
            self._fails[name] += 1
            if self._fails[name] < self._fail_after:
                continue
            last = self._down.get(name)
            if last is None or now - last >= self._remind:
                self._down[name] = now
                changes.append((name, False))
        return changes


_access: AvitoAccess | None = None


def configure(access: AvitoAccess | None) -> None:
    global _access
    _access = access


def current() -> AvitoAccess | None:
    return _access
