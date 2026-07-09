"""Поиск станций метро по части названия (с прощением опечаток).

Кандидаты берутся из реальных данных (станции, встреченные в собранных лотах),
поэтому выбранное значение гарантированно совпадает с тем, что в объявлениях.
"""

from __future__ import annotations

import difflib

from ..models import _normalize_text


def suggest_metros(query: str, candidates: list[str], limit: int = 8) -> list[str]:
    """Станции, подходящие под запрос: сначала подстрока, затем нечёткие (опечатки)."""
    q = _normalize_text(query)
    if not q:
        return []
    norm = {c: _normalize_text(c) for c in candidates}

    substring = [c for c in candidates if q in norm[c]]

    # Нечёткое совпадение — ловит опечатки («прафсаюзная» → «Профсоюзная»).
    close = set(difflib.get_close_matches(q, list(norm.values()), n=limit * 2, cutoff=0.55))
    fuzzy = [c for c in candidates if norm[c] in close and c not in substring]

    out: list[str] = []
    seen: set[str] = set()
    for c in (*substring, *fuzzy):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:limit]
