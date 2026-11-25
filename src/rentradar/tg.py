"""Единая сборка Telegram-бота: прокси и/или relay (Cloudflare Worker) из настроек."""

import socket
from ipaddress import IPv4Address
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from .config import Settings

RELAY_REQUEST_TIMEOUT = 12
POLL_TIMEOUT = 8


class RelayResolver(AbstractResolver):
    """Route the relay host to a known reachable edge and preserve TLS hostname verification."""

    def __init__(self, host: str, address: str) -> None:
        self._host = host
        self._address = str(IPv4Address(address))

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        if host == self._host:
            return [dict(hostname=host, host=self._address, port=port,
                         family=socket.AF_INET, proto=0, flags=socket.AI_NUMERICHOST)]
        resolver = ThreadedResolver()
        try:
            return await resolver.resolve(host, port, family)
        finally:
            await resolver.close()

    async def close(self) -> None:
        pass


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
            timeout=RELAY_REQUEST_TIMEOUT if settings.telegram_api_base else 60,
        )
        if settings.telegram_api_base:
            # Reused TLS connections to the relay stall on this network after
            # several responses. A fresh connection avoids the 60-70s timeout
            # for both incoming updates and outgoing messages.
            session._connector_init["force_close"] = True
            relay_host = urlsplit(settings.telegram_api_base).hostname
            if relay_host and settings.telegram_relay_ipv4 and not settings.telegram_proxy:
                session._connector_init["resolver"] = RelayResolver(
                    relay_host, settings.telegram_relay_ipv4,
                )
    return Bot(
        settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
