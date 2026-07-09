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
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .collectors import AvitoCollector, CianCollector, YandexCollector
from .collectors.avito import fetch_detail
from .config import SearchProfile, Settings, load_emoji, load_profiles
from .errors import CollectorBlockedError
from .models import ScoredListing, Source
from .personal import PersonalStore
from .personal.bot import build_router
from .personal.dispatcher import PersonalDispatcher
from .personal.filters import matches
from .pipeline import Pipeline
from .publisher import TelegramPublisher
from .scoring import TastyScoringEngine
from .storage import SqlMarketStatsProvider, SqlStorage
from .storage.orm import SubscriberRow
from .vision import VisionQuotaError, build_analyzer

log = logging.getLogger("rentradar")


def _lifecycle_nudge(
    sub: SubscriberRow, now: datetime, settings: Settings
) -> tuple[str | None, str]:
    """Какое напоминание (если есть) отправить подписчику. (kind, текст) или (None, '')."""
    if sub.paused_at is not None:  # на паузе — не трогаем
        return None, ""
    pu = sub.paid_until
    if pu is not None and pu > now:  # подписка активна
        if not sub.nudged_expiry and (pu - now).days <= settings.lifecycle_expiry_days:
            days = max(1, (pu - now).days)
            return "expiry", (
                f"⏳ Подписка заканчивается через <b>{days} дн.</b>\n"
                "Продли, чтобы не пропускать свежие варианты — кнопка «💳 Подписка»."
            )
        return None, ""
    if pu is not None and pu <= now:  # истекла
        if not sub.nudged_winback and (now - pu).days <= settings.lifecycle_winback_days:
            return "winback", (
                "👋 Подписка закончилась, а рынок не стоит — по твоему фильтру "
                "появляются новые варианты. Вернись: кнопка «💳 Подписка»."
            )
        return None, ""
    # Никогда не платил → триал. Бесплатные кончились — момент для конверсии.
    if not sub.nudged_trial and sub.free_sends_used >= settings.free_sends_limit:
        return "trial", (
            "🔓 Бесплатные показы закончились.\nОформи подписку — и снова буду "
            "присылать все подходящие варианты первым. Кнопка «💳 Подписка»."
        )
    return None, ""


