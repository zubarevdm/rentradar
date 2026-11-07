import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rentradar.collectors import avito, avito_access


class FakeAccess(avito_access.AvitoAccess):
    def __init__(self, tmp_path):
        super().__init__(proxy="http://phone:1056", cookies_path=tmp_path / "c.json")
        self.recovered: list[bool] = []

    async def recover(self, *, ip_banned: bool) -> bool:
        self.recovered.append(ip_banned)
        self.cookies = {"ok": "1"}
        return True


def _resp(status: int, text: str):
    return SimpleNamespace(status_code=status, text=text, headers={})


@pytest.mark.parametrize("status,body,ip_banned", [
    (429, "Доступ ограничен: проблема с IP", True),
    (439, "проверка безопасности", False),
    (403, "Доступ ограничен: проблема с IP", True),
])
async def test_block_waits_before_recovery_and_probe(
    monkeypatch, tmp_path, status, body, ip_banned,
):
    access = FakeAccess(tmp_path)
    avito_access.configure(access)
    clock = [1000.0]
    monkeypatch.setattr(avito.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(avito, "_block_cooldown_seconds", 300)
    request = Mock(side_effect=[_resp(status, body), _resp(200, "ok")])
    monkeypatch.setattr(avito, "_blocking_get", request)
    assert await avito._avito_fetch_html("https://www.avito.ru/x", proxy=access.proxy) is None
    assert request.call_count == 1 and access.recovered == []
    clock[0] += 299
    assert await avito._avito_fetch_html("https://www.avito.ru/y", proxy=access.proxy) is None
    assert request.call_count == 1 and access.recovered == []
    clock[0] += 2
    assert await avito._avito_fetch_html("https://www.avito.ru/x", proxy=access.proxy) == "ok"
    assert request.call_count == 2 and access.recovered == [ip_banned]


async def test_repeated_refusal_extends_shared_pause(monkeypatch, tmp_path):
    access = FakeAccess(tmp_path)
    avito_access.configure(access)
    clock = [1000.0]
    monkeypatch.setattr(avito.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(avito, "_block_cooldown_seconds", 300)
    request = Mock(return_value=_resp(429, "проблема с IP"))
    monkeypatch.setattr(avito, "_blocking_get", request)
    await avito._avito_fetch_html("https://www.avito.ru/search", proxy=access.proxy)
    clock[0] += 301
    await avito._avito_fetch_html("https://www.avito.ru/search", proxy=access.proxy)
    clock[0] += 599
    await avito._avito_fetch_html("https://www.avito.ru/detail", proxy=access.proxy)
    assert request.call_count == 2
    clock[0] += 2
    await avito._avito_fetch_html("https://www.avito.ru/search", proxy=access.proxy)
    assert request.call_count == 3


@pytest.mark.parametrize("retry", ["900", "Thu, 01 Jan 1970 00:31:40 GMT"])
async def test_mobile_transport_respects_retry_after(monkeypatch, tmp_path, retry):
    access = FakeAccess(tmp_path)
    avito_access.configure(access)
    clock = [1000.0]
    monkeypatch.setattr(avito.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(avito.time, "time", lambda: clock[0])
    monkeypatch.setattr(avito, "_block_cooldown_seconds", 300)
    blocked = _resp(429, "проблема с IP")
    blocked.headers = {"Retry-After": retry}
    request = Mock(side_effect=[blocked, _resp(200, "ok")])
    monkeypatch.setattr(avito, "_blocking_get", request)
    await avito._avito_fetch_html("https://www.avito.ru/search", proxy=access.proxy)
    clock[0] += 899
    assert await avito._avito_fetch_html("https://www.avito.ru/detail", proxy=access.proxy) is None
    assert request.call_count == 1
    clock[0] += 2
    assert await avito._avito_fetch_html("https://www.avito.ru/search", proxy=access.proxy) == "ok"


def test_recover_is_rate_limited(tmp_path):
    access = avito_access.AvitoAccess(proxy=None, cookies_path=tmp_path / "c.json")
    calls = []

    async def warm():
        calls.append(1)
        return True

    access.warm = warm
    assert asyncio.run(access.recover(ip_banned=False))
    assert not asyncio.run(access.recover(ip_banned=False))
    assert calls == [1]


async def test_avito_waits_for_shared_phone_request(monkeypatch, tmp_path):
    access = FakeAccess(tmp_path)
    avito_access.configure(access)
    request = Mock(return_value=_resp(200, "ok"))
    monkeypatch.setattr(avito, "_blocking_get", request)
    async with avito_access.proxy_lock(access.proxy):
        task = asyncio.create_task(avito._avito_fetch_html(
            "https://www.avito.ru/search", proxy=access.proxy,
        ))
        await asyncio.sleep(0)
        assert not task.done() and request.call_count == 0
    assert await asyncio.wait_for(task, 2) == "ok"


def test_phone_monitor_alerts_only_on_sustained_change():
    mon = avito_access.PhoneMonitor(proxy="http://phone:1056", fail_after=3)
    results = iter([False, False, True, False, False, False, False, True])

    async def probe():
        return next(results)

    mon._checks = {"proxy": probe}
    seen = [asyncio.run(mon.check()) for _ in range(8)]
    # Р”РІР° СЃР±РѕСЏ Рё РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ вЂ” С‚РёС€РёРЅР°; С‚СЂРё РїРѕРґСЂСЏРґ вЂ” Р°Р»РµСЂС‚ РѕРґРёРЅ СЂР°Р·; РІРѕР·РІСЂР°С‚ вЂ” Р°Р»РµСЂС‚.
    assert seen == [[], [], [], [], [], [("proxy", False)], [], [("proxy", True)]]


def test_session_roundtrip_keeps_user_agent(tmp_path):
    path = tmp_path / "c.json"
    a = avito_access.AvitoAccess(proxy=None, cookies_path=path)
    a.user_agent, a.cookies = "Firefox/158.0", {"u": "1"}
    a._save()
    b = avito_access.AvitoAccess(proxy=None, cookies_path=path)
    assert b.headers == {"User-Agent": "Firefox/158.0"} and b.cookies == {"u": "1"}
