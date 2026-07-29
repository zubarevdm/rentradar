"""Поиск станций метро по части названия (с прощением опечаток).

Кандидаты берутся из реальных данных (станции, встреченные в собранных лотах),
поэтому выбранное значение гарантированно совпадает с тем, что в объявлениях.
"""

from __future__ import annotations

import difflib

from ..models import _normalize_text


def suggest_metros(query: str, candidates: list[str], limit: int = 6) -> list[str]:
    """Станции под запрос, компактно и по релевантности: сначала те, что НАЧИНАЮТСЯ
    с введённого (самое ожидаемое, сокращает ввод), затем подстрока, затем нечёткие
    (опечатки) — и только если точных мало и запрос от 3 букв. Не спамим всю ветку."""
    q = _normalize_text(query)
    if not q:
        return []
    norm = {c: _normalize_text(c) for c in candidates}

    prefix = [c for c in candidates if norm[c].startswith(q)]
    substring = [c for c in candidates if q in norm[c] and c not in prefix]
    out: list[str] = [*prefix, *substring]

    # Нечёткое (опечатки «прафсаюзная»→«Профсоюзная») — добираем, если точных мало.
    if len(out) < limit and len(q) >= 3:
        close = set(difflib.get_close_matches(q, list(norm.values()), n=limit, cutoff=0.6))
        out.extend(c for c in candidates if norm[c] in close and c not in out)

    return out[:limit]
