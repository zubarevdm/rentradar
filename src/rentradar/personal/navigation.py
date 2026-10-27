"""Visible inline navigation, shared by the chat and listing captions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ACTIONS = (
    ("new", "🔍 Новый поиск"),
    ("filters", "📋 Мои фильтры"),
    ("status", "ℹ️ Статус"),
    ("buy", "💳 Подписка"),
    ("invite", "🎁 Пригласить"),
    ("support", "🆘 Поддержка"),
)


def main_menu() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"nav:{key}") for key, label in ACTIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + 2] for i in range(0, 6, 2)])


def listing_keyboard(url: str, *, personal: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Открыть объявление →", url=url)]]
    if personal:
        rows[0].append(InlineKeyboardButton(text="☰ Меню", callback_data="nav:menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)
