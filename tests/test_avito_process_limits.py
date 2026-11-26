from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from rentradar.collectors import avito
from rentradar.runtime import run_child


async def test_parser_subprocess_preserves_detail(monkeypatch):
    html = (Path(__file__).parent / "fixtures/avito-8517144096-terms.html").read_text("utf-8")
    monkeypatch.setattr(avito, "_avito_fetch_html", AsyncMock(return_value=html))
    result = await avito.fetch_detail("https://www.avito.ru/test", price=45000, parse_timeout=12)
    assert result is not None
    assert result == avito.parse_detail(html, price=45000)


async def test_timeout_kills_child_and_preserves_next_operation():
    with pytest.raises(TimeoutError):
        await run_child("timeit", "-r", "1", "-n", "1", "__import__('time').sleep(15)",
                        data=b"", seconds=0.25)
    assert b"per loop" in await run_child("timeit", "-r", "1", "-n", "1", "pass",
                                          data=b"", seconds=5)
