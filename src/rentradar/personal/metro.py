"""Поиск станций метро по части названия (с прощением опечаток).

Локальный граф Москвы независим от объявлений. Поиск допускает части названий
и опечатки, а соседние станции выбираются только по связям одной линии.
"""

from __future__ import annotations

import difflib
import hashlib
from collections import deque
from dataclasses import dataclass

from ..models import _normalize_text
from .metro_data import ALIASES, ROUTES


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    line: str
    line_name: str

    @property
    def key(self) -> str:
        return f"{self.name} · {self.line}"

    @property
    def aliases(self) -> tuple[str, ...]:
        special = {
            ("4", "Деловой центр"): ("Выставочная",),
            ("4", "Москва-Сити"): ("Международная",),
            ("D1", "Москва-Сити"): ("Тестовская", "Тестовская (Москва-Сити)"),
            ("14", "Москва-Сити"): ("Деловой центр",),
            ("D4", "Москва-Сити"): ("Тестовская",),
        }
        return (*ALIASES.get(self.name, ()), *special.get((self.line, self.name), ()))


STATIONS: dict[str, Station] = {}
EDGES: dict[str, list[str]] = {}


def _station_id(line: str, name: str) -> str:
    return hashlib.sha256(f"{line}:{name}".encode()).hexdigest()[:12]


for _line, _label, _path in ROUTES:
    _ids = []
    for _name in _path.split("|"):
        _id = _station_id(_line, _name)
        STATIONS[_id] = Station(_id, _name, _line, _label)
        EDGES.setdefault(_id, [])
        _ids.append(_id)
    for _a, _b in zip(_ids, _ids[1:], strict=False):
        if _b not in EDGES[_a]:
            EDGES[_a].append(_b)
        if _a not in EDGES[_b]:
            EDGES[_b].append(_a)


def search_stations(query: str) -> list[Station]:
    q = _normalize_text(query)
    if not q:
        return []
    exact = [s for s in STATIONS.values()
             if q in {_normalize_text(n) for n in (s.name, s.key, *s.aliases)}]
    if exact or " · " in query:
        return exact
    names = list(dict.fromkeys(n for s in STATIONS.values() for n in (s.name, *s.aliases)))
    found = suggest_metros(query, names, 8)
    return list(dict.fromkeys(s for name in found for s in STATIONS.values()
                              if name in (s.name, *s.aliases)))


def station_matches(
    selected: str, name: str, line: str | None = None, color: str | None = None,
) -> bool:
    """Legacy unqualified filters keep their behavior; new keys retain the line."""
    q = _normalize_text(selected)
    name_part, _, qualified_line = name.partition(" · ")
    line = line or qualified_line or None
    n = _normalize_text(name_part)
    candidates = [s for s in STATIONS.values()
                  if n in {_normalize_text(v) for v in (s.name, *s.aliases)}]
    if " · " not in selected:
        return q == n or any(q in {_normalize_text(v) for v in (s.name, *s.aliases)}
                             for s in candidates)
    selected_name, _, selected_line = selected.partition(" · ")
    matches = [s for s in candidates if s.line == selected_line
               and _normalize_text(selected_name) == _normalize_text(s.name)]
    if not line and color:
        # Exact source palette values only: do not guess a line from a nearby color.
        palette = {
            "ed1b35": "1", "e42313": "1", "44b85c": "2", "4fb04f": "2",
            "0078be": "3", "0572b9": "3", "00bfff": "4", "24bcef": "4",
            "894e35": "5", "915133": "5", "f58220": "6", "ef7e24": "6",
            "8e479c": "7", "943e90": "7", "a1a2a3": "9", "acadaf": "9",
            "9aca3c": "10", "b4d445": "10", "79cdcd": "11", "82c0c0": "11",
            "a1b3d4": "12", "ffa8af": "14", "de64a1": "15",
            "f6a800": "D1", "efb71d": "D1", "e74280": "D2", "ea5b0c": "D3",
            "e95b0c": "D3", "40b280": "D4", "47b485": "D4",
        }
        line = palette.get(color.lower().lstrip("#"))
    if line:
        return bool(matches) and line.upper().replace("МЦД-", "D") == selected_line.upper()
    # A source without line information cannot identify an ambiguous station.
    return bool(matches) and len({s.line for s in candidates}) == 1


def neighbours(station_id: str) -> list[tuple[Station, str]]:
    """Bounded BFS within one line, including all branches and ring closure."""
    root = STATIONS[station_id]
    result = [(root, "Основная")]
    seen = {station_id}
    queue = deque((n, 1, STATIONS[n].name) for n in EDGES[station_id])
    while queue:
        sid, distance, direction = queue.popleft()
        if sid in seen or distance > 3:
            continue
        seen.add(sid)
        result.append((STATIONS[sid], f"через {direction} · {distance} пер."))
        queue.extend((n, distance + 1, direction) for n in EDGES[sid] if n not in seen)
    return result


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
        close = difflib.get_close_matches(q, list(norm.values()), n=limit, cutoff=0.6)
        out.extend(c for value in close for c in candidates if norm[c] == value and c not in out)

    return out[:limit]
