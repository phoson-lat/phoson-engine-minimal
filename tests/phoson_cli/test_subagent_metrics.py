"""Tests for the metrics wire-format between subagent producer and parser."""

from phoson_cli.tools.subagent_panel import (
    AgentStatus,
    format_agent_block,
    format_metrics_line,
    parse_subagent_metrics,
)


def test_metrics_roundtrip_single_agent() -> None:
    """The line emitted by ``format_metrics_line`` must round-trip."""
    line = format_metrics_line(
        duration_ms=1234,
        input_tokens=42,
        output_tokens=99,
        cost_usd=0.00123,
        credits=0,
    )
    block = format_agent_block(
        index=0,
        task_preview="hola",
        body="cuerpo",
        metrics_line=line,
    )

    metrics = parse_subagent_metrics(block)

    assert len(metrics) == 1
    m = metrics[0]
    assert m.index == 0
    assert m.task == "hola"
    assert m.status == AgentStatus.DONE
    assert m.duration_ms == 1234
    assert m.input_tokens == 42
    assert m.output_tokens == 99
    assert m.cost_usd == 0.00123
    assert m.error is None


def test_metrics_roundtrip_multiple_agents() -> None:
    blocks: list[str] = []
    for i in range(3):
        line = format_metrics_line(
            duration_ms=100 * (i + 1),
            input_tokens=10 * (i + 1),
            output_tokens=20 * (i + 1),
            cost_usd=0.001 * (i + 1),
        )
        blocks.append(
            format_agent_block(
                index=i,
                task_preview=f"task-{i}",
                body=f"result-{i}",
                metrics_line=line,
            )
        )
    output = "\n\n".join(blocks)

    metrics = parse_subagent_metrics(output)

    assert [m.index for m in metrics] == [0, 1, 2]
    assert [m.duration_ms for m in metrics] == [100, 200, 300]
    assert [m.input_tokens for m in metrics] == [10, 20, 30]
    assert [m.output_tokens for m in metrics] == [20, 40, 60]


def test_metrics_parse_error_block() -> None:
    block = format_agent_block(
        index=2,
        task_preview="bad task",
        body="",
        error="boom",
    )

    metrics = parse_subagent_metrics(block)

    assert len(metrics) == 1
    m = metrics[0]
    assert m.index == 2
    assert m.status == AgentStatus.ERROR
    assert m.error == "boom"


def test_metrics_parse_ignores_garbage_blocks() -> None:
    """Malformed blocks must be skipped without raising."""
    output = (
        "=== Agent X: bad ===\n"  # non-numeric index → skipped
        "garbage\n\n"
        + format_agent_block(
            index=0,
            task_preview="ok",
            body="r",
            metrics_line=format_metrics_line(
                duration_ms=10,
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
            ),
        )
    )

    metrics = parse_subagent_metrics(output)

    assert len(metrics) == 1
    assert metrics[0].index == 0


def test_metrics_parse_handles_missing_metrics_line() -> None:
    block = "=== Agent 0: solo ===\nresult without metrics"

    metrics = parse_subagent_metrics(block)

    assert len(metrics) == 1
    assert metrics[0].index == 0
    assert metrics[0].status == AgentStatus.DONE
    assert metrics[0].duration_ms == 0
    assert metrics[0].cost_usd == 0.0


def test_fallback_model_roundtrip() -> None:
    """``fallback_model`` survives the wire format and marks the metrics."""
    line = format_metrics_line(
        duration_ms=500,
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.0,
        credits=0,
        fallback_model="anthropic/claude-3.5-haiku",
    )
    block = format_agent_block(
        index=2, task_preview="tarea", body="ok", metrics_line=line
    )
    metrics = parse_subagent_metrics(block)
    assert len(metrics) == 1
    assert metrics[0].fallback_model == "anthropic/claude-3.5-haiku"
    assert metrics[0].status == AgentStatus.DONE


def test_no_fallback_model_by_default() -> None:
    line = format_metrics_line(
        duration_ms=500,
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.0,
        credits=0,
    )
    metrics = parse_subagent_metrics(
        format_agent_block(index=0, task_preview="t", body="b", metrics_line=line)
    )
    assert metrics[0].fallback_model is None


