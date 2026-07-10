"""Конфигурация RentRadar.

Секреты и режимы — из окружения/`.env` (`Settings`). Параметры поиска и веса
скоринга — из YAML (`SearchProfile`), чтобы менять «вкусность» без правки кода,
как требовал заказчик.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import DealType, PropertyType


class Settings(BaseSettings):
    """Глобальные настройки из окружения (префикс RENTRADAR_)."""

    model_config = SettingsConfigDict(
        env_prefix="RENTRADAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    public_channel: str = ""
    private_channel: str = ""
    # Годовой OAuth-токен Claude Code (claude setup-token) — для админ-команды /cc.
    # Пусто = /cc выключен. Работает от подписки Claude.ai, без API-биллинга.
    claude_token: str = ""
    admin_id: int | None = None  # основной админ (на него шлём системные алерты)
    admin_ids: str = ""  # доп. админы через запятую: "111,222,333"

    dry_run: bool = True
    db_path: Path = Path("data/rentradar.db")
    profiles_path: Path = Path("profiles.yaml")
    emoji_path: Path = Path("emoji.json")
    log_level: str = "INFO"

    # HTTP-прокси для Авито (web-unblocker, решающий антибот, или обычный прокси).
    # Пусто = напрямую (ОК на домашнем IP; на VPS Авито банит → нужен прокси).
    # Формат: "http://user:pass@host:port".
    avito_proxy: str = ""
    # Дообогащение Авито с детальной страницы (описание/залог/комиссия/полноразмер-
    # фото — в выдаче их нет). Кап запросов за проход и пауза между ними — чтобы не
    # злить антибот; проход идёт после каждого сбора. 0 в капе = обогащение выключено.
    avito_detail_max_per_run: int = 8
    avito_detail_delay_sec: float = 8.0

    # Часы публичного постинга (MSK, через запятую) — пики активности: утро/обед/
    # вечер/перед сном. Пусто → почасовой режим (poll_interval_min).
    public_post_hours: str = "9,13,18,22"
    # Запасной интервал постинга, если public_post_hours пуст.
    poll_interval_min: int = 60
    # Окно «свежести» для публичного топа, часов. Под постинг 4×/день расширено,
    # чтобы утренний пост захватил лоты за ночь (разрыв 22→8 ≈ 10ч). Дедуп по
    # posted_log не даёт постить один лот дважды между слотами.
    public_lookback_hours: int = 12
    # Частота центрального сбора площадок в минутах (питает канал и личного бота).
    collect_interval_min: int = 10
    # Окно для расчёта рыночных медиан, дней (старые цены не тянут медиану).
    # Сами лоты НЕ удаляются — окно влияет только на расчёт, история хранится.
    market_window_days: int = 90
    # Пауза между постами, сек — защита от флуд-лимитов Telegram.
    post_delay_sec: float = 3.0
    # Максимум постов за слот — ГЛОБАЛЬНЫЙ топ-N по всем профилям (канал-тизер).
    max_posts_per_run: int = 5
    # Сколько фото класть в пост (1..10). В личке больше (5-7), в канале меньше (3-5).
    max_photos_personal: int = 7
    max_photos_public: int = 5
    # CTA-ссылка в конце поста. В КАНАЛЕ ведём на бота (читатель настроит свой
    # поиск), в ЛИЧКЕ — на публичный канал (подписчик увидит общий поток и
    # перешлёт друзьям). Пустой URL → строки нет в этом контексте.
    cta_text: str = "FlatLikeThat — найти квартиру"  # в канале → бот
    cta_url: str = "https://t.me/FlatLike_bot"
    cta_public_text: str = "FlatLikeThat — найти квартиру"  # в личке → паблик (тот же текст)
    cta_public_url: str = "https://t.me/flatslike"

    # ── Персональный (платный) бот ─────────────────────────────────────
    # Бесплатных отправок до пайвола.
    free_sends_limit: int = 5
    # Максимум активных фильтров на пользователя.
    max_filters_per_user: int = 3
    # Окно «свежести» лотов для матчинга, часов. 7 дней: показываем все актуальные
    # варианты, а не только за сутки. Повторов не будет — дедуп по content_key
    # (was_sent на пользователя): уже отправленную квартиру второй раз не пришлём.
    personal_lookback_hours: int = 168
    # Максимум отправок одному пользователю за одну проверку (антифлуд).
    personal_max_per_check: int = 5
    # Защита контента от кражи конкурентами: запрет пересылки/сохранения сообщений
    # (Telegram protect_content). В боте (платный контент) — включаем всегда. В
    # канале ОСТОРОЖНО: убивает пересылки друзьям = главный канал роста, поэтому
    # по умолчанию выключено — включай, только если защита важнее виральности.
    protect_content_bot: bool = True
    protect_content_public: bool = False
    # Оплата ЮKassa: provider-token из @BotFather (пусто = оплата выключена).
    yookassa_provider_token: str = ""
    # Цена подписки, ₽, и срок, дней.
    sub_price_rub: int = 499
    sub_days: int = 30
    # Сколько дней получает пригласивший, когда приглашённый ОПЛАТИЛ подписку.
    referral_days: int = 7
    # Максимум дней паузы (заморозки) подписки — потом авто-возобновление.
    pause_max_days: int = 2
    # Разовый бонус за подписку на публичный канал, дней.
    channel_bonus_days: int = 7
    # Lifecycle: за сколько дней до конца подписки напомнить; сколько дней после
    # истечения слать win-back. Час суточного прогона напоминаний (MSK).
    lifecycle_expiry_days: int = 3
    lifecycle_winback_days: int = 7
    lifecycle_hour: int = 12

    # ── Vision: анализ фото нейросетью (выбор лучших кадров + оценка ремонта) ──
    # Пустой ключ = vision выключен (постим как раньше, без анализа). Эндпоинт по
    # умолчанию — OpenAI-совместимый слой Gemini; модель — самый дешёвый flash-lite.
    vision_api_key: str = ""
    vision_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    vision_model: str = "gemini-2.5-flash-lite"
    vision_max_images: int = 12  # сколько фото слать в один вызов (кап стоимости)
    vision_max_per_run: int = 15  # сколько лотов анализировать за один проход
    vision_interval_min: int = 5  # как часто крутить vision-проход
    # Не тратить токены на лоты, которые даже с идеальной красотой (appeal=100,
    # +12.5 к скору) не дотянут до публикации. Порог паблика 60 → дефолт ~48.
    # Поставь 0, чтобы анализировать всё (нужно, если личка ищет по ремонту дёшево).
    vision_min_score: int = 48
    # Паблик-гейт по «красоте»: ремонты, которые НЕ пускаем в канал, и мин. appeal.
    # Неанализированные лоты пропускаются (чтобы канал не голодал при лаге vision).
    public_block_renovations: list[str] = Field(
        default_factory=lambda: ["needs_repair", "soviet"]
    )
    # WOW-порог красоты для канала. Паблик — витрина-приманка: постим ВИЗУАЛЬНО
    # эффектные квартиры (высокий appeal), а не «лучшие сделки» — лучшие сделки
    # держим для бота (там персональный поток по фильтрам, всегда ценнее). Так
    # зритель канала думает «вау, хочу такую» и идёт в бота искать под себя.
    public_min_appeal: int = 75
    # Ранжировать канал по красоте (appeal), а не по «вкусности» (недооценке).
    # Тем самым канал = эмоция/картинка, бот = выгода/персонализация.
    public_rank_by_appeal: bool = True
    # В канал пускать только проверенные vision лоты (иначе скам-оверлей на фото
    # может проскочить до анализа). Выключить, если vision выключен — иначе канал
    # будет голодать.
    public_require_analyzed: bool = True
    # Показывать ли оценку ремонта/красоты в посте. Выключено: строка «ремонт:
    # simple» занижает восприятие квартир — параметр ведём скрыто (скоринг,
    # фильтры и паблик-гейт им пользуются как раньше). True — вернуть для отладки.
    show_renovation_in_post: bool = False

    @field_validator("admin_id", mode="before")
    @classmethod
    def _blank_admin_id_to_none(cls, v: object) -> object:
        # Пустое поле в .env (RENTRADAR_ADMIN_ID=) приходит как "" — трактуем как None.
        return None if v in ("", None) else v

    @property
    def admins(self) -> set[int]:
        """Все админы: основной admin_id + перечисленные в admin_ids."""
        result: set[int] = set()
        if self.admin_id is not None:
            result.add(self.admin_id)
        for part in self.admin_ids.split(","):
            part = part.strip()
            if part.isdigit():
                result.add(int(part))
        return result

    @property
    def db_url(self) -> str:
        """URL для async SQLAlchemy поверх SQLite."""
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"


class ScoringWeights(BaseModel):
    """Веса сигналов «вкусности». Сумма не обязана равняться 1 — нормируем сами.

    `condition` — состояние/красота по vision-оценке (appeal). ~25% веса: ощутимо
    влияет на ранг, но цена/рынок остаются главными."""

    below_market: float = 0.35
    price_per_m2: float = 0.18
    location: float = 0.12
    freshness: float = 0.10
    condition: float = 0.25


class FraudRules(BaseModel):
    """Жёсткие правила фрод-фильтра (anti-skam guard)."""

    # Дисконт выше этого порога считаем подозрительным (вероятная приманка).
    suspicious_discount: float = 0.55
    # Минимально допустимая цена аренды (отсев мусора/ошибок), ₽/мес.
    min_price: int = 5_000
    # Требовать хотя бы одно фото.
    require_photos: bool = True


class SearchProfile(BaseModel):
    """Именованный профиль поиска — то, что пользователь «выбирает».

    Содержит и фильтры площадки, и пороги/веса скоринга. Несколько профилей
    позволяют вести разные стратегии («студии у метро до 40к» и т.п.).
    """

    name: str
    city: str
    deal_type: DealType = DealType.LONG_RENT
    property_types: list[PropertyType] = Field(default_factory=list)

    price_min: int | None = None
    price_max: int | None = None
    rooms: list[int] = Field(default_factory=list)
    area_min: float | None = None
    area_max: float | None = None
    metro_only: bool = False

    # Порог TastyScore для публикации, [0..100].
    publish_threshold: float = 60.0

    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    fraud: FraudRules = Field(default_factory=FraudRules)


def load_profiles(path: Path) -> list[SearchProfile]:
    """Загрузить профили поиска из YAML. Пустой/отсутствующий файл → []."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("profiles", [])
    return [SearchProfile.model_validate(item) for item in raw]


def load_emoji(path: Path) -> dict:
    """Карта кастом-эмодзи (slot/линия → custom_emoji_id) из JSON. Нет файла → {}."""
    import json

    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}
