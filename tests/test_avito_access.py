import asyncio
from types import SimpleNamespace

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


def _run(monkeypatch, tmp_path, responses):
    access = FakeAccess(tmp_path)
    avito_access.configure(access)
    avito._blocked_until.clear()
    seen: list[dict | None] = []

    def fake_get(url, imp, params, proxy, timeout, cookies=None):
        seen.append(dict(cookies or {}))
        return responses.pop(0)

    monkeypatch.setattr(avito, "_blocking_get", fake_get)
    try:
        html = asyncio.run(avito._avito_fetch_html("https://www.avito.ru/x", proxy=access.proxy))
    finally:
        avito_access.configure(None)
    return html, access, seen


def test_challenge_warms_and_retries_with_cookies(monkeypatch, tmp_path):
    html, access, seen = _run(monkeypatch, tmp_path, [
        _resp(439, "Доступ ограничен: проверка безопасности"), _resp(200, "<html>ok</html>"),
    ])
    assert html == "<html>ok</html>"
    assert access.recovered == [False]
    assert seen[-1] == {"ok": "1"}


def test_ip_ban_requests_rotation(monkeypatch, tmp_path):
    html, access, _ = _run(monkeypatch, tmp_path, [
        _resp(403, "Доступ ограничен: проблема с IP"), _resp(200, "ok"),
    ])
    assert html == "ok" and access.recovered == [True]


def test_gives_up_after_one_recovery(monkeypatch, tmp_path):
    html, access, _ = _run(monkeypatch, tmp_path, [
        _resp(439, "проверка безопасности"), _resp(439, "проверка безопасности"),
    ])
    assert html is None and access.recovered == [False]
    assert avito._blocked_until["http://phone:1056"] > 0


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


def test_phone_monitor_alerts_only_on_sustained_change():
    mon = avito_access.PhoneMonitor(proxy="http://phone:1056", fail_after=3)
    results = iter([False, False, True, False, False, False, False, True])

    async def probe():
        return next(results)

    mon._checks = {"proxy": probe}
    seen = [asyncio.run(mon.check()) for _ in range(8)]
    # Два сбоя и восстановление — тишина; три подряд — алерт один раз; возврат — алерт.
    assert seen == [[], [], [], [], [], [("proxy", False)], [], [("proxy", True)]]
