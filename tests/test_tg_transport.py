import pytest
from aiohttp import web
from aiogram.client.telegram import TelegramAPIServer

from rentradar.config import Settings
from rentradar.tg import make_bot


@pytest.mark.parametrize("relay", [True, False])
async def test_relay_uses_fresh_connections_and_direct_reuses(relay):
    transports = set()

    async def get_me(request):
        transports.add(request.transport)
        return web.json_response({
            "ok": True,
            "result": {"id": 123456, "is_bot": True, "first_name": "Test"},
        })

    app = web.Application()
    app.router.add_post("/bot123456:TEST/getMe", get_me)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    bot = make_bot(Settings(
        _env_file=None, bot_token="123456:TEST", telegram_proxy="",
        telegram_api_base=base if relay else "",
    ))
    if not relay:
        bot.session.api = TelegramAPIServer.from_base(base)
    try:
        for _ in range(8):
            assert (await bot.get_me()).id == 123456
        assert len(transports) == (8 if relay else 1)
    finally:
        await bot.session.close()
        await runner.cleanup()
