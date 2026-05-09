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
