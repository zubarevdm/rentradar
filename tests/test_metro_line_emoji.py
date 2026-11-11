from rentradar.publisher.telegram import _line_circle, _metro_line

EM = {"lines": {"1": "id1", "2": "id2", "5": "id5", "15": "id15", "4": "id4", "8": "id8"}}


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def test_station_restricts_line_choice():
    # Розовый МЦД-2 у Царицыно — не Некрасовская: станция на 2 и D2.
    assert _metro_line("Царицыно", *_rgb("#DF467C")) == "D2"
    # Некрасовская у Циана малиновая — не Сокольническая.
    assert _metro_line("Некрасовка", *_rgb("DB0A5B")) == "15"
    # Графит Рублёво-Архангельской — не Кольцевая.
    assert _metro_line("Бульвар Генерала Карбышева", *_rgb("27303F")) == "17"
    # Пересадочная: цвет выбирает из линий станции.
    assert _metro_line("Новогиреево", *_rgb("#3BB183")) == "D4"
    assert _metro_line("Новогиреево", *_rgb("#FFCD1C")) == "8"


def test_unknown_station_falls_back_to_color():
    assert _metro_line("Несуществующая", *_rgb("#ED1B35")) == "1"
    assert _metro_line(None, *_rgb("#44B85C")) == "2"


def test_line_without_custom_emoji_gets_plain_circle():
    assert _line_circle("DB0A5B", EM, "Некрасовка").startswith('<tg-emoji emoji-id="id15">')
    assert "tg-emoji" not in _line_circle("27303F", EM, "Бульвар Генерала Карбышева")
