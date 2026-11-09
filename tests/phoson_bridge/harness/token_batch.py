"""Tests for token coalescing (`_TOKEN_BATCH_SECONDS`).

Run them with ``uv run python tests/phoson_bridge/harness/token_batch.py``
(``test_harnesses.py`` runs every harness under pytest).

No pytest: asserts and a `main()` that prints what it checks. The engine emits
one `agent.event` PER TOKEN (hundreds per second); each one is an NDJSON line +
IPC + dispatch in the webview. The bridge buffers them for 30 ms and flushes
them in a single notification. What the UI sees:

1. N consecutive tokens go out as ONE line with the concatenated content (and
   in order).
2. Reasoning and text tokens travel in the same window without mixing.
3. A later event from the SAME session (e.g. `session.assistant.done`) never
   overtakes the buffered tokens: the text arrives before the UI closes the
   message.
4. The timer flush happens even if no further event arrives.
5. Non-token notifications are left untouched.
"""

import io
import sys
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phoson_bridge.server as server  # noqa: E402


class _RecordingStream(io.StringIO):
    """The bridge's `protocol_out`: keeps what was written and does nothing on flush."""

    def flush(self) -> None:
        return None


class _Bridge:
    """A `Bridge` with its writer running (see `test_watchdog_models.py`)."""

    def __init__(self) -> None:
        self.out = _RecordingStream()
        self.bridge = server.Bridge(self.out)
        self.bridge._warm_task.cancel()
        self.writer = asyncio.create_task(self.bridge._writer())

    def log(self) -> str:
        return self.out.getvalue()

    def events(self) -> list[dict]:
        """The log's `agent.event` notifications, already parsed."""
        out = []
        for line in self.log().splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("method") == "agent.event":
                out.append(msg["params"]["event"])
        return out

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.log().splitlines() if line.strip()]

    async def close(self) -> None:
        self.writer.cancel()
        await asyncio.sleep(0)
        # Drain what is left in the queue: cancelling the writer can lose what
        # was queued in the same loop iteration (the output must reflect
        # EVERYTHING emitted, not just what the writer managed to flush).
        while True:
            try:
                _sid, line = self.bridge._out.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.out.write(line + "\n")


def _emit_token(bridge, sid: str, content: str, etype: str = "AgentTokenEvent") -> None:
    bridge._emit(
        "agent.event", {"sessionId": sid, "event": {"type": etype, "content": content}}
    )


async def _test_many_tokens_coalesce_into_one_line() -> None:
    harness = _Bridge()
    for piece in ("Ho", "la", " ", "mun", "do"):
        _emit_token(harness.bridge, "s1", piece)
    # No later events: the window timer flushes on its own.
    await asyncio.sleep(0.05)
    await harness.close()

    events = harness.events()
    assert len(events) == 1, (
        f"esperaba 1 evento coalescido, hubo {len(events)}: {events}"
    )
    assert events[0] == {"type": "AgentTokenEvent", "content": "Hola mundo"}, events
    print("  ok  N tokens seguidos salen como una sola notificación coalescida")


async def _test_reasoning_and_text_keep_their_own_batch() -> None:
    harness = _Bridge()
    _emit_token(harness.bridge, "s2", "pienso", "AgentReasoningEvent")
    _emit_token(harness.bridge, "s2", "luego ")
    _emit_token(harness.bridge, "s2", "escribo", "AgentReasoningEvent")
    _emit_token(harness.bridge, "s2", "exist")
    await asyncio.sleep(0.05)
    await harness.close()

    events = harness.events()
    texts = [e for e in events if e["type"] == "AgentTokenEvent"]
    reasoning = [e for e in events if e["type"] == "AgentReasoningEvent"]
    assert texts == [{"type": "AgentTokenEvent", "content": "luego exist"}], texts
    assert reasoning == [{"type": "AgentReasoningEvent", "content": "piensoescribo"}], (
        reasoning
    )
    print("  ok  tokens y reasoning se acumulan por separado y en orden")


async def _test_later_event_flushes_tokens_before_itself() -> None:
    harness = _Bridge()
    _emit_token(harness.bridge, "s3", "texto ")
    _emit_token(harness.bridge, "s3", "final")
    # The event that closes the turn: it must go out AFTER the tokens.
    harness.bridge._emit(
        "session.assistant.done", {"sessionId": "s3", "status": "done"}
    )
    await harness.close()

    lines = harness.lines()
    methods = [m.get("method") for m in lines]
    assert "agent.event" in methods and "session.assistant.done" in methods, methods
    assert methods.index("agent.event") < methods.index("session.assistant.done"), (
        f"los tokens se emitieron después del cierre del turno: {methods}"
    )
    print("  ok  un evento de cierre nunca se adelanta a los tokens acumulados")


async def _test_non_token_events_are_untouched() -> None:
    harness = _Bridge()
    harness.bridge._emit("session.user_message", {"sessionId": "s4", "text": "hola"})
    harness.bridge._emit("ui.flush", {"sessionId": "s4"})
    await asyncio.sleep(0.05)
    await harness.close()

    methods = [m.get("method") for m in harness.lines()]
    assert methods == ["session.user_message", "ui.flush"], methods
    print("  ok  las notificaciones que no son tokens conservan su forma")


async def _test_batches_are_per_session() -> None:
    harness = _Bridge()
    _emit_token(harness.bridge, "sa", "de A")
    _emit_token(harness.bridge, "sb", "de B")
    harness.bridge._emit(
        "session.assistant.done", {"sessionId": "sb", "status": "done"}
    )
    await asyncio.sleep(0.05)
    await harness.close()

    # sA is flushed by its timer; sB by the close of its turn. One session's
    # content never ends up in the other's notification.
    for line in harness.lines():
        if line.get("method") != "agent.event":
            continue
        sid, event = line["params"]["sessionId"], line["params"]["event"]
        assert sid in ("sa", "sb"), line
        assert event["content"] == f"de {sid[-1].upper()}", line
    print("  ok  el coalescido es por sesión (sin fugas entre workspaces)")


async def main() -> None:
    tests = [
        _test_many_tokens_coalesce_into_one_line,
        _test_reasoning_and_text_keep_their_own_batch,
        _test_later_event_flushes_tokens_before_itself,
        _test_non_token_events_are_untouched,
        _test_batches_are_per_session,
    ]
    failed = 0
    for test in tests:
        try:
            await test()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    if failed:
        print(f"\n{failed} prueba(s) fallaron")
        sys.exit(1)
    print("\ntodas las pruebas de coalescido de tokens pasaron")


if __name__ == "__main__":
    asyncio.run(main())
