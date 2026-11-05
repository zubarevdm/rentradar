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


def nudge_keyboard(*, setup: bool = False) -> InlineKeyboardMarkup:
    """Buttons under lifecycle reminders: one tap to the next useful step."""
    if setup:
        rows = [[InlineKeyboardButton(text="🔍 Настроить поиск", callback_data="nav:new")]]
    else:
        rows = [
            [InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="nav:buy")],
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="nav:invite")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
