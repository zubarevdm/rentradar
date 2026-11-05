"""Единая сборка Telegram-бота: прокси и/или relay (Cloudflare Worker) из настроек."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from .config import Settings


def make_bot(settings: Settings) -> Bot:
    # С серверов в РФ api.telegram.org бывает недоступен: тогда ходим через relay.
    session: AiohttpSession | None = None
    if settings.telegram_api_base or settings.telegram_proxy:
        session = AiohttpSession(
            api=(
                TelegramAPIServer.from_base(settings.telegram_api_base.rstrip("/"))
                if settings.telegram_api_base
                else TelegramAPIServer.from_base("https://api.telegram.org")
            ),
            proxy=settings.telegram_proxy or None,
        )
        if settings.telegram_api_base:
            # Reused TLS connections to the relay stall on this network after
            # several responses. A fresh connection avoids the 60-70s timeout
            # for both incoming updates and outgoing messages.
            session._connector_init["force_close"] = True
    return Bot(
        settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
