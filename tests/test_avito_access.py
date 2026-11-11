import asyncio
from types import SimpleNamespace

from rentradar.collectors import avito, avito_access


class FakeAccess(avito_access.AvitoAccess):
    def __init__(self, tmp_path):
        super().__init__(proxy="http://phone:1056", cookies_path=tmp_path / "c.json")
        self.recovered: list[bool] = []
        self.retries: list[bool] = []

    async def recover(self, *, ip_banned: bool, retry: bool = False) -> bool:
        self.recovered.append(ip_banned)
        self.retries.append(retry)
        self.cookies = {"ok": "1"}
        return True


def _resp(status: int, text: str):
    return SimpleNamespace(status_code=status, text=text, headers={})


def _run(monkeypatch, tmp_path, responses):
    access = FakeAccess(tmp_path)
    avito_access.configure(access)
    avito._blocked_until.clear()
    seen: list[dict | None] = []

    def fake_get(url, imp, params, proxy, timeout, cookies=None, headers=None):
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


def test_second_block_retries_on_new_ip(monkeypatch, tmp_path):
    html, access, _ = _run(monkeypatch, tmp_path, [
        _resp(439, "проверка безопасности"), _resp(439, "проверка безопасности"),
        _resp(200, "ok"),
    ])
    assert html == "ok" and access.retries == [False, True]


def test_gives_up_after_two_recoveries(monkeypatch, tmp_path):
    html, access, _ = _run(monkeypatch, tmp_path, [_resp(439, "проверка безопасности")] * 3)
    assert html is None and access.retries == [False, True]
    assert avito._blocked_until["http://phone:1056"] > 0


def _counting_access(tmp_path):
    access = avito_access.AvitoAccess(
        proxy=None, cookies_path=tmp_path / "c.json", rotate_cmd="true", rotate_wait_sec=0,
    )
    calls: list[str] = []

    async def warm():
        calls.append("warm")
        access._last_warm_ok = avito_access.time.monotonic()
        return True

    async def rotate():
        calls.append("rotate")
        return True

    access.warm, access.rotate_ip = warm, rotate
    return access, calls


def test_block_soon_after_warm_rotates_ip(tmp_path):
    access, calls = _counting_access(tmp_path)
    assert asyncio.run(access.recover(ip_banned=False))
    access._last_recover -= 1000  # прошла пауза между восстановлениями
    assert asyncio.run(access.recover(ip_banned=False))
    assert calls == ["warm", "rotate", "warm"]


def test_retry_skips_gap_and_rotates(tmp_path):
    access, calls = _counting_access(tmp_path)
    assert asyncio.run(access.recover(ip_banned=False))
    assert asyncio.run(access.recover(ip_banned=False, retry=True))
    assert calls == ["warm", "rotate", "warm"]


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


def test_phone_monitor_reminds_while_down():
    mon = avito_access.PhoneMonitor(proxy="http://phone:1056", fail_after=1, remind_sec=100)

    async def probe():
        return False

    mon._checks = {"proxy": probe}
    assert asyncio.run(mon.check()) == [("proxy", False)]
    assert asyncio.run(mon.check()) == []
    mon._down["proxy"] -= 200  # прошло больше remind_sec
    assert asyncio.run(mon.check()) == [("proxy", False)]


def test_session_roundtrip_keeps_user_agent(tmp_path):
    path = tmp_path / "c.json"
    a = avito_access.AvitoAccess(proxy=None, cookies_path=path)
    a.user_agent, a.cookies = "Firefox/158.0", {"u": "1"}
    a._save()
    b = avito_access.AvitoAccess(proxy=None, cookies_path=path)
    assert b.headers == {"User-Agent": "Firefox/158.0"} and b.cookies == {"u": "1"}
