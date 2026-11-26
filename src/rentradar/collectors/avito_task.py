"""Private subprocess entry point for bounded Avito operations."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


def _parse(data: dict) -> dict | None:
    if os.name == "posix":
        import resource

        ceiling = 384 * 1024 * 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if soft == resource.RLIM_INFINITY or soft > ceiling:
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    from .avito import _detail_from_html

    detail = _detail_from_html(data["html"], "", data["price"], data["limit"])
    return detail._asdict() if detail is not None else None


async def _warm(data: dict) -> dict | None:
    from .avito_access import AvitoAccess

    access = AvitoAccess(
        proxy=data["proxy"], cookies_path=Path(data["cookies_path"]),
        profile_dir=Path(data["profile_dir"]),
    )
    if await access._warm_in_child():
        return {"ua": access.user_agent, "cookies": access.cookies}
    return None


if __name__ == "__main__":
    operation, = sys.argv[1:]
    data = json.load(sys.stdin.buffer)
    try:
        result = _parse(data) if operation == "parse" else asyncio.run(_warm(data))
        print(json.dumps({"result": result}, ensure_ascii=True))
    except MemoryError:
        print('{"error":"memory_limit"}')
