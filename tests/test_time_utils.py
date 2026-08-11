import pytest

from app.time_utils import format_time, parse_time


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("00:00:00.000", 0),
        ("00:00:05.250", 5.25),
        ("01:02:03.004", 3723.004),
        ("120:59:59.999", 435599.999),
        ("00:00:01.5", 1.5),
    ],
)
def test_parse_time(text, seconds):
    assert parse_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "text",
    ["", "1:2:3", "00:60:00.000", "00:00:60", "-01:00:00", "abc"],
)
def test_invalid_time(text):
    with pytest.raises(ValueError):
        parse_time(text)


def test_format_round_trip():
    value = 7384.567
    assert format_time(value) == "02:03:04.567"
    assert parse_time(format_time(value)) == pytest.approx(value)
