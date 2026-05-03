from phoson_cli.repl import _message_preview


def test_message_preview_string_short() -> None:
    result = _message_preview("Hello, world!", max_len=50)
    assert result == "Hello, world!"


def test_message_preview_string_truncated() -> None:
    long_text = "a" * 100
    result = _message_preview(long_text, max_len=20)
    assert len(result) == 20
    assert result.endswith("…")


def test_message_preview_collapse_whitespace() -> None:
    result = _message_preview("Hello    world\n\nfoo", max_len=50)
    assert result == "Hello world foo"


def test_message_preview_non_string() -> None:
    result = _message_preview(["item1", "item2"], max_len=50)
    assert "item1" in result


def test_message_preview_max_len_exact() -> None:
    exact = "Hello"
    result = _message_preview(exact, max_len=5)
    assert result == "Hello"
