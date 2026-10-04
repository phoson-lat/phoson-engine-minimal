"""Two named CLI sessions talking through the peers plugin (end to end).

Two real ``SessionController`` instances (``--name frontend-agent`` and
``--name backend-agent``) share a temp peers dir. The frontend's model calls
``peer_ask``; the backend's *wake loop* must pick the request up as an
autonomous turn (rendered in its own sink) and its final answer must come
back as the tool result — with no tool call on the backend side.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_agent.models import AgentDoneEvent, AgentRunResult, AgentStartEvent
from phoson_cli.controller import SessionController


class _Sink:
    def __init__(self) -> None:
        self.user_messages: list[str] = []
        self.notifications: list[tuple[str, str]] = []

    def on_user_message(self, text, message) -> None:
        self.user_messages.append(text)

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _done(answer: str) -> AgentDoneEvent:
    return AgentDoneEvent(
        result=AgentRunResult(
            final_content=answer,
            history=[
                Message(role="user", content="q"),
                Message(role="assistant", content=answer),
            ],
            input_messages=[],
            steps=[],
        )
    )


def _controller(tmp_path, name: str) -> tuple[SessionController, _Sink]:
    sink = _Sink()
    config = PhosonConfig(
        provider="ollama",
        model="test-model",
        sessions_dir=tmp_path / f"sessions-{name}",
        peer_name=name,
        peers_data_dir=tmp_path / "peers",
        llm_titles=False,
    )
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)
    controller._wake_poll_seconds = 0.02
    return controller, sink


def _peers(controller: SessionController):
    return next(
        p
        for p in controller.engine._loaded_plugins
        if type(p).__name__ == "PeersPlugin"
    )


async def test_frontend_asks_backend_and_gets_turn_answer(
    tmp_path, monkeypatch
) -> None:
    import phoson_plugin_peers._plugin as peers_mod

    monkeypatch.setattr(peers_mod, "_ASK_POLL_SECONDS", 0.01)
    front, front_sink = _controller(tmp_path, "frontend-agent")
    back, back_sink = _controller(tmp_path, "backend-agent")

    backend_prompts: list[str] = []

    async def backend_stream(path, config):
        content = path[-1].content
        backend_prompts.append(
            content
            if isinstance(content, str)
            else "".join(getattr(b, "text", "") for b in content)
        )
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done("API: GET /users, POST /users")

    back.engine.stream = backend_stream
    back.start_monitor_wake_loop()

    tool_results: list[str] = []

    async def frontend_stream(path, config):
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        # What the model would do: call peer_ask, then answer with it.
        peer_ask = next(t for t in front.engine.tools if t.name == "peer_ask")
        result = await peer_ask.handler(
            {"to": "backend-agent", "content": "Pásame la doc de la API"},
            front.engine.context,
        )
        tool_results.append(result)
        yield _done(f"Got it: {result}")

    front.engine.stream = frontend_stream
    try:
        outcome = await asyncio.wait_for(
            front.run_turn("dile al backend-agent que te pase la doc de la API"),
            10,
        )
        assert outcome.status == "done"
        assert tool_results == [
            "Reply from backend-agent:\nAPI: GET /users, POST /users"
        ]
        # The backend window rendered the incoming message as its own turn.
        assert len(back_sink.user_messages) == 1
        shown = back_sink.user_messages[0]
        assert shown.startswith("[PEER MESSAGES]")
        assert ">>> request from frontend-agent" in shown
        assert "Pásame la doc de la API" in shown
        assert backend_prompts and "[PEER MESSAGES]" in backend_prompts[0]
        assert any(
            "Reply sent to frontend-agent" in m for _, m in back_sink.notifications
        )
        # The reply was consumed by peer_ask: the frontend has no stray wake.
        assert _peers(front).pending_wakes(None) == []
    finally:
        await back.shutdown()
        await front.shutdown()


async def test_header_shows_agent_name(tmp_path) -> None:
    ctrl, _sink = _controller(tmp_path, "solo-agent")
    try:
        assert ctrl.monitor_status() == "👥 solo-agent"
    finally:
        await ctrl.shutdown()


async def test_name_collision_disables_peers_with_warning(tmp_path) -> None:
    import os
    import json
    import warnings

    from phoson_plugin_peers.storage import PeerStore

    store = PeerStore(tmp_path / "peers", "default")
    store.claim("taken", instance="other")
    path = store.root / "agents" / "taken.json"
    raw = json.loads(path.read_text())
    raw["pid"] = 1 if os.getpid() != 1 else 2
    path.write_text(json.dumps(raw))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ctrl, _sink = _controller(tmp_path, "taken")
    try:
        assert any("already in use" in str(w.message) for w in caught)
        assert not any(t.name == "peer_ask" for t in ctrl.engine.tools)
    finally:
        await ctrl.shutdown()
