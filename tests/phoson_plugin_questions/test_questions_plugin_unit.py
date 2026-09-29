"""Unit tests for the bundled Questions plugin and its ``questions`` tool."""

import pytest

from phoson_agent import (
    Choice,
    Question,
    FormField,
    QuestionOption,
    QuestionsResult,
    InteractionResult,
)
from phoson_cli.config import PhosonConfig
from phoson_plugin_questions import (
    MAX_QUESTIONS,
    QuestionsPlugin,
    create_plugin,
    build_questions_tool,
)
from phoson_cli.session_utils import build_questions_plugins


class _Ctx:
    def __init__(self, ui: object) -> None:
        self.extra = {"plugin_ui": ui}


class _NativeUi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Question]]] = []

    async def ask(self, *, title: str, questions: list[Question]) -> QuestionsResult:
        self.calls.append((title, list(questions)))
        return QuestionsResult(
            status="submitted",
            selections={questions[0].id: (questions[0].options[0].id,)},
            other_text={questions[1].id: "custom"} if len(questions) > 1 else {},
        )


class _FallbackUi:
    def __init__(self, picks: list[str | None]) -> None:
        self._picks = list(picks)
        self.forms: list[tuple[str, list[FormField]]] = []

    async def select(self, *, title, message, choices) -> InteractionResult:
        pick = self._picks.pop(0) if self._picks else None
        return InteractionResult(
            status="submitted" if pick is not None else "cancelled",
            values={"choice": pick} if pick is not None else {},
        )

    async def form(self, *, title, fields) -> InteractionResult:
        self.forms.append((title, list(fields)))
        return InteractionResult(status="submitted", values={"other": "typed"})


class _NoUi:
    pass


def _payload() -> dict:
    return {
        "questions": [
            {
                "question": "Which database?",
                "header": "DB",
                "options": [
                    {"label": "Postgres", "description": "sql"},
                    {"label": "SQLite", "description": "file"},
                ],
            },
            {
                "question": "Which features?",
                "header": "Feat",
                "multiSelect": True,
                "options": [
                    {"label": "Auth", "description": "a"},
                    {"label": "Cache", "description": "c"},
                ],
            },
        ]
    }


def test_tool_schema_matches_the_ask_user_question_shape() -> None:
    tool = create_plugin().get_tools()[0]
    assert tool.name == "questions"
    schema = tool.parameters
    assert schema["required"] == ["questions"]
    items = schema["properties"]["questions"]
    assert items["minItems"] == 1 and items["maxItems"] == MAX_QUESTIONS
    option_schema = items["items"]["properties"]["options"]
    assert option_schema["minItems"] == 2 and option_schema["maxItems"] == 4


@pytest.mark.asyncio
async def test_native_ask_path_returns_formatted_answers() -> None:
    tool = build_questions_tool(QuestionsPlugin())
    ui = _NativeUi()

    answer = await tool.handler(_payload(), _Ctx(ui))

    assert ui.calls and ui.calls[0][0] == "Questions"
    assert answer == "DB: Postgres\nFeat: Other: custom"


@pytest.mark.asyncio
async def test_fallback_path_uses_select_and_other_form() -> None:
    tool = build_questions_tool(QuestionsPlugin())
    ui = _FallbackUi(picks=["Postgres", "__other__"])

    answer = await tool.handler(_payload(), _Ctx(ui))

    assert "DB: Postgres" in answer
    assert "Feat: Other: typed" in answer
    assert len(ui.forms) == 1 and ui.forms[0][1][0].id == "other"


@pytest.mark.asyncio
async def test_missing_ui_and_invalid_payload_are_reported() -> None:
    tool = build_questions_tool(QuestionsPlugin())
    assert "unavailable" in await tool.handler(_payload(), _Ctx(_NoUi()))
    assert "unavailable" in await tool.handler(_payload(), None)
    assert "Invalid" in await tool.handler({"questions": []}, _Ctx(_NativeUi()))
    bad = {
        "questions": [
            {
                "question": "q",
                "header": "h",
                "options": [{"label": str(i), "description": "d"} for i in range(5)],
            }
        ]
    }
    assert "between 2 and 4" in await tool.handler(bad, _Ctx(_NativeUi()))


@pytest.mark.asyncio
async def test_cancelled_batch_is_reported() -> None:
    class _Cancelling:
        async def ask(self, *, title, questions):
            return QuestionsResult(status="cancelled")

    tool = build_questions_tool(QuestionsPlugin())
    assert "dismissed" in await tool.handler(_payload(), _Ctx(_Cancelling()))


def test_configure_sets_title_and_caps_max_questions() -> None:
    plugin = QuestionsPlugin()
    plugin.configure({"title": "  Setup  ", "max_questions": 99})
    assert plugin.title == "Setup"
    assert plugin.max_questions == MAX_QUESTIONS


def test_build_questions_plugins_is_opt_in() -> None:
    disabled = PhosonConfig(provider="ollama", model="m")
    assert build_questions_plugins(disabled) == []

    enabled = PhosonConfig(
        provider="ollama", model="m", enable_questions=True, questions_title="Ask"
    )
    specs = build_questions_plugins(enabled)
    assert len(specs) == 1
    assert isinstance(specs[0], QuestionsPlugin)
    assert specs[0].title == "Ask"


def test_question_option_defaults() -> None:
    option = QuestionOption(id="a", label="A")
    assert option.description is None
    choice = Choice(id="a", label="A")
    assert choice.detail is None
