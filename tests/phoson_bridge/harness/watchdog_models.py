"""Tests for the handler watchdog and the `models.list` cache.

Run them with ``uv run python tests/phoson_bridge/harness/watchdog_models.py``
(``test_harnesses.py`` runs every harness under pytest).

No pytest: asserts and a `main()` that prints what it checks. Covers the two
behaviors the UI notices:

1. A handler that **shows no sign of life** triggers the `notify`/`warn` notice
   (the real case: a frozen engine, with the UI stuck on "Thinking…" forever).
2. A live handler (even a slow one) does NOT trigger the notice.
3. `models.list` caches the catalog per provider and does not hit the network
   again; a cached response is marked with `cached: true`; a network failure is
   not cached.
"""

import io
import os
import sys
import time
import asyncio
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phoson_bridge.server as server  # noqa: E402


class _RecordingStream(io.StringIO):
    """The bridge's `protocol_out`: keeps what was written and does nothing on flush."""

    def flush(self) -> None:  # the writer flushes per line
        return None


class _FakeRepl:
    """The bare minimum `_models_list` reads from the session."""

    current_model = "demo/model"
    config = type("Cfg", (), {"provider": "openrouter", "base_url": ""})()


def _patch_sessions(harness: "_Bridge") -> None:
    """Stand-in for the session layer for `_models_list`.

    `models.list` NO LONGER builds the engine (it must work during onboarding,
    when `build_chat` could not even start the client): it reads the live config
    with `config_for` and the repl only if it already exists.
    """
    harness.bridge.sessions.config_for = lambda _key: _FakeRepl().config  # type: ignore[method-assign]
    harness.bridge.sessions.repl_if_built = lambda _key: _FakeRepl()  # type: ignore[method-assign]


class _Bridge:
    """A `Bridge` with its writer running.

    Without the `_writer` task, `_emit` only enqueues and the protocol stream
    stays empty (exactly what the real sidecar does when `serve()` starts).
    """

    def __init__(self) -> None:
        self.out = _RecordingStream()
        self.bridge = server.Bridge(self.out)
        # The warm-up would build a real `PhosonRepl`: tests do not need it and
        # it would only add seconds of startup.
        self.bridge._warm_task.cancel()
        self.writer = asyncio.create_task(self.bridge._writer())

    def log(self) -> str:
        return self.out.getvalue()

    async def close(self) -> None:
        self.writer.cancel()
        await asyncio.sleep(0)


async def _test_watchdog_fires_when_silent() -> None:
    harness = _Bridge()

    async def hang(_params):
        await asyncio.sleep(30)

    harness.bridge._methods["test.hang"] = hang
    task = asyncio.create_task(
        harness.bridge._dispatch(
            {"id": 1, "method": "test.hang", "params": {"sessionId": "s1"}}
        )
    )
    await asyncio.sleep(1.2)
    task.cancel()
    await asyncio.sleep(0)
    await harness.close()

    log = harness.log()
    assert "sin enviar nada" in log, f"el watchdog no avisó:\n{log}"
    assert '"kind":"warn"' in log, f"el aviso no es un warn:\n{log}"
    print("  ok  watchdog avisa cuando el handler no da señales de vida")


async def _test_watchdog_quiet_when_alive() -> None:
    harness = _Bridge()

    async def alive(params):
        for _ in range(12):
            harness.bridge._emit(
                "notify",
                {"sessionId": params.get("sessionId"), "kind": "info", "message": "…"},
            )
            await asyncio.sleep(0.1)
        return {}

    harness.bridge._methods["test.alive"] = alive
    task = asyncio.create_task(
        harness.bridge._dispatch(
            {"id": 2, "method": "test.alive", "params": {"sessionId": "s2"}}
        )
    )
    await asyncio.sleep(1.2)
    task.cancel()
    await asyncio.sleep(0)
    await harness.close()

    log = harness.log()
    assert "sin enviar nada" not in log, f"el watchdog aviso con el motor vivo:\n{log}"
    print("  ok  el watchdog calla cuando hay actividad (modelo lento != colgado)")


async def _test_watchdog_fires_while_the_loop_is_frozen() -> None:
    """The REAL case: the handler **freezes the event loop** (like the hung `git`).

    Neither asyncio tasks nor the loop's writer can run in that interval, so the
    notice has to go out from the watchdog thread. It is checked by reading the
    stream *during* the freeze (captured from another thread, at 0.9 s): if the
    notice were queued, it would not appear until the loop was released.
    """
    harness = _Bridge()
    snapshot: dict[str, str] = {}

    def capture() -> None:
        snapshot["log"] = harness.log()

    async def frozen(params):
        # Exact replica of the real bug: the engine emits (`session.user_message`)
        # and THEN freezes in a synchronous call. That emission must not count as
        # a sign of life: it is queued, but nobody drains it with the loop stopped.
        harness.bridge._emit(
            "notify",
            {"sessionId": params.get("sessionId"), "kind": "info", "message": "hola"},
        )
        time.sleep(1.2)  # synchronous: freezes the event loop on purpose
        return {}

    harness.bridge._methods["test.frozen"] = frozen
    capturer = threading.Timer(0.9, capture)
    capturer.start()
    await harness.bridge._dispatch(
        {"id": 3, "method": "test.frozen", "params": {"sessionId": "s6"}}
    )
    capturer.join()
    await harness.close()

    got = snapshot.get("log", "")
    assert "sin enviar nada" in got, (
        f"el aviso no salió con el event loop congelado:\n{got or '<vacío>'}"
    )
    # The writer was not running during the freeze (the handler's emission was
    # still queued): so the notice could not have gone out through the loop's
    # queue.
    assert "hola" not in got, f"el writer drenó durante la congelación:\n{got}"
    print("  ok  el aviso sale aunque el event loop este congelado (hilo aparte)")


