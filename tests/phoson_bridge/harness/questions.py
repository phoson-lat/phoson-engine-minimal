"""Tests for plugin interactions (`questions`/`select`/`form`).

Run them with ``uv run python tests/phoson_bridge/harness/questions.py``
(``test_harnesses.py`` runs every harness under pytest).

No pytest: asserts and a `main()` that prints what it checks. Covers the
contract between `GuiConfirmation` and the engine's `SinkPluginUiService`, which
feeds the `questions` tool of the `phoson_plugin_questions` plugin:

1. `ask_questions_plugin` emits `confirm.request` with `kind="questions"` and
   the serialized questions (id, header, options, multi_select, allow_other).
2. The UI's response (`selections`/`other`) is translated to `QuestionsResult`.
3. `cancelled` resolves to `None` (the engine turns it into
   `status="cancelled"`).
4. `select_plugin`/`form_plugin` (fallback) return the chosen id and the form
   values.
"""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoson_agent import Choice, Question, FormField, QuestionOption  # noqa: E402
from phoson_bridge.confirmation import GuiConfirmation  # noqa: E402

QUESTION = Question(
    id="q1",
    header="Auth",
    question="¿Qué método de autenticación prefieres?",
    options=(
        QuestionOption(id="a", label="Token", description="Simple"),
        QuestionOption(id="b", label="OAuth", description="Por delegación"),
    ),
    multi_select=True,
    allow_other=True,
)


def _responder(response: dict):
    """Fake `request_user`: records the request and returns `response`."""

    seen: dict = {}

    async def request_user(_key: str, kind: str, payload: dict) -> dict:
        seen.clear()
        seen.update(kind=kind, payload=payload)
        return response

    return request_user, seen


async def _run() -> None:
    # 1) The request travels with kind "questions" and the serialized questions.
    request_user, seen = _responder({"selections": {"q1": ["a", "b"]}, "other": {}})
    result = await GuiConfirmation("k", request_user).ask_questions_plugin(
        "Preferencias", [QUESTION]
    )
    assert seen["kind"] == "questions", seen
    sent = seen["payload"]["questions"][0]
    assert sent["id"] == "q1" and sent["header"] == "Auth", sent
    assert sent["multi_select"] is True and sent["allow_other"] is True, sent
    assert [o["label"] for o in sent["options"]] == ["Token", "OAuth"], sent
    print("ok: ask serializa la pregunta con kind=questions")

    # 2) The UI's response is translated to QuestionsResult.
    assert result is not None and result.status == "submitted", result
    assert dict(result.selections) == {"q1": ("a", "b")}, result.selections
    assert dict(result.other_text) == {}, result.other_text
    print("ok: selections -> QuestionsResult(selections=...)")

    request_user, _seen = _responder(
        {"selections": {}, "other": {"q1": "mi respuesta"}}
    )
    result = await GuiConfirmation("k", request_user).ask_questions_plugin(
        "T", [QUESTION]
    )
    assert result is not None and dict(result.other_text) == {"q1": "mi respuesta"}, (
        result
    )
    print("ok: other -> QuestionsResult(other_text=...)")

    # 3) Cancelling resolves to None (the engine sees it as status="cancelled").
    request_user, _seen = _responder({"cancelled": True})
    gui = GuiConfirmation("k", request_user)
    assert await gui.ask_questions_plugin("T", [QUESTION]) is None
    assert await gui.select_plugin("T", "M", [Choice(id="a", label="A")]) is None
    assert await gui.form_plugin("T", [FormField(id="x", label="X")]) is None
    print("ok: cancelled -> None en ask/select/form")

    # 4) Select/form fallback.
    request_user, seen = _responder({"choice": "b"})
    chosen = await GuiConfirmation("k", request_user).select_plugin(
        "T", "M", [Choice(id="a", label="A"), Choice(id="b", label="B")]
    )
    assert chosen == "b", chosen
    assert seen["kind"] == "select", seen
    assert seen["payload"]["choices"][1] == {"id": "b", "label": "B", "detail": None}, (
        seen
    )
    print("ok: select_plugin devuelve el id elegido")

    request_user, seen = _responder({"values": {"other": "hola"}})
    values = await GuiConfirmation("k", request_user).form_plugin(
        "T", [FormField(id="other", label="Tu respuesta")]
    )
    assert values == {"other": "hola"}, values
    assert seen["kind"] == "form", seen
    assert seen["payload"]["fields"][0]["id"] == "other", seen
    print("ok: form_plugin devuelve los valores")

    # 5) The real glue: the engine's `SinkPluginUiService` delegates `ask` to
    #    `GuiConfirmation.ask_questions_plugin` (it used to return "unavailable").
    from phoson_cli.plugin_ui import SinkPluginUiService

    class _FakeSink:
        def notify(self, kind: str, message: str) -> None:  # pragma: no cover
            return None

    request_user, _seen = _responder({"selections": {"q1": ["a"]}, "other": {}})
    ui = SinkPluginUiService(
        _FakeSink(), None, confirmation=GuiConfirmation("k", request_user)
    )
    result = await ui.ask(title="Preferencias", questions=[QUESTION])
    assert result.status == "submitted" and dict(result.selections) == {"q1": ("a",)}, (
        result
    )
    print("ok: SinkPluginUiService.ask delega en GuiConfirmation")

    ui_sin_preguntas = SinkPluginUiService(_FakeSink(), None, confirmation=object())
    result = await ui_sin_preguntas.ask(title="T", questions=[QUESTION])
    assert result.status == "unavailable", result
    print("ok: sin ask_questions_plugin degrada a unavailable (fail-safe)")


def main() -> None:
    asyncio.run(_run())
    print("TODO OK")


if __name__ == "__main__":
    main()
