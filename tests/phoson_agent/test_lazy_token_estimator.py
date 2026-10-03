"""Tokenizer tables must not be decoded while opening an empty session."""

from unittest.mock import Mock

import pytest

from phoson_agent.plugins import summarizer


@pytest.mark.parametrize(
    ("provider", "encoding_name"),
    [
        ("openai", "o200k_base"),
        ("anthropic", "cl100k_base"),
        ("unknown", "cl100k_base"),
    ],
)
def test_encoding_loads_once_on_first_estimate(monkeypatch, provider, encoding_name):
    encoding = Mock()
    encoding.encode.return_value = [1, 2]
    get_encoding = Mock(return_value=encoding)
    monkeypatch.setattr(summarizer.tiktoken, "get_encoding", get_encoding)

    estimator = summarizer.TokenEstimator.for_provider(provider)
    get_encoding.assert_not_called()
    assert estimator.count_messages([]) == 0
    assert estimator.count_tools([]) == 0
    assert estimator.count_system(None) == 0
    get_encoding.assert_not_called()

    assert estimator.count_text("hello") == 2
    assert estimator.count_text("again") == 2
    assert estimator._encoding is encoding
    get_encoding.assert_called_once_with(encoding_name)


def test_encoding_failure_can_be_retried(monkeypatch):
    encoding = Mock()
    encoding.encode.return_value = [1]
    get_encoding = Mock(side_effect=[RuntimeError("unavailable"), encoding])
    monkeypatch.setattr(summarizer.tiktoken, "get_encoding", get_encoding)
    estimator = summarizer.TokenEstimator()
    with pytest.raises(RuntimeError, match="unavailable"):
        estimator.count_text("hello")
    assert estimator.count_text("hello") == 1
    assert get_encoding.call_count == 2