async def _test_watchdog_warns_after_a_long_silence() -> None:
    """It wrote and then went silent (a hung provider call with no timeout): warn
    when the silence exceeds `PHOSON_WATCHDOG_SILENCE_SECONDS`."""
    harness = _Bridge()

    async def quiet(params):
        harness.bridge._emit(
            "notify",
            {"sessionId": params.get("sessionId"), "kind": "info", "message": "hola"},
        )
        await asyncio.sleep(1.6)  # the loop is still alive, but no more output
        return {}

    harness.bridge._methods["test.quiet"] = quiet
    await harness.bridge._dispatch(
        {"id": 4, "method": "test.quiet", "params": {"sessionId": "s7"}}
    )
    await harness.close()

    log = harness.log()
    assert "hola" in log, f"la primera salida no se escribió:\n{log}"
    assert "sin enviar nada" in log, f"no avisó tras el silencio prolongado:\n{log}"
    print("  ok  avisa tras un silencio prolongado (p. ej. red colgada)")


async def _test_models_list_is_cached() -> None:
    harness = _Bridge()
    calls = 0

    async def fake_fetch(_cfg):
        nonlocal calls
        calls += 1
        return [{"id": "m1"}, {"id": "m2"}], None

    _patch_sessions(harness)
    harness.bridge._fetch_models = fake_fetch  # type: ignore[method-assign]

    first = await harness.bridge._models_list({"sessionId": "s3"})
    second = await harness.bridge._models_list({"sessionId": "s3"})
    await harness.close()

    assert calls == 1, f"la red se consultó {calls} veces (esperaba 1)"
    assert first["models"] == [{"id": "m1"}, {"id": "m2"}]
    assert "cached" not in first
    assert second.get("cached") is True, second
    assert second["models"] == first["models"]
    print("  ok  models.list cachea el catálogo (1 sola consulta de red)")


async def _test_models_error_is_not_cached() -> None:
    harness = _Bridge()
    calls = 0

    async def failing_fetch(_cfg):
        nonlocal calls
        calls += 1
        return [], "sin red"

    _patch_sessions(harness)
    harness.bridge._fetch_models = failing_fetch  # type: ignore[method-assign]

    first = await harness.bridge._models_list({"sessionId": "s4"})
    second = await harness.bridge._models_list({"sessionId": "s4"})
    await harness.close()

    assert first.get("error") == "sin red"
    assert calls == 2, f"un fallo de red se cacheó ({calls} llamadas)"
    assert second.get("error") == "sin red"
    print("  ok  un fallo de red no se cachea (reintenta en la siguiente llamada)")


async def _test_models_cache_invalidated_on_provider_change() -> None:
    harness = _Bridge()
    calls = 0

    async def fake_fetch(_cfg):
        nonlocal calls
        calls += 1
        return [{"id": "m1"}], None

    _patch_sessions(harness)
    harness.bridge._fetch_models = fake_fetch  # type: ignore[method-assign]

    await harness.bridge._models_list({"sessionId": "s5"})
    harness.bridge._invalidate_models()
    await harness.bridge._models_list({"sessionId": "s5"})
    await harness.close()

    assert calls == 2, f"cambiar de proveedor no invalidó la caché ({calls} llamadas)"
    print("  ok  cambiar de proveedor/base_url invalida la caché del catálogo")


async def _amain() -> None:
    await _test_watchdog_fires_when_silent()
    await _test_watchdog_quiet_when_alive()
    await _test_watchdog_fires_while_the_loop_is_frozen()
    await _test_watchdog_warns_after_a_long_silence()
    await _test_models_list_is_cached()
    await _test_models_error_is_not_cached()
    await _test_models_cache_invalidated_on_provider_change()


def main() -> int:
    # Windows console (cp1252): reconfigured so accented characters can be printed.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — if it fails, carry on anyway
            pass
    print("bridge: watchdog + cache de models.list")
    # Short thresholds for the test (the real ones are 15 s / 60 s; they can also
    # be forced with PHOSON_WATCHDOG_SECONDS / PHOSON_WATCHDOG_SILENCE_SECONDS).
    os.environ["PHOSON_WATCHDOG_SECONDS"] = "0.4"
    os.environ["PHOSON_WATCHDOG_SILENCE_SECONDS"] = "0.7"
    asyncio.run(_amain())
    print("todas las comprobaciones OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
