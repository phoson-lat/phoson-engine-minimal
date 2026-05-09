import datetime

from phoson_agent._internals import duration_ms, to_result_text


def test_to_result_text_with_string_returns_same_value() -> None:
    assert to_result_text("ok") == "ok"


def test_to_result_text_with_dict_serializes_ascii_json() -> None:
    text = to_result_text({"city": "Querétaro", "temp": 27})

    assert text == '{"city": "Quer\\u00e9taro", "temp": 27}'


def test_duration_ms_calculates_delta_in_milliseconds() -> None:
    started_at = datetime.datetime(2026, 1, 1, 10, 0, 0, tzinfo=datetime.UTC)
    ended_at = datetime.datetime(2026, 1, 1, 10, 0, 1, 250000, tzinfo=datetime.UTC)

    assert duration_ms(started_at, ended_at) == 1250
