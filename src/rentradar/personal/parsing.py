"""Парсеры пользовательского ввода для диалога настройки фильтра.

Чистые функции — тестируются без aiogram. Терпимы к разным форматам ввода:
«30000-70000», «до 70000», «от 30000», «пропустить» и т.п.
"""

from __future__ import annotations

import re

_SKIP_WORDS = {"пропустить", "-", "любой", "любые", "нет", "skip"}

_ROOM_WORDS = {"студия": 0, "studio": 0, "ст": 0}


def is_skip(text: str) -> bool:
    return text.strip().lower() in _SKIP_WORDS


def parse_rooms(text: str) -> list[int]:
    """«студия, 1, 2» / «1 2 3» → [0, 1, 2, 3]. Пусто/пропустить → []."""
    if is_skip(text):
        return []
    rooms: list[int] = []
    for token in re.split(r"[,\s]+", text.strip().lower()):
        if not token:
            continue
        if token in _ROOM_WORDS:
            rooms.append(_ROOM_WORDS[token])
        elif token.rstrip("+").isdigit():
            rooms.append(int(token.rstrip("+")))
    return sorted(set(rooms))


def parse_price_range(text: str) -> tuple[int | None, int | None]:
    """«30000-70000» → (30000, 70000); «до 70000» → (None, 70000);
    «от 30000» → (30000, None); пропустить → (None, None)."""
    if is_skip(text):
        return None, None
    nums = [int(n) for n in re.findall(r"\d+", text.replace(" ", ""))]
    low = text.lower()
    if "до" in low and nums:
        return None, nums[0]
    if "от" in low and nums:
        return nums[0], None
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    if len(nums) == 1:
        # одно число без слов трактуем как максимум
        return None, nums[0]
    return None, None


def parse_csv(text: str) -> list[str]:
    """«Парк культуры, Фрунзенская» → [...]; пропустить → []."""
    if is_skip(text):
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_int_or_none(text: str) -> int | None:
    if is_skip(text):
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None