def _passes_public_gate(scored: ScoredListing, settings: Settings) -> bool:
    """Пускать ли лот в публичный канал по «красоте». Неанализированные (renovation
    is None) проходят — чтобы канал не голодал, пока vision не догнал."""
    listing = scored.listing
    # Без vision-проверки в канал не пускаем — иначе скам-оверлей на фото проскочит
    # до анализа (как и случилось). Проверенные лоты имеют analyzed_at.
    if settings.public_require_analyzed and listing.analyzed_at is None:
        return False
    if listing.flagged:  # скам-оверлей на фото
        return False
    if listing.renovation in settings.public_block_renovations:
        return False
    return not (
        settings.public_min_appeal
        and listing.appeal is not None
        and listing.appeal < settings.public_min_appeal
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
    await storage.init()
    await storage.dispose()
    log.info("БД инициализирована: %s", settings.db_path)


async def _collect(settings: Settings) -> None:
    """Собрать и сохранить объявления (наполнение истории для медиан)."""
    profiles = _load_profiles_or_warn(settings)
    if not profiles:
        return
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init()
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
    await storage.init()
    try:
        segments = await storage.recompute_market_stats(window_days=settings.market_window_days)
        log.info("Пересчитаны медианы по %d сегментам", segments)
    finally:
        await storage.dispose()


def _build_collectors(settings: Settings) -> list:
    """Все активные коллекторы площадок. Добавление площадки = строка здесь.
    Авито — через прокси (если задан в .env), т.к. на VPS его банят напрямую."""
    return [
        CianCollector(),
        YandexCollector(),
        AvitoCollector(proxy=settings.avito_proxy or None),
    ]


def _build_pipeline(settings: Settings, storage: SqlStorage) -> tuple[Pipeline, TelegramPublisher]:
    publisher = TelegramPublisher.from_settings(settings)
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
    await storage.init()
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
    await storage.init()

    # Один общий Telegram-бот: и постинг в канал, и личные чаты подписчиков.
    bot: Bot | None = None
    if settings.bot_token and not settings.dry_run:
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        publisher = TelegramPublisher(
            dry_run=False,
            bot=bot,
            max_photos_personal=settings.max_photos_personal,
            max_photos_public=settings.max_photos_public,
            emoji=load_emoji(settings.emoji_path),
            show_renovation=settings.show_renovation_in_post,
            cta_text=settings.cta_text,
            cta_url=settings.cta_url,
            avito_proxy=settings.avito_proxy,
        )
    else:
        publisher = TelegramPublisher(
            dry_run=True,
            max_photos_personal=settings.max_photos_personal,
            max_photos_public=settings.max_photos_public,
            show_renovation=settings.show_renovation_in_post,
            cta_text=settings.cta_text,
            cta_url=settings.cta_url,
            avito_proxy=settings.avito_proxy,
        )

    pipeline = Pipeline(
        collectors=_build_collectors(settings),
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
        "serve: сбор/%dм · топ-%d %s (окно %dч) · личная/1м · vision=%s · бот=%s · dry=%s",
        settings.collect_interval_min,
        settings.max_posts_per_run,
        post_when,
        settings.public_lookback_hours,
        settings.vision_model if analyzer else "off",
        "on" if bot else "off",
        settings.dry_run,
    )

    async def enrich_job() -> None:
        """Дообогатить свежие Авито-лоты с детальных страниц: описание, залог,
        комиссия, полноразмер-фото (в выдаче их нет — поэтому у Авито не работали
        «Заехать», фильтр «без посредников» и текстовый скам-гейт). Вежливый темп;
        при блоке антибота проход прерывается до следующего сбора."""
        if settings.avito_detail_max_per_run <= 0:
            return
        try:
            since = _utcnow() - timedelta(hours=24)
            cands = await storage.unenriched_listings(
                Source.AVITO.value, since, settings.avito_detail_max_per_run
            )
            done = 0
            for listing in cands:
                if done:  # пауза между запросами (не перед первым)
                    await asyncio.sleep(settings.avito_detail_delay_sec)
                detail = await fetch_detail(
                    listing.url,
                    price=listing.price,
                    proxy=settings.avito_proxy or None,
                )
                if detail is None:
                    log.warning(
                        "Обогащение Авито: блок/ошибка после %d/%d — жду следующего сбора",
                        done,
                        len(cands),
                    )
                    break
                await storage.save_enrichment(
                    listing.source.value,
                    listing.external_id,
                    enriched_at=_utcnow(),
                    description=detail.description,
                    deposit_rub=detail.deposit_rub,
                    commission_pct=detail.commission_pct,
                    meters_included=detail.meters_included,
                    photos=detail.photos,
                )
                done += 1
            if done:
                log.info("Обогащение Авито: %d лотов", done)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обогащения Авито")

    async def collect_job() -> None:
        try:
            await storage.recompute_market_stats(window_days=settings.market_window_days)
            total = 0
            agg: dict[str, list[str]] = {}
            for p in profiles:
                new, statuses = await pipeline.collect_only(p)
                total += new
                for src, st in statuses.items():
                    agg.setdefault(src, []).append(st)
            log.info("Сбор: новых лотов = %d", total)
            runtime["last_collect_at"] = _utcnow()
            runtime["last_collect_new"] = total
            await _check_source_health(agg)  # 🔴/🟢 при смене состояния площадки
            # Сразу дообогащаем свежий Авито — до vision-прохода и постинга.
            await enrich_job()
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка сбора")
            await _alert_admin(
                f"⚠️ Джоб сбора упал: {type(exc).__name__}: {exc}",
                key="job:collect",
                cooldown_h=1,
            )

    async def public_job() -> None:
        if public_paused["on"]:
            log.info("Публичный постинг на паузе (/resumepublic — снять)")
            return
        try:
            since = _utcnow() - timedelta(hours=settings.public_lookback_hours)
            candidates = []
            for p in profiles:
                candidates.extend(await pipeline.score_recent(p, since))
            # Паблик-гейт по «красоте»: в канал только приличный ремонт.
            passed = [c for c in candidates if _passes_public_gate(c, settings)]
            # Канал голодает из-за отставшего vision? Кандидаты по скору есть, но
            # ни один не проанализирован → молчать нельзя, алертим админа.
            if candidates and not passed:
                n_unanalyzed = sum(1 for c in candidates if c.listing.analyzed_at is None)
                if n_unanalyzed == len(candidates) and settings.public_require_analyzed:
                    await _alert_admin(
                        f"⚠️ Канал голодает: все {n_unanalyzed} кандидатов слота не "
                        "прошли vision-анализ (vision отстаёт или стоит). "
                        "Проверь лог vision-прохода.",
                        key="starving",
                        cooldown_h=3,
                    )
            n = len(await pipeline.publish_top(passed))
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
        Алертим только на смене состояния (ok↔down) — без спама каждые 10 минут."""
        for src, results in agg.items():
            state = "ok" if "ok" in results else "down"
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
                        matches(listing, flt, ignore_renovation=True)
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
                best_urls = (
                    [listing.photos[i] for i in analysis.best_photos] if analysis else []
                )
                await storage.save_photo_analysis(
                    listing.source.value,
                    listing.external_id,
                    renovation=(analysis.renovation if analysis else None),
                    has_furniture=(analysis.has_furniture if analysis else None),
                    appeal=(analysis.appeal if analysis else None),
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

    async def personal_job() -> None:
        try:
            n = await personal.run_due(_utcnow())
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
        """Раз в сутки: триал-кончился / подписка-истекает / win-back. Дедуп по флагам."""
        if bot is None:
            return
        try:
            now = _utcnow()
            sent = 0
            for sub in await personal_store.lifecycle_candidates():
                kind, text = _lifecycle_nudge(sub, now, settings)
                if kind is None:
                    continue
                try:
                    await bot.send_message(sub.telegram_id, text)
                    await personal_store.mark_nudged(sub.telegram_id, kind)
                    sent += 1
                except Exception:  # noqa: BLE001 — заблокировал бота и т.п.
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

    scheduler = AsyncIOScheduler()
    scheduler.add_job(collect_job, "interval", minutes=max(1, settings.collect_interval_min))
    # Публичный постинг: по часам-пикам (MSK), иначе — почасовой запасной режим.
    post_hours = settings.public_post_hours.strip()
    if post_hours:
        scheduler.add_job(
            public_job, "cron", hour=post_hours, minute=0, timezone=ZoneInfo("Europe/Moscow")
        )
        log.info("Публичный постинг по часам MSK: %s", post_hours)
    else:
        scheduler.add_job(public_job, "interval", minutes=max(1, settings.poll_interval_min))
    scheduler.add_job(personal_job, "interval", minutes=1)
    if analyzer is not None:
        scheduler.add_job(vision_job, "interval", minutes=max(1, settings.vision_interval_min))
    if bot is not None:  # lifecycle-напоминания — раз в сутки в lifecycle_hour MSK
        scheduler.add_job(
            lifecycle_job,
            "cron",
            hour=settings.lifecycle_hour,
            minute=0,
            timezone=ZoneInfo("Europe/Moscow"),
        )
    scheduler.start()

    async def startup_jobs() -> None:
        # На старте — сбор + первый vision-проход (чтобы БД и личка сразу со свежими
        # данными и анализом). Публичный постинг НЕ дёргаем: иначе каждый рестарт =
        # пачка постов в канал. Он пойдёт по расписанию (раз в poll_interval_min).
        await collect_job()
        await vision_job()

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
                vqueue = len(await storage.unanalyzed_listings("Москва", since, 10_000))
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
                await collect_job()
                await message.answer(
                    f"✅ Собрано, новых: {runtime['last_collect_new']}. /health — детали."
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
                s = await personal_store.admin_stats(_utcnow())
                await message.answer(
                    "📊 <b>Подписчики</b>\n"
                    f"Всего: {s['subscribers']}\n"
                    f"Платных активных: {s['paid']} (из них на паузе: {s['paused']})\n"
                    f"На триале: {s['trial']}\n"
                    f"Истёкших: {s['expired']}\n"
                    f"Активных фильтров: {s['active_filters']}"
                )

            @dp.message(Command("admin"))
            async def _admin_help(message: Message) -> None:
                """Админ: список админ-команд."""
                if not _is_admin(message):
                    return
                await message.answer(
                    "🛠 <b>Админ-команды</b>\n"
                    "/health — состояние системы\n"
                    "/stats — подписчики\n"
                    "/collectnow — собрать сейчас\n"
                    "/postnow — опубликовать топ в канал\n"
                    "/pausepublic · /resumepublic — пауза канала\n"
                    "/grant &lt;id&gt; [дней] — выдать подписку"
                )

            asyncio.create_task(startup_jobs())  # параллельно с поллингом
            log.info("Бот на связи: /start /new /myfilters /status /buy · админ: /admin")
            await dp.start_polling(bot)
        else:
            await startup_jobs()
            await asyncio.Event().wait()  # dry-run/без токена: только публичный канал
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Остановка serve по сигналу")
    finally:
        scheduler.shutdown(wait=False)
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
    await storage.init()
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
