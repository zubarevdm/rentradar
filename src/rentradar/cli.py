"""Точка входа RentRadar.

Команды:
  rentradar init-db          — создать схему БД.
  rentradar collect          — собрать объявления по профилям в БД (без скоринга/постинга).
  rentradar recompute-stats  — пересчитать рыночные медианы (market_stats).
  rentradar run              — один полный прогон конвейера (collect → score → publish).
  rentradar serve            — Telegram, рассылка и общие фоновые задачи.
  rentradar worker --source  — отдельный сборщик одной площадки.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import ccagent
from .config import SearchProfile, Settings, load_emoji, load_profiles
from .models import Listing, ScoredListing, source_rank
from .personal import PersonalStore
from .personal.access import TARIFFS, TRIAL, access_of, moscow_when, plural
from .personal.dispatcher import PersonalDispatcher
from .personal.filters import matches
from .pipeline import Pipeline
from .runtime import configure_database, log_start, owner, run_until_signal
from .scoring import TastyScoringEngine
from .storage import SqlMarketStatsProvider, SqlStorage
from .storage.orm import SubscriberRow
from .vision import VisionQuotaError, build_analyzer
from .worker import (
    SOURCES,
    STALE_AFTER,
    WorkerState,
    build_collector,
    run_worker,
    source_enabled,
)

log = logging.getLogger("rentradar")

if TYPE_CHECKING:
    from .publisher import TelegramPublisher

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
    pipeline, publisher = _build_pipeline(settings, storage)
    try:
        for profile in profiles:
            await pipeline.collect_only(profile)
        total = await storage.count_listings()
        log.info("Всего объявлений в БД: %d", total)
    finally:
        await publisher.close()
        await storage.dispose()


async def _recompute_stats(settings: Settings) -> None:
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init(pause_max_days=settings.pause_max_days)
    try:
        segments = await storage.recompute_market_stats(window_days=settings.market_window_days)
        log.info("Пересчитаны медианы по %d сегментам", segments)
    finally:
        await storage.dispose()


def _build_collectors(settings: Settings) -> list:
    return [build_collector(settings, s) for s in SOURCES if source_enabled(settings, s)]


def _build_pipeline(settings: Settings, storage: SqlStorage) -> tuple[Pipeline, TelegramPublisher]:
    from .publisher import TelegramPublisher

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
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.filters import Command
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Message
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from .personal.bot import build_router
    from .personal.navigation import nudge_keyboard
    from .publisher import TelegramPublisher
    from .tg import POLL_TIMEOUT, make_bot

    profiles = _load_profiles_or_warn(settings)
    if not profiles:
        return
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.require_worker_schema()
    worker_state = WorkerState(storage)

    # Один общий Telegram-бот: и постинг в канал, и личные чаты подписчиков.
    bot: Bot | None = None
    if settings.bot_token and not settings.dry_run:
        bot = make_bot(settings)
        publisher = TelegramPublisher(
            dry_run=False,
            storage=storage,
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
        collectors=[],
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

    async def market_job() -> None:
        since = _utcnow() - timedelta(hours=settings.personal_lookback_hours)
        await personal_store.prune_attempts(since)
        await personal.outbox.prune(since)
        await storage.recompute_market_stats(window_days=settings.market_window_days)

    async def public_job() -> None:
        if public_paused["on"]:
            log.info("Публичный постинг на паузе (/resumepublic — снять)")
            return
        try:
            since = _utcnow() - timedelta(hours=settings.public_lookback_hours)
            candidates = []
            city_listings = {}
            for p in profiles:
                if p.city not in city_listings:
                    city_listings[p.city] = await storage.recent_listings(p.city, since)
                # Канал: свой (низкий) порог вкусности — отбор по красоте, лучшие
                # сделки бережём боту (см. public_min_score).
                candidates.extend(
                    await pipeline.score_recent(p, since, min_score=settings.public_min_score,
                                                listings=city_listings[p.city])
                )
            # Паблик-гейт по «красоте»: в канал только приличный ремонт.
            passed = [c for c in candidates if _passes_public_gate(c, settings)]
            if analyzer is not None and settings.vision_public_finalists > 0:
                passed = await _pick_showcase(passed)
                if not passed:
                    return
            # Канал голодает из-за отставшего vision? Кандидаты по скору есть, но
            # ни один не проанализирован → молчать нельзя, алертим админа.
            if candidates and not passed:
                n_unanalyzed = sum(1 for c in candidates if c.listing.analyzed_at is None)
                if (
                    n_unanalyzed == len(candidates)
                    and settings.vision_enabled
                    and settings.public_require_analyzed
                ):
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
            by_appeal = (settings.vision_enabled and settings.public_rank_by_appeal) or (
                analyzer is not None and settings.vision_public_finalists > 0
            )
            n = len(await pipeline.publish_top(passed, by_appeal=by_appeal))
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
    # Пауза публичного постинга (админ-команда) — не мешает сбору и личке.
    public_paused = {"on": False}
    # Живые метрики для команды /health.
    runtime: dict = {
        "started_at": _utcnow(),
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

    phone_state: dict[str, bool] = {}

    async def check_source_health() -> None:
        now = _utcnow()
        for source, row in (await worker_state.read()).items():
            if not source_enabled(settings, source):
                continue
            down = (row.heartbeat < now - STALE_AFTER or row.state == "stopped"
                    or row.failures >= SOURCE_DOWN_AFTER)
            state = "down" if down else "up"
            previous = source_health.get(source)
            source_health[source] = state
            if previous != state and (down or previous == "down"):
                text = "недоступен" if down else "снова работает"
                await _alert_admin(
                    f"Сборщик <b>{source}</b> {text}. /health — детали.",
                    key=f"src:{source}", cooldown_h=0,
                )
            if source == "avito":
                for part, ok in (row.phone or {}).items():
                    if phone_state.get(part) == ok:
                        continue
                    phone_state[part] = ok
                    if part in phone_texts:
                        await _alert_admin(
                            phone_texts[part][ok], key=f"phone:{part}", cooldown_h=0,
                        )

    async def _analyze_and_save(listing: Listing) -> Listing:
        """Vision по фото лота → сохранить в БД; вернуть лот с результатом анализа."""
        analysis = await analyzer.analyze(listing.photos[: settings.vision_max_images])
        if analysis is None:
            raise ValueError("Vision returned no usable analysis")
        # Нет фото самой квартиры (только дом/двор/рендеры) → ремонт и красоту
        # НЕ определить: renovation/appeal = None. Такой лот пройдёт только под
        # фильтр «любой» и не попадёт в WOW-канал (appeal неизвестен).
        no_interior = bool(analysis and analysis.no_interior)
        best_urls = (
            [listing.photos[i] for i in analysis.best_photos if i < len(listing.photos)]
            if analysis and not no_interior
            else []
        )
        fields = {
            "renovation": None if no_interior else (analysis.renovation if analysis else None),
            "has_furniture": analysis.has_furniture if analysis else None,
            "appeal": None if no_interior else (analysis.appeal if analysis else None),
            "best_photos": best_urls,
            "flagged": bool(analysis and analysis.contact_overlay),
            "analyzed_at": _utcnow(),
        }
        saved = await storage.save_photo_analysis(
            listing.source.value, listing.external_id, expected_photos=listing.photos, **fields,
        )
        if not saved:
            raise ValueError("Photo set changed during analysis")
        return listing.model_copy(update=fields)

    async def _analyze_public(listing: Listing) -> Listing:
        """Анализ с повтором: 429 у Polza — разовый лимит провайдера, не баланс."""
        for attempt in range(3):
            try:
                return await _analyze_and_save(listing)
            except VisionQuotaError as exc:
                if "429" not in str(exc) or attempt == 2:
                    raise
                await asyncio.sleep(10 * (attempt + 1))
        raise AssertionError("unreachable")

    async def _pick_showcase(passed: list[ScoredListing]) -> list[ScoredListing]:
        """Vision только для финалистов слота: N лучших по скору ещё не постившихся
        лотов с фото → анализ → в канал самые красивые (appeal ≥ порога)."""
        pool: list[ScoredListing] = []
        for c in sorted(_dedupe_scored(passed), key=lambda c: c.score.total, reverse=True):
            if len(pool) >= settings.vision_public_finalists:
                break
            if c.listing.photos and not await storage.was_posted(c.listing.content_key):
                pool.append(c)
        finalists: list[ScoredListing] = []
        for c in pool:
            listing = c.listing
            if listing.analyzed_at is None:
                try:
                    listing = await _analyze_public(listing)
                except VisionQuotaError as exc:
                    await _alert_admin(
                        f"⚠️ Vision (паблик): кончился баланс/лимит API или неверный ключ. "
                        f"Слот публикую без оценки красоты.\n\n{exc}",
                        key="vision", cooldown_h=6,
                    )
                    return pool  # лучше пост по скору, чем пустой слот
                except Exception:  # noqa: BLE001
                    log.exception("Vision (паблик): сбой на лоте %s", listing.url)
                    continue
            finalists.append(c.model_copy(update={"listing": listing}))
        ok = [
            c for c in finalists
            if not c.listing.flagged
            and c.listing.renovation not in settings.public_block_renovations
            and (c.listing.appeal or 0) >= settings.public_min_appeal
        ]
        ok.sort(key=lambda c: (c.listing.appeal or 0, c.score.total), reverse=True)
        log.info(
            "Паблик: vision по %d финалистам, appeal %s, прошло порог %d",
            len(finalists), [c.listing.appeal for c in finalists], len(ok),
        )
        return ok

    async def vision_job() -> None:
        """Проанализировать свежие необработанные лоты (выбор фото + ремонт), закэшировать.
        Только реальные лоты (прошли фрод-фильтр) и с фото; кап на вызовы за проход.
        При ошибке «кончился баланс/лимит» — уведомить админа и прекратить проход
        (постинг при этом продолжается как обычно, без анализа)."""
        if analyzer is None or not settings.vision_enabled:
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
                            best_photos=[], expected_photos=listing.photos,
                            analyzed_at=_utcnow(),
                        )
                        continue
                    await _analyze_and_save(listing)
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
                    await storage.defer_analysis(listing, _utcnow() + timedelta(minutes=30))
                    continue
                alert_state.pop("vision", None)  # успех → следующий сбой снова алертнёт
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

    async def personal_job() -> None:
        try:
            await personal.queue_due(_utcnow())
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
        "proxy": {False: "Телефон-прокси Авито не отвечает.", True: "Телефон-прокси на связи."},
        "control": {False: "Нет управления телефоном (adb).", True: "Управление восстановлено."},
    }
    scheduler = AsyncIOScheduler()
    scheduler.add_job(market_job, "interval", minutes=30, next_run_time=datetime.now(UTC))
    scheduler.add_job(check_source_health, "interval", seconds=30)
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
            try:
                await personal.poll_outbox(_utcnow())
            except Exception:
                log.exception("Could not read the delivery outbox")
            await asyncio.sleep(1)

    delivery_task = asyncio.create_task(immediate_worker())
    startup_task = asyncio.create_task(vision_job())

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
                worker_rows = await worker_state.read()
                lines = []
                for source in SOURCES:
                    row = worker_rows.get(source)
                    if not source_enabled(settings, source):
                        lines.append(f"{source}: отключён")
                        continue
                    if row is None:
                        lines.append(f"{source}: нет сигнала")
                        continue
                    age = int((now - row.heartbeat).total_seconds())
                    state = "offline" if age > STALE_AFTER.total_seconds() else row.state
                    started = moscow_when(row.started_at, now) if row.started_at else "—"
                    last = moscow_when(row.last_success, now) if row.last_success else "—"
                    lines.append(
                        f"{source}: {state}, сигнал {age} с, старт {started}, "
                        f"успех {last}, +{row.new_count}, Δ{row.changed_count}"
                    )
                src = "\n".join(lines)
                oldest, retries, uncertain = await personal.outbox.metrics(now)
                total = await storage.count_listings()
                since = now - timedelta(hours=24)
                vqueue = (await storage.count_unanalyzed("Москва", since)
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
                    f"{src}\n"
                    f"Рассылка: ожидание {oldest} с, повторы {retries}, "
                    f"неподтверждённые {uncertain}\n"
                    f"Паблик: {pub}\n"
                    f"Лотов в БД: {total} · очередь vision: {vqueue}\n"
                    f"Память: {mem_s} · аптайм: {up // 60}ч {up % 60}м"
                )

            @dp.message(Command("collectnow"))
            async def _collect_now(message: Message) -> None:
                """Админ: форснуть сбор площадок прямо сейчас."""
                if not _is_admin(message):
                    return
                labels = {
                    "requested": "запрошен", "running": "уже выполняется",
                    "queued": "уже запрошен", "offline": "нет связи", "disabled": "отключён",
                }
                now = _utcnow()
                results = []
                for source in SOURCES:
                    state = "disabled"
                    if source_enabled(settings, source):
                        state = await worker_state.request(source, now)
                    results.append(f"{source}: {labels[state]}")
                await message.answer("\n".join(results) + "\n/health — результаты.")

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

            @dp.message(Command("users"))
            async def _users(message: Message) -> None:
                """Админ: сколько людей запускало бота и кто последний."""
                if not _is_admin(message):
                    return
                now = _utcnow()
                r = await personal_store.users_report(now)
                msk = timedelta(hours=3)
                rows = []
                for uid, name, created, ref in r["recent"]:
                    who = f"@{html.escape(name)}" if name else f"<code>{uid}</code>"
                    when = (created + msk).strftime("%d.%m %H:%M") if created else "?"
                    rows.append(f"{when} · {who}" + (" · по приглашению" if ref else ""))
                await message.answer(
                    "👥 <b>Запускали бота</b>\n"
                    f"Всего: {r['total']}\n"
                    f"Сегодня (UTC): {r['today']} · за 7 дн: {r['d7']}"
                    f" · за 30 дн: {r['d30']}\n"
                    f"Создали поиск: {r['with_filter']} из {r['total']}\n\n"
                    "<b>Последние (МСК):</b>\n" + ("\n".join(rows) or "—")
                )

            @dp.message(Command("admin"))
            async def _admin_help(message: Message) -> None:
                """Админ: список админ-команд."""
                if not _is_admin(message):
                    return
                cc = "\n/cc &lt;задача&gt; — подготовить правку через Claude"
                await message.answer(
                    "🛠 <b>Админ-команды</b>\n"
                    "/health — состояние системы\n"
                    "/stats — подписчики\n"
                    "/users — сколько людей запускало бота\n"
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
                        "Выпуск — через релизный скрипт на VPS."
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
                    await message.answer(
                        "Выпуск требует переключения всех служб. "
                        "Используйте релизный скрипт на VPS."
                    )
                    return

                if not body:
                    await message.answer(
                        "Использование:\n<code>/cc задача текстом</code> — Claude сделает "
                        "правку в ветке и покажет дифф"
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

            log.info("Бот на связи: /start /new /myfilters /status /buy · админ: /admin")
            await dp.start_polling(bot, polling_timeout=POLL_TIMEOUT)
        else:
            await run_until_signal(asyncio.Event().wait())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Остановка serve по сигналу")
    finally:
        scheduler.shutdown(wait=False)
        tasks = [delivery_task, startup_task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await personal.close()
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


async def _worker(settings: Settings, source: str) -> None:
    profiles = _load_profiles_or_warn(settings)
    if not profiles:
        raise RuntimeError("No collection profiles configured")
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    try:
        await storage.require_worker_schema()
        await run_until_signal(run_worker(settings, source, profiles, storage))
    finally:
        await storage.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="rentradar")
    parser.add_argument("command", choices=[*_COMMANDS, "worker"], help="команда")
    parser.add_argument("--source", choices=SOURCES)
    args = parser.parse_args()
    if (args.command == "worker") != (args.source is not None):
        parser.error("--source is required for worker and only supported by worker")

    settings = Settings()
    settings.db_path = settings.db_path.resolve()
    _setup_logging(settings.log_level)
    configure_database(settings.db_path)
    with ExitStack() as locks:
        if args.command == "worker":
            locks.enter_context(owner(settings.db_path, args.source))
            log_start("collector", args.source)
            asyncio.run(_worker(settings, args.source))
            return
        if args.command in ("collect", "run"):
            for source in SOURCES:
                if source_enabled(settings, source):
                    locks.enter_context(owner(settings.db_path, source))
        if args.command == "serve":
            locks.enter_context(owner(settings.db_path, "bot"))
            log_start("bot")
        if args.command in ("init-db", "collect", "run", "recompute-stats", "vision-test"):
            locks.enter_context(owner(settings.db_path, "migration"))
        asyncio.run(_COMMANDS[args.command](settings))


if __name__ == "__main__":
    main()
