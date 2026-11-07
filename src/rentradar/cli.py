"""Точка входа RentRadar.

Команды:
  rentradar init-db          — создать схему БД.
  rentradar collect          — собрать объявления по профилям в БД (без скоринга/постинга).
  rentradar recompute-stats  — пересчитать рыночные медианы (market_stats).
  rentradar run              — один полный прогон конвейера (collect → score → publish).
  rentradar serve            — автономный режим: автопостинг по расписанию (раз в N минут).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import ccagent
from .collectors import AvitoCollector, CianCollector, YandexCollector, avito_access
from .collectors.avito import set_request_cooldown, set_request_delay
from .config import SearchProfile, Settings, load_emoji, load_profiles
from .errors import CollectorBlockedError
from .models import ScoredListing, Source, source_rank
from .personal import PersonalStore
from .personal.access import TARIFFS, TRIAL, access_of, moscow_when, plural
from .personal.bot import build_router
from .personal.dispatcher import PersonalDispatcher
from .personal.filters import matches
from .personal.navigation import nudge_keyboard
from .pipeline import Pipeline
from .preparation import AvitoPreparer
from .publisher import TelegramPublisher
from .scoring import TastyScoringEngine
from .storage import SqlMarketStatsProvider, SqlStorage
from .storage.orm import SubscriberRow
from .tg import make_bot
from .vision import VisionQuotaError, build_analyzer

log = logging.getLogger("rentradar")

# Сколько сборов подряд площадка должна не собраться, чтобы алертить 🔴.
SOURCE_DOWN_AFTER = 3


def _lifecycle_kind(sub: SubscriberRow, now: datetime, settings: Settings) -> str | None:
    """Какое напоминание положено сейчас: trial_soon / trial / expiry / winback.

    Конец пробника и конец оплаты — разные события: после пробника не шлём ещё и
    «доступ закончился». Дедуп — флагами nudged_*.
    """
    access = access_of(sub, now, settings.pause_max_days)
    until = access.until
    if access.paused or until is None:
        return None
    on_trial = sub.trial_until is not None and until == sub.trial_until
    if until > now:
        if on_trial:
            soon = until - now <= timedelta(hours=settings.lifecycle_trial_soon_hours)
            return "trial_soon" if soon and not sub.nudged_trial_soon else None
        if not sub.nudged_expiry and until - now <= _expiry_lead(sub, until, settings):
            return "expiry"
        return None
    if now - until > timedelta(days=settings.lifecycle_winback_days):
        return None
    if on_trial:
        return None if sub.nudged_trial else "trial"
    return None if sub.nudged_winback else "winback"


def _expiry_lead(sub: SubscriberRow, until: datetime, settings: Settings) -> timedelta:
    """За сколько напомнить о конце оплаты: ~15% срока, от 3 ч до N дней. Иначе
    тарифы на 24 ч и 3 дня просили бы продлить сразу после покупки."""
    cap = timedelta(days=settings.lifecycle_expiry_days)
    start = sub.access_starts_at
    if start is None or start >= until:
        return cap
    return min(cap, max(timedelta(hours=3), (until - start) * 0.15))


def _lifecycle_text(
    kind: str,
    sub: SubscriberRow,
    now: datetime,
    settings: Settings,
    *,
    delivered: int = 0,
    has_filters: bool = True,
) -> str:
    until = access_of(sub, now, settings.pause_max_days).until or now
    price = min(t.price for t in TARIFFS.values())
    found = (
        f"За это время я прислал тебе {delivered} "
        f"{plural(delivered, 'вариант', 'варианта', 'вариантов')}.\n"
        if delivered
        else ""
    )
    if kind == "trial_soon":
        if not has_filters:
            return (
                f"⏳ Пробный доступ закончится {moscow_when(until, now)}, а поиск ещё "
                "не настроен. Это займёт минуту — и я начну присылать варианты."
            )
        return (
            f"⏳ Пробный доступ закончится {moscow_when(until, now)}.\n{found}"
            f"Чтобы не пропустить новые, выбери тариф — от {price} ₽ за сутки."
        )
    if kind == "trial":
        return (
            f"Пробные сутки закончились.\n{found}Фильтры сохранены — продолжить можно "
            f"с любого тарифа, от {price} ₽ за сутки."
        )
    if kind == "expiry":
        return (
            f"⏳ Доступ закончится {moscow_when(until, now)}. "
            "Продли, чтобы не пропустить новые варианты."
        )
    return "Доступ закончился. Фильтры сохранены — продлить можно в пару нажатий."


def _lifecycle_nudge(
    sub: SubscriberRow,
    now: datetime,
    settings: Settings,
    *,
    delivered: int = 0,
    has_filters: bool = True,
) -> tuple[str | None, str]:
    """Какое напоминание (если есть) отправить подписчику. (kind, текст) или (None, '')."""
    kind = _lifecycle_kind(sub, now, settings)
    if kind is None:
        return None, ""
    return kind, _lifecycle_text(
        kind, sub, now, settings, delivered=delivered, has_filters=has_filters
    )



def _dedupe_scored(scored: list[ScoredListing]) -> list[ScoredListing]:
    """Кросс-площадочный дедуп кандидатов канала: одна квартира с разных площадок →
    оставить дешевле (при равенстве Avito>Cian>Yandex). Порядок первого появления
    сохраняем — финальную сортировку сделает publish_top."""
    order: list[str] = []
    best: dict[str, ScoredListing] = {}
    for s in scored:
        key = s.listing.content_key
        if key not in best:
            order.append(key)
            best[key] = s
            continue
        cur = best[key]
        if source_rank(s.listing.source.value, s.listing.price) < source_rank(
            cur.listing.source.value, cur.listing.price
        ):
            best[key] = s
    return [best[key] for key in order]


def _passes_public_gate(scored: ScoredListing, settings: Settings) -> bool:
    """Пускать ли лот в публичный WOW-канал по «красоте»."""
    listing = scored.listing
    # Без vision-проверки в канал не пускаем — иначе скам-оверлей на фото проскочит
    # до анализа (как и случилось). Проверенные лоты имеют analyzed_at.
    if settings.vision_enabled and settings.public_require_analyzed and listing.analyzed_at is None:
        return False
    if listing.flagged:  # скам-оверлей на фото
        return False
    # Красиво и намного дешевле рынка — приманка мошенника (флаг ставит скоринг).
    if "too_cheap_for_showcase" in scored.score.flags:
        return False
    if settings.public_max_price and listing.price > settings.public_max_price:
        return False
    if not settings.vision_enabled:
        return True
    if listing.renovation in settings.public_block_renovations:
        return False
    # WOW-порог: канал — витрина, нужен ПОДТВЕРЖДЁННЫЙ красивый интерьер. Неизвестный
    # appeal (нет фото квартиры — только дом/двор/рендеры) в канал НЕ пускаем.
    return not (
        settings.public_min_appeal
        and (listing.appeal is None or listing.appeal < settings.public_min_appeal)
    )


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def _load_profiles_or_warn(settings: Settings) -> list[SearchProfile]:
    profiles = load_profiles(settings.profiles_path)
    if not profiles:
        log.warning("Профили не найдены в %s — нечего запускать", settings.profiles_path)
    return profiles


async def _init_db(settings: Settings) -> None:
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)
    await storage.dispose()
    log.info("БД инициализирована: %s", settings.db_path)


async def _collect(settings: Settings) -> None:
    """Собрать и сохранить объявления (наполнение истории для медиан)."""
    profiles = _load_profiles_or_warn(settings)
    if not profiles:
        return
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)
    collectors = _build_collectors(settings)
    try:
        for profile in profiles:
            for collector in collectors:
                try:
                    raws = await collector.fetch(profile)
                    listings = [n for r in raws if (n := collector.normalize(r))]
                    fresh = await storage.upsert_listings(listings)
                    log.info(
                        "collect %s [%s]: получено=%d, новых=%d",
                        profile.name,
                        collector.source_name,
                        len(listings),
                        len(fresh),
                    )
                except CollectorBlockedError as exc:
                    log.warning(
                        "collect %s [%s]: блокировка (%s)",
                        profile.name,
                        collector.source_name,
                        exc,
                    )
        total = await storage.count_listings()
        log.info("Всего объявлений в БД: %d", total)
    finally:
        await storage.dispose()


async def _recompute_stats(settings: Settings) -> None:
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)
    try:
        segments = await storage.recompute_market_stats(window_days=settings.market_window_days)
        log.info("Пересчитаны медианы по %d сегментам", segments)
    finally:
        await storage.dispose()


def _build_collectors(settings: Settings, *, scheduled: bool = False) -> list:
    """Все активные коллекторы площадок. Добавление площадки = строка здесь.
    Авито — через прокси (если задан в .env), т.к. на VPS его банят напрямую."""
    set_request_cooldown(settings.avito_block_cooldown_min * 60)
    set_request_delay(settings.avito_request_delay_sec)
    if settings.avito_browser_warmup and settings.avito_proxy and avito_access.current() is None:
        avito_access.configure(avito_access.AvitoAccess(
            proxy=settings.avito_proxy,
            cookies_path=settings.avito_cookies_path,
            profile_dir=settings.avito_browser_profile,
            rotate_cmd=settings.avito_rotate_cmd,
            rotate_wait_sec=settings.avito_rotate_wait_sec,
        ))
    collectors: list = [
        CianCollector(),
        YandexCollector(fallback_proxy=settings.yandex_fallback_proxy or None),
    ]
    if settings.avito_enabled:
        collectors.append(
            AvitoCollector(
                proxy=settings.avito_proxy or None,
                max_pages=settings.avito_max_pages,
                min_interval_sec=0 if scheduled else settings.avito_collect_interval_min * 60,
            )
        )
    return collectors


def _build_pipeline(settings: Settings, storage: SqlStorage) -> tuple[Pipeline, TelegramPublisher]:
    publisher = TelegramPublisher.from_settings(settings, storage=storage)
    pipeline = Pipeline(
        collectors=_build_collectors(settings),
        storage=storage,
        scoring=TastyScoringEngine(SqlMarketStatsProvider(storage)),
        publisher=publisher,
        settings=settings,
        post_delay=settings.post_delay_sec,
        max_per_cycle=settings.max_posts_per_run,
    )
    return pipeline, publisher


async def _run_cycle(
    storage: SqlStorage,
    pipeline: Pipeline,
    profiles: list[SearchProfile],
    window_days: int,
) -> int:
    """Один полный цикл: обновить медианы, собрать кандидатов со всех профилей и
    площадок, опубликовать ГЛОБАЛЬНЫЙ топ (читаемый канал, а не спам).
    Блокировка отдельной площадки обрабатывается внутри конвейера."""
    await storage.recompute_market_stats(window_days=window_days)
    candidates = []
    for profile in profiles:
        candidates.extend(await pipeline.collect_and_score(profile))
    published = await pipeline.publish_top(candidates)
    return len(published)


async def _run(settings: Settings) -> None:
    profiles = _load_profiles_or_warn(settings)
    if not profiles:
        return
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)
    pipeline, publisher = _build_pipeline(settings, storage)
    try:
        await _run_cycle(storage, pipeline, profiles, settings.market_window_days)
    finally:
        await publisher.close()
        await storage.dispose()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _serve(settings: Settings) -> None:
    """Автономный режим: один бот ведёт публичный канал И персональные подписки.

    Развязка частот: сбор площадок — часто (питает всё), публичный топ-5 — раз в
    час, личная рассылка — каждую минуту (по интервалу каждого пользователя).
    """
    profiles = _load_profiles_or_warn(settings)
    if not profiles:
        return
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)

    # Один общий Telegram-бот: и постинг в канал, и личные чаты подписчиков.
    bot: Bot | None = None
    if settings.bot_token and not settings.dry_run:
        bot = make_bot(settings)
        publisher = TelegramPublisher(
            dry_run=False,
            storage=storage,
            prepare_personal_separately=True,
            bot=bot,
            via_relay=bool(settings.telegram_api_base),
            cian_jpeg_proxy_enabled=settings.cian_jpeg_proxy_enabled,
            max_photos_personal=settings.max_photos_personal,
            max_photos_public=settings.max_photos_public,
            emoji=load_emoji(settings.emoji_path),
            show_renovation=settings.vision_enabled and settings.show_renovation_in_post,
            cta_text=settings.cta_text,
            cta_url=settings.cta_url,
            cta_public_text=settings.cta_public_text,
            cta_public_url=settings.cta_public_url,
            avito_proxy=settings.avito_proxy,
            protect_bot=settings.protect_content_bot,
            protect_public=settings.protect_content_public,
        )
    else:
        publisher = TelegramPublisher(
            dry_run=True,
            storage=storage,
            prepare_personal_separately=True,
            max_photos_personal=settings.max_photos_personal,
            max_photos_public=settings.max_photos_public,
            show_renovation=settings.vision_enabled and settings.show_renovation_in_post,
            cta_text=settings.cta_text,
            cta_url=settings.cta_url,
            cta_public_text=settings.cta_public_text,
            cta_public_url=settings.cta_public_url,
            avito_proxy=settings.avito_proxy,
            protect_bot=settings.protect_content_bot,
            protect_public=settings.protect_content_public,
        )

    pipeline = Pipeline(
        collectors=_build_collectors(settings, scheduled=True),
        storage=storage,
        scoring=TastyScoringEngine(SqlMarketStatsProvider(storage)),
        publisher=publisher,
        settings=settings,
        post_delay=settings.post_delay_sec,
        max_per_cycle=settings.max_posts_per_run,
    )
    personal_store = PersonalStore(storage, pause_max_days=settings.pause_max_days)
    personal = PersonalDispatcher(
        store=personal_store,
        storage=storage,
        scoring=TastyScoringEngine(SqlMarketStatsProvider(storage)),
        publisher=publisher,
        settings=settings,
        require_prepared_avito=True,
    )
    analyzer = build_analyzer(settings)  # None при пустом ключе → vision выключен
    vision_scoring = TastyScoringEngine(SqlMarketStatsProvider(storage))

    post_when = (
        f"в {settings.public_post_hours} MSK"
        if settings.public_post_hours.strip()
        else f"каждые {settings.poll_interval_min}м"
    )
    log.info(
        "serve: сбор/%gм · топ-%d %s (окно %dч) · личная/1м · vision=%s · бот=%s · dry=%s",
        settings.collect_interval_min,
        settings.max_posts_per_run,
        post_when,
        settings.public_lookback_hours,
        settings.vision_model if analyzer else "off",
        "on" if bot else "off",
        settings.dry_run,
    )

    async def _wanted_by_subscribers(listings: list) -> list:
        """Лоты, которые подходят под фильтр хотя бы одного подписчика с доступом.

        Карточка Авито — лишний запрос через телефон-прокси, а каждый запрос
        приближает 429. Тянем её только для лотов, которые реально уйдут в личку.
        Данные карточки (комиссия, описание) могут лишь отсеять лот, но не добавить,
        поэтому матчинг по данным из выдачи — надмножество итоговой рассылки.
        Паблик-канал дотягивает карточку сам при отправке (fallback в паблишере).
        """
        if not listings:
            return []
        now = _utcnow()
        active: dict[int, bool] = {}
        filters = []
        for flt in await personal_store.all_active_filters():
            if flt.user_id not in active:
                active[flt.user_id] = await personal_store.is_active(flt.user_id, now)
            if active[flt.user_id]:
                filters.append(flt)
        return [lst for lst in listings if any(matches(lst, f) for f in filters)]

    immediate_delivery = asyncio.Event()
    preparer = AvitoPreparer(storage, personal_store, settings, immediate_delivery.set)
    source_intervals = {
        Source.CIAN.value: settings.collect_interval_min,
        Source.YANDEX.value: settings.yandex_collect_interval_min,
    }
    if settings.avito_enabled:
        source_intervals[Source.AVITO.value] = settings.avito_collect_interval_min
    source_locks = {source: asyncio.Lock() for source in source_intervals}

    async def collect_job(source: str) -> int:
        # Covers startup and scheduled invocations of the same source.
        lock = source_locks[source]
        if lock.locked():
            return 0
        async with lock:
            total = 0
            try:
                if source == Source.CIAN.value:
                    await storage.recompute_market_stats(window_days=settings.market_window_days)
                selected_profiles = profiles
                if source == Source.AVITO.value:
                    # Avito's search is city-wide; profile filters are applied in
                    # our database. Fetch each city's largest requested limit once.
                    by_city = {}
                    for profile in profiles:
                        key = profile.city.strip().casefold()
                        previous = by_city.get(key)
                        if previous is None or profile.collect_limit > previous.collect_limit:
                            by_city[key] = profile
                    selected_profiles = list(by_city.values())
                agg: dict[str, list[str]] = {}
                for profile in selected_profiles:
                    new, statuses = await pipeline.collect_only(
                        profile, source=source, on_new=immediate_delivery.set,
                    )
                    total += new
                    for src, status in statuses.items():
                        agg.setdefault(src, []).append(status)
                log.info("Сбор [%s]: новых лотов = %d", source, total)
                runtime["last_collect_at"] = _utcnow()
                runtime["last_collect_new"] = total
                await _check_source_health(agg)
                if source == Source.AVITO.value:
                    await preparer.run()
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка сбора [%s]", source)
                await _alert_admin(
                    f"⚠️ Джоб сбора {source} упал: {type(exc).__name__}: {exc}",
                    key=f"job:collect:{source}", cooldown_h=1,
                )
            return total

    async def public_job() -> None:
        if public_paused["on"]:
            log.info("Публичный постинг на паузе (/resumepublic — снять)")
            return
        try:
            since = _utcnow() - timedelta(hours=settings.public_lookback_hours)
            candidates = []
            for p in profiles:
                # Канал: свой (низкий) порог вкусности — отбор по красоте, лучшие
                # сделки бережём боту (см. public_min_score).
                candidates.extend(
                    await pipeline.score_recent(p, since, min_score=settings.public_min_score)
                )
            # Паблик-гейт по «красоте»: в канал только приличный ремонт.
            passed = [c for c in candidates if _passes_public_gate(c, settings)]
            # Канал голодает из-за отставшего vision? Кандидаты по скору есть, но
            # ни один не проанализирован → молчать нельзя, алертим админа.
            if candidates and not passed:
                n_unanalyzed = sum(1 for c in candidates if c.listing.analyzed_at is None)
                if n_unanalyzed == len(candidates) and settings.vision_enabled and settings.public_require_analyzed:
                    await _alert_admin(
                        f"⚠️ Канал голодает: все {n_unanalyzed} кандидатов слота не "
                        "прошли vision-анализ (vision отстаёт или стоит). "
                        "Проверь лог vision-прохода.",
                        key="starving",
                        cooldown_h=3,
                    )
            # Канал = витрина: ранжируем по красоте (WOW), а не по «вкусности» —
            # лучшие сделки бережём для бота. Кросс-дедуп одной квартиры с разных
            # площадок (дешевле/приоритет) — чтобы не постить дубли.
            passed = _dedupe_scored(passed)
            n = len(await pipeline.publish_top(passed, by_appeal=settings.vision_enabled and settings.public_rank_by_appeal))
            runtime["last_public_at"] = _utcnow()
            runtime["last_public_n"] = n
            log.info(
                "Публичный топ: опубликовано %d (кандидатов %d, прошло гейт %d)",
                n,
                len(candidates),
                len(passed),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка публичного постинга")
            await _alert_admin(
                f"⚠️ Джоб публичного постинга упал: {type(exc).__name__}: {exc}",
                key="job:public",
                cooldown_h=1,
            )

    # Троттл алертов ПО КЛЮЧУ: разные категории (vision/сбор/площадка/джоб) не
    # глушат друг друга. Значение — время последней отправки этого ключа.
    alert_state: dict[str, datetime] = {}
    # Состояние площадок для health-мониторинга: source → 'ok'|'down'. Алертим на
    # переходе (не каждый цикл): 🔴 при падении, 🟢 при восстановлении.
    source_health: dict[str, str] = {}
    source_fails: dict[str, int] = {}
    # Пауза публичного постинга (админ-команда) — не мешает сбору и личке.
    public_paused = {"on": False}
    # Живые метрики для команды /health.
    runtime: dict = {
        "started_at": _utcnow(),
        "last_collect_at": None,
        "last_collect_new": None,
        "last_public_at": None,
        "last_public_n": None,
    }

    async def _alert_admin(text: str, *, key: str = "general", cooldown_h: float = 6.0) -> None:
        """Уведомить всех админов. `key` — категория (свой троттл у каждой),
        `cooldown_h` — не частить одинаковыми (0 = слать всегда, для редких событий)."""
        now = _utcnow()
        last = alert_state.get(key)
        if last is not None and cooldown_h and (now - last).total_seconds() < cooldown_h * 3600:
            return
        alert_state[key] = now
        log.warning("ADMIN ALERT [%s]: %s", key, text)
        if bot is not None:
            for admin in settings.admins:
                try:
                    await bot.send_message(admin, text)
                except Exception:  # noqa: BLE001
                    log.warning("Не смог уведомить админа %s (%s)", admin, key)

    async def _check_source_health(agg: dict[str, list[str]]) -> None:
        """По итогам сбора: source считается живым, если собрал хоть в одном профиле.
        Алертим только на смене состояния (ok↔down) — без спама каждые 10 минут.
        «Лежит» — только после SOURCE_DOWN_AFTER неудачных сборов подряд: разовый
        сбой (капча, смена IP телефона) сам проходит на следующем сборе."""
        for src, results in agg.items():
            if "ok" in results:
                source_fails[src] = 0
                state = "ok"
            else:
                source_fails[src] = source_fails.get(src, 0) + 1
                state = "down" if source_fails[src] >= SOURCE_DOWN_AFTER else "ok"
            if source_health.get(src, "ok") == state:
                continue
            source_health[src] = state
            if state == "down":
                await _alert_admin(
                    f"🔴 Площадка <b>{src}</b> перестала собираться "
                    "(блокировка/ошибка). Остальные площадки работают.",
                    key=f"src:{src}",
                    cooldown_h=0,
                )
            else:
                await _alert_admin(
                    f"🟢 Площадка <b>{src}</b> снова собирается.",
                    key=f"src:{src}",
                    cooldown_h=0,
                )

    async def vision_job() -> None:
        """Проанализировать свежие необработанные лоты (выбор фото + ремонт), закэшировать.
        Только реальные лоты (прошли фрод-фильтр) и с фото; кап на вызовы за проход.
        При ошибке «кончился баланс/лимит» — уведомить админа и прекратить проход
        (постинг при этом продолжается как обычно, без анализа)."""
        if analyzer is None:
            return
        try:
            since = _utcnow() - timedelta(hours=24)
            cands = await storage.unanalyzed_listings(
                "Москва", since, settings.vision_max_per_run * 4
            )
            profile = SearchProfile(name="vision", city="Москва")
            # Активные фильтры подписчиков — чтобы анализировать и нужные им лоты,
            # даже с низким скором (адресно, по структурным критериям без ремонта).
            active_filters = await personal_store.all_active_filters()
            analyzed = 0
            for listing in cands:
                if analyzed >= settings.vision_max_per_run:
                    break
                # Ошибка на ОДНОМ лоте (скоринг/матчинг/анализ) не должна валить
                # проход: очередь FIFO, и «ядовитый» лот в её голове иначе клинит
                # vision навсегда (так и вышло: анализ стоял с 28.06 при живом API).
                try:
                    score = await vision_scoring.score(listing, profile)
                    # Кому нужен этот лот: годен в паблик (скор≥порога) ИЛИ структурно
                    # подходит под чей-то фильтр (метро/цена/комнаты, без учёта ремонта).
                    wanted_by_user = any(
                        matches(listing, flt)
                        for flt in active_filters
                    )
                    hopeless = (
                        score.total < settings.vision_min_score and not wanted_by_user
                    )
                    # Экономия: не жжём токены на лоты без фото, скам и никому не нужные.
                    if not listing.photos or not score.is_publishable or hopeless:
                        # Помечаем обработанным без вызова сети — чтобы не дёргать повторно.
                        await storage.save_photo_analysis(
                            listing.source.value,
                            listing.external_id,
                            renovation=None,
                            has_furniture=None,
                            appeal=None,
                            best_photos=[],
                            analyzed_at=_utcnow(),
                        )
                        continue
                    analysis = await analyzer.analyze(
                        listing.photos[: settings.vision_max_images]
                    )
                except VisionQuotaError as exc:
                    await _alert_admin(
                        "⚠️ Vision: похоже, кончился баланс или лимит API "
                        "(или неверный ключ).\nПостинг продолжается без анализа фото.\n\n"
                        f"{exc}",
                        key="vision",
                        cooldown_h=6,
                    )
                    return  # не долбим API дальше в этом проходе
                except Exception:  # noqa: BLE001
                    log.exception("Vision: сбой на лоте %s — пропускаю", listing.url)
                    await storage.save_photo_analysis(
                        listing.source.value,
                        listing.external_id,
                        renovation=None,
                        has_furniture=None,
                        appeal=None,
                        best_photos=[],
                        analyzed_at=_utcnow(),
                    )
                    continue
                alert_state.pop("vision", None)  # успех → следующий сбой снова алертнёт
                # Нет фото самой квартиры (только дом/двор/рендеры) → ремонт и красоту
                # НЕ определить: renovation/appeal = None. Такой лот пройдёт только под
                # фильтр «любой» и не попадёт в WOW-канал (appeal неизвестен).
                no_interior = bool(analysis and analysis.no_interior)
                best_urls = (
                    [listing.photos[i] for i in analysis.best_photos]
                    if analysis and not no_interior
                    else []
                )
                reno = None if no_interior else (analysis.renovation if analysis else None)
                appeal = None if no_interior else (analysis.appeal if analysis else None)
                await storage.save_photo_analysis(
                    listing.source.value,
                    listing.external_id,
                    renovation=reno,
                    has_furniture=(analysis.has_furniture if analysis else None),
                    appeal=appeal,
                    best_photos=best_urls,
                    flagged=bool(analysis and analysis.contact_overlay),
                    analyzed_at=_utcnow(),
                )
                analyzed += 1
            if analyzed:
                log.info("Vision: проанализировано %d", analyzed)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка vision-прохода")
            await _alert_admin(
                f"⚠️ Джоб vision упал: {type(exc).__name__}: {exc}",
                key="job:vision",
                cooldown_h=1,
            )

    async def personal_job(*, immediate: bool = False) -> None:
        try:
            n = await (personal.run_immediate(_utcnow()) if immediate else personal.run_due(_utcnow()))
            if n:
                log.info("Личная рассылка: отправлено %d", n)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка личной рассылки")
            await _alert_admin(
                f"⚠️ Джоб личной рассылки упал: {type(exc).__name__}: {exc}",
                key="job:personal",
                cooldown_h=1,
            )

    async def lifecycle_job() -> None:
        """Ежечасно днём: конец пробника скоро/наступил, конец оплаты, win-back."""
        if bot is None:
            return
        try:
            now = _utcnow()
            sent = 0
            for sub in await personal_store.lifecycle_candidates():
                kind = _lifecycle_kind(sub, now, settings)
                if kind is None:
                    continue
                delivered, has_filters = 0, True
                if kind in ("trial_soon", "trial") and sub.trial_until is not None:
                    delivered = await personal_store.count_delivered(
                        sub.telegram_id, sub.trial_until - TRIAL
                    )
                    has_filters = bool(await personal_store.list_filters(sub.telegram_id))
                text = _lifecycle_text(
                    kind, sub, now, settings, delivered=delivered, has_filters=has_filters
                )
                try:
                    await bot.send_message(
                        sub.telegram_id,
                        text,
                        reply_markup=nudge_keyboard(setup=kind == "trial_soon" and not has_filters),
                    )
                    await personal_store.mark_nudged(sub.telegram_id, kind)
                    sent += 1
                except TelegramForbiddenError:
                    # Бот заблокирован: ежечасный повтор только засорял бы лог.
                    await personal_store.mark_nudged(sub.telegram_id, kind)
                except Exception:  # noqa: BLE001 — сеть/флуд: повторим в следующий час
                    log.warning("lifecycle: не уведомил %s", sub.telegram_id)
            if sent:
                log.info("Lifecycle: отправлено напоминаний %d", sent)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка lifecycle")
            await _alert_admin(
                f"⚠️ Джоб lifecycle упал: {type(exc).__name__}: {exc}",
                key="job:lifecycle",
                cooldown_h=1,
            )

    phone_texts = {
        ("proxy", False): "📵 Телефон-прокси Авито <b>не отвечает</b>: нет связи, Wi-Fi включён "
                          "или Tailscale выключен. Авито не собирается.",
        ("proxy", True): "📶 Телефон-прокси Авито снова на связи.",
        ("control", False): "⚠️ Нет управления телефоном (adb) — IP при бане не сменится. "
                            "Скорее всего телефон перезагрузился: подключи к ПК и выполни "
                            "<code>adb tcpip 5555</code>.",
        ("control", True): "✅ Управление телефоном (adb) восстановлено.",
    }
    phone_monitor = (
        avito_access.PhoneMonitor(
            proxy=settings.avito_proxy, control_cmd=settings.avito_phone_check_cmd
        )
        if settings.avito_enabled and settings.avito_browser_warmup and settings.avito_proxy
        else None
    )

    async def phone_job() -> None:
        for part, ok in await phone_monitor.check():
            await _alert_admin(phone_texts[(part, ok)], key=f"phone:{part}", cooldown_h=0)

    scheduler = AsyncIOScheduler()
    for source, interval in source_intervals.items():
        scheduler.add_job(
            collect_job, "interval", minutes=max(1, interval), args=[source],
            id=f"collect_{source}", max_instances=1, coalesce=True,
        )
        log.info("Расписание %s: каждые %g мин", source, interval)
    if phone_monitor is not None:
        scheduler.add_job(phone_job, "interval", minutes=max(1, settings.avito_phone_check_min))
    # Публичный постинг: по часам-пикам (MSK), иначе — почасовой запасной режим.
    post_hours = settings.public_post_hours.strip()
    if post_hours:
        scheduler.add_job(
            public_job,
            "cron",
            hour=post_hours,
            minute=0,
            jitter=max(0, settings.public_post_jitter_sec),  # ±сдвиг, чтобы не ровно в HH:00
            timezone=ZoneInfo("Europe/Moscow"),
        )
        log.info(
            "Публичный постинг по часам MSK: %s (±%dс разброс)",
            post_hours,
            settings.public_post_jitter_sec,
        )
    else:
        scheduler.add_job(public_job, "interval", minutes=max(1, settings.poll_interval_min))
    scheduler.add_job(personal_job, "interval", minutes=1)
    if analyzer is not None:
        scheduler.add_job(vision_job, "interval", minutes=max(1, settings.vision_interval_min))
    if bot is not None:  # lifecycle-напоминания — ежечасно в lifecycle_hours MSK
        scheduler.add_job(
            lifecycle_job,
            "cron",
            hour=settings.lifecycle_hours,
            minute=5,
            timezone=ZoneInfo("Europe/Moscow"),
        )
    scheduler.start()

    async def immediate_worker() -> None:
        while True:
            await immediate_delivery.wait()
            immediate_delivery.clear()
            await personal_job(immediate=True)

    delivery_task = asyncio.create_task(immediate_worker())

    async def startup_jobs() -> None:
        # На старте — сбор + первый vision-проход (чтобы БД и личка сразу со свежими
        # данными и анализом). Публичный постинг НЕ дёргаем: иначе каждый рестарт =
        # пачка постов в канал. Он пойдёт по расписанию (раз в poll_interval_min).
        await asyncio.gather(*(collect_job(source) for source in source_intervals))
        await vision_job()

    startup_task = None
    try:
        if bot is not None:
            dp = Dispatcher(storage=MemoryStorage())
            dp.include_router(build_router(personal_store, settings))

            def _is_admin(message: Message) -> bool:
                return message.from_user is not None and message.from_user.id in settings.admins

            @dp.message(Command("postnow", "postnew"))
            async def _post_now(message: Message) -> None:
                """Админ: форснуть публикацию топа в канал прямо сейчас."""
                if not _is_admin(message):
                    return
                if public_paused["on"]:
                    await message.answer("⏸ Публичный постинг на паузе — сними /resumepublic.")
                    return
                await message.answer("⏳ Публикую топ в канал…")
                await public_job()
                await message.answer("✅ Готово — проверь канал и лог.")

            @dp.message(Command("health"))
            async def _health(message: Message) -> None:
                """Админ: сводка состояния системы."""
                if not _is_admin(message):
                    return
                now = _utcnow()
                if source_health:
                    src = "  ".join(
                        f"{'🟢' if s == 'ok' else '🔴'} {n}"
                        for n, s in sorted(source_health.items())
                    )
                else:
                    src = "ещё не собирал"
                total = await storage.count_listings()
                since = now - timedelta(hours=24)
                vqueue = (len(await storage.unanalyzed_listings("Москва", since, 10_000))
                          if analyzer is not None else "отключён")
                try:
                    import resource

                    mem_s = f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024} МБ"
                except Exception:  # noqa: BLE001 — Windows/dev без resource
                    mem_s = "н/д"

                def _ago(dt: datetime | None) -> str:
                    if dt is None:
                        return "—"
                    m = int((now - dt).total_seconds() // 60)
                    return f"{m} мин назад" if m < 120 else f"{m // 60} ч назад"

                up = int((now - runtime["started_at"]).total_seconds() // 60)
                pub = _ago(runtime["last_public_at"])
                if runtime["last_public_n"] is not None:
                    pub += f" ({runtime['last_public_n']} шт)"
                if public_paused["on"]:
                    pub += " ⏸ПАУЗА"
                await message.answer(
                    "🩺 <b>Health</b>\n"
                    f"Площадки: {src}\n"
                    f"Сбор: {_ago(runtime['last_collect_at'])} "
                    f"(+{runtime['last_collect_new'] or 0} новых)\n"
                    f"Паблик: {pub}\n"
                    f"Лотов в БД: {total} · очередь vision: {vqueue}\n"
                    f"Память: {mem_s} · аптайм: {up // 60}ч {up % 60}м"
                )

            @dp.message(Command("collectnow"))
            async def _collect_now(message: Message) -> None:
                """Админ: форснуть сбор площадок прямо сейчас."""
                if not _is_admin(message):
                    return
                await message.answer("⏳ Запускаю сбор…")
                counts = await asyncio.gather(*(
                    collect_job(source) for source in source_intervals
                ))
                await message.answer(
                    f"✅ Собрано, новых: {sum(counts)}. /health — детали."
                )

            @dp.message(Command("pausepublic"))
            async def _pause_public(message: Message) -> None:
                """Админ: приостановить публичный постинг (сбор и личка продолжаются)."""
                if not _is_admin(message):
                    return
                public_paused["on"] = True
                await message.answer("⏸ Публичный постинг на паузе. /resumepublic — снять.")

            @dp.message(Command("resumepublic"))
            async def _resume_public(message: Message) -> None:
                """Админ: возобновить публичный постинг."""
                if not _is_admin(message):
                    return
                public_paused["on"] = False
                await message.answer("▶️ Публичный постинг возобновлён.")

            @dp.message(Command("stats"))
            async def _stats(message: Message) -> None:
                """Админ: метрики подписчиков."""
                if not _is_admin(message):
                    return
                now = _utcnow()
                s = await personal_store.admin_stats(now)
                delivered = await personal_store.count_delivered(None, now - timedelta(hours=24))
                await message.answer(
                    "📊 <b>Подписчики</b>\n"
                    f"Всего: {s['subscribers']}\n"
                    f"Активный доступ по оплате/бонусам: {s['paid']}; на паузе: {s['paused']}\n"
                    f"На триале: {s['trial']}\n"
                    f"Истёкших: {s['expired']}\n"
                    f"Активных фильтров: {s['active_filters']}\n"
                    f"Доставлено объявлений за 24 ч: {delivered}"
                )

            @dp.message(Command("admin"))
            async def _admin_help(message: Message) -> None:
                """Админ: список админ-команд."""
                if not _is_admin(message):
                    return
                cc = "\n/cc &lt;задача&gt; · /cc apply — правки кодом через Claude"
                await message.answer(
                    "🛠 <b>Админ-команды</b>\n"
                    "/health — состояние системы\n"
                    "/stats — подписчики\n"
                    "/collectnow — собрать сейчас\n"
                    "/postnow — опубликовать топ в канал\n"
                    "/paid &lt;telegram_id&gt; &lt;24h|3d|7d|30d&gt; "
                    "&lt;operation_id&gt; — оплата\n"
                    "operation_id — уникальный номер подтверждения, "
                    "например pay_20261025_001.\n"
                    "Повтор вернёт сохранённый результат; для другой оплаты нужен новый номер.\n"
                    "/grant &lt;telegram_id&gt; [дней] — подарок, без награды за оплату.\n"
                    "/pausepublic · /resumepublic — пауза канала\n"
                    + (cc if ccagent.is_configured(settings.claude_token) else "")
                )

            async def _cc_report(message: Message, res: dict) -> None:
                """Отправить админу отчёт о прогоне /cc."""
                st = res.get("status")
                if st == "busy":
                    await message.answer("⏳ Уже выполняется другая задача /cc — подожди.")
                elif st == "timeout":
                    await message.answer("⏱ Claude не уложился в лимит времени, прогон отменён.")
                elif st == "no_changes":
                    await message.answer(
                        "🤖 Claude не внёс изменений.\n\n"
                        + html.escape((res.get("summary") or "")[:1500])
                    )
                elif st == "done":
                    ok = res.get("pytest_rc") == 0
                    tests = "✅ прошли" if ok else f"❌ упали (код {res.get('pytest_rc')})"
                    warn = "" if ok else "\n\n⚠️ Тесты красные — применять НЕ рекомендую."
                    br = html.escape(res.get("branch", ""))
                    await message.answer(
                        f"🤖 <b>Готово</b> · ветка <code>{br}</code>\n\n"
                        f"{html.escape((res.get('summary') or '')[:1000])}\n\n"
                        f"📝 Файлов изменено: {res.get('changed')}\n"
                        f"<pre>{html.escape((res.get('diffstat') or '')[:1200])}</pre>\n"
                        f"🧪 Тесты: {tests}\n"
                        f"<pre>{html.escape((res.get('pytest') or '')[:500])}</pre>"
                        f"💳 ≈${float(res.get('cost') or 0):.3f} (по подписке не списывается)"
                        f"{warn}\n\n"
                        "Выкатить на прод: <code>/cc apply</code>"
                    )
                else:
                    await message.answer(
                        "❌ Ошибка /cc: " + html.escape(str(res.get("msg") or res))[:500]
                    )

            @dp.message(Command("cc"))
            async def _cc(message: Message) -> None:
                """Админ: правки кодом через Claude Code. /cc <задача> · /cc apply."""
                if not _is_admin(message):
                    return
                if not ccagent.is_configured(settings.claude_token):
                    await message.answer(
                        "⚙️ /cc не настроен: нет RENTRADAR_CLAUDE_TOKEN или скриптов "
                        "(команда работает только на сервере)."
                    )
                    return
                parts = (message.text or "").split(maxsplit=1)
                body = parts[1].strip() if len(parts) > 1 else ""

                if body.lower() == "apply":
                    await message.answer("⏳ Вливаю ветку в main…")
                    res = await ccagent.apply()
                    st = res.get("status")
                    if st == "applied":
                        await message.answer(
                            f"✅ Ветка <code>{html.escape(res.get('branch', ''))}</code> "
                            "влита в main. Перезапускаю сервис — вернусь через ~10 сек."
                        )
                        await asyncio.sleep(1)  # дать сообщению уйти до рестарта
                        subprocess.Popen(["systemctl", "restart", "rentradar"])
                    elif st == "nothing":
                        await message.answer("Нечего применять — сначала /cc &lt;задача&gt;.")
                    elif st == "conflict":
                        cbr = html.escape(res.get("branch", ""))
                        await message.answer(
                            f"⚠️ Конфликт при вливании <code>{cbr}</code> "
                            "— нужно разрулить вручную на сервере."
                        )
                    else:
                        emsg = html.escape(str(res.get("msg") or res))[:400]
                        await message.answer("❌ Не удалось применить: " + emsg)
                    return

                if not body:
                    await message.answer(
                        "Использование:\n<code>/cc задача текстом</code> — Claude сделает "
                        "правку в ветке и покажет дифф\n"
                        "<code>/cc apply</code> — выкатить последнюю на прод"
                    )
                    return

                await message.answer("🤖 Claude работает над задачей… (обычно 1–3 мин)")

                async def _bg() -> None:
                    try:
                        res = await ccagent.run_task(body)
                        await _cc_report(message, res)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Ошибка /cc")
                        await message.answer(f"❌ /cc упал: {type(exc).__name__}: {exc}")

                asyncio.create_task(_bg())  # не блокируем поллинг на минуты

            startup_task = asyncio.create_task(startup_jobs())  # параллельно с поллингом
            log.info("Бот на связи: /start /new /myfilters /status /buy · админ: /admin")
            await dp.start_polling(bot)
        else:
            await startup_jobs()
            await asyncio.Event().wait()  # dry-run/без токена: только публичный канал
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Остановка serve по сигналу")
    finally:
        scheduler.shutdown(wait=False)
        if startup_task is not None:
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
        delivery_task.cancel()
        await asyncio.gather(delivery_task, return_exceptions=True)
        if bot is not None:
            await bot.session.close()
        await storage.dispose()


async def _vision_test(settings: Settings) -> None:
    """Прогнать vision на нескольких реальных лотах из БД и распечатать оценку —
    чтобы вручную сверить достоверность перед боевым включением."""
    analyzer = build_analyzer(settings)
    if analyzer is None:
        log.error("Vision выключен: задай RENTRADAR_VISION_API_KEY в .env")
        return
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)
    try:
        since = _utcnow() - timedelta(hours=72)
        listings = await storage.recent_listings("Москва", since)
        sample = [x for x in listings if x.photos][:5]
        if not sample:
            log.warning("Нет лотов с фото в БД за 72ч — сначала собери (serve/collect)")
            return
        log.info("Проверяю vision на %d лотах (модель %s)…", len(sample), settings.vision_model)
        for i, listing in enumerate(sample, 1):
            try:
                a = await analyzer.analyze(listing.photos[: settings.vision_max_images])
            except VisionQuotaError as exc:
                log.error("Ошибка API (баланс/лимит/ключ): %s", exc)
                return
            print(f"\n=== лот {i}/{len(sample)} ===")
            print("объявление :", listing.url)
            print("всего фото :", len(listing.photos))
            if a is None:
                print("РЕЗУЛЬТАТ   : не удалось проанализировать")
                continue
            print(f"ремонт      : {a.renovation} | мебель: {a.has_furniture} | красота: {a.appeal}")
            print(f"выбраны фото: индексы {a.best_photos}")
            for idx in a.best_photos:
                if 0 <= idx < len(listing.photos):
                    print("  ", listing.photos[idx])
    finally:
        await storage.dispose()


_COMMANDS = {
    "init-db": _init_db,
    "collect": _collect,
    "recompute-stats": _recompute_stats,
    "run": _run,
    "serve": _serve,
    "vision-test": _vision_test,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="rentradar")
    parser.add_argument("command", choices=list(_COMMANDS), help="команда")
    args = parser.parse_args()

    settings = Settings()
    _setup_logging(settings.log_level)
    asyncio.run(_COMMANDS[args.command](settings))


if __name__ == "__main__":
    main()