def test_summary_renders_fallback_warning() -> None:
    """A fallback agent renders with a warning style and a caption note."""
    from phoson_cli.tools.subagent_panel import render_subagent_summary

    metrics = parse_subagent_metrics(
        format_agent_block(
            index=0,
            task_preview="tarea",
            body="ok",
            metrics_line=format_metrics_line(
                duration_ms=500,
                input_tokens=10,
                output_tokens=20,
                cost_usd=0.0,
                credits=0,
                fallback_model="anthropic/claude-3.5-haiku",
            ),
        )
    )
    table = render_subagent_summary(metrics)
    assert table is not None
    assert table.caption is not None
    assert "fallback" in table.caption
    assert "anthropic/claude-3.5-haiku" in table.caption


def test_summary_without_fallback_has_no_caption() -> None:
    from phoson_cli.tools.subagent_panel import render_subagent_summary

    metrics = parse_subagent_metrics(
        format_agent_block(
            index=0,
            task_preview="tarea",
            body="ok",
            metrics_line=format_metrics_line(
                duration_ms=500,
                input_tokens=10,
                output_tokens=20,
                cost_usd=0.0,
                credits=0,
            ),
        )
    )
    table = render_subagent_summary(metrics)
    assert table is not None
    assert table.caption is None


# ── Cell formatters (Time / Tokens / Cost) ────────────────────────────────────


def test_format_duration_units() -> None:
    from phoson_cli.tools.subagent_panel import _format_duration

    assert _format_duration(0) == "0ms"
    assert _format_duration(450) == "450ms"
    assert _format_duration(999) == "999ms"
    # Seconds keep one decimal (dropped when whole).
    assert _format_duration(1000) == "1s"
    assert _format_duration(4200) == "4.2s"
    assert _format_duration(9999) == "10s"  # rounds 9.999s up
    assert _format_duration(12300) == "12.3s"
    # Minutes / hours accumulate h:m:s with no padding.
    assert _format_duration(60000) == "1m0s"
    assert _format_duration(123000) == "2m3s"
    assert _format_duration(3600000) == "1h0m0s"
    assert _format_duration(3662000) == "1h1m2s"
    assert _format_duration(7325000) == "2h2m5s"


def test_abbr_tokens_units_and_overflow() -> None:
    from phoson_cli.formatting import abbr_tokens

    # Below 1000 stays raw.
    assert abbr_tokens(0) == "0"
    assert abbr_tokens(42) == "42"
    assert abbr_tokens(999) == "999"
    # K / M / B, trailing zeros dropped.
    assert abbr_tokens(1000) == "1K"
    assert abbr_tokens(1050) == "1.1K"
    assert abbr_tokens(1200) == "1.2K"
    assert abbr_tokens(1_000_000) == "1M"
    assert abbr_tokens(1_234_567) == "1.2M"
    assert abbr_tokens(1_500_000_000) == "1.5B"
    # Rounding must not overflow a unit: 999999 -> 1M, not 1000K.
    assert abbr_tokens(999_999) == "1M"
    assert abbr_tokens(999_999_999) == "1B"


def test_format_tokens_abbreviates_and_keeps_small_values() -> None:
    from phoson_cli.tools.subagent_panel import _format_tokens

    # Zero stays an em-dash; small values unchanged (backwards compatible).
    assert _format_tokens(0, 0) == "—"
    assert _format_tokens(42, 17) == "42in / 17out"
    # Each side abbreviates independently above 1000.
    assert _format_tokens(1200, 3400) == "1.2Kin / 3.4Kout"
    assert _format_tokens(1_200_000, 3_400_000) == "1.2Min / 3.4Mout"
    # One side can overflow while the other stays raw.
    assert _format_tokens(999_999, 1200) == "1Min / 1.2Kout"


def test_format_cost_unchanged() -> None:
    from phoson_cli.tools.subagent_panel import _format_cost

    assert _format_cost(0) == "—"
    assert _format_cost(0.0015) == "$0.00150"
