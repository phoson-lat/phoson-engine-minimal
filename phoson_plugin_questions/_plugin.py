"""The Questions plugin: an AskUserQuestion-style interactive tool.

A bundled :class:`~phoson_agent.plugin.Plugin` that exposes a single ``questions``
tool. The agent uses it to ask the user one to four multiple-choice questions in
one interaction, mirroring Claude Code's ``AskUserQuestion``: each question has a
short ``header``, a ``question`` body, two to four ``options`` (label +
description), an optional ``multiSelect`` flag, and a free-text "Other" fallback.

The tool is host-agnostic. It prefers the neutral
:meth:`phoson_agent.cli_extensions.PluginUiService.ask` primitive when the host
provides it (one card, native multi-select), and otherwise degrades to composing
``select``/``form`` calls, so it works on older hosts too. In a non-interactive
host (one-shot/CI) both paths return ``unavailable`` and the tool tells the model
so, never reading stdin.
"""

import logging
from typing import Any

from phoson_agent import Choice, FormField
from phoson_agent.models import AgentTool
from phoson_agent.plugin import Plugin
from phoson_agent.cli_extensions import Question, QuestionOption, QuestionsResult

logger = logging.getLogger(__name__)

#: Claude Code's limits: at most four questions, two to four options each.
MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4

#: Sentinel option id used by the ``select``/``form`` fallback for "Other".
OTHER_ID = "__other__"

_QUESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_QUESTIONS,
            "description": (
                "One to four questions to ask the user in a single interaction."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The complete question to ask, ending with a question "
                            "mark. Clear, specific, and self-contained."
                        ),
                    },
                    "header": {
                        "type": "string",
                        "description": (
                            "Very short label for the question (max ~12 characters), "
                            "e.g. 'Auth method' or 'Library'."
                        ),
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true the user may pick more than one option; "
                            "otherwise they pick exactly one."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "minItems": MIN_OPTIONS,
                        "maxItems": MAX_OPTIONS,
                        "description": "Two to four mutually distinct choices.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": (
                                        "Concise option text (1–5 words) shown to "
                                        "the user."
                                    ),
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Explanation of what this option means or "
                                        "its trade-offs."
                                    ),
                                },
                            },
                            "required": ["label", "description"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["question", "header", "options"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_TOOL_DESCRIPTION = (
    "Ask the user one to four multiple-choice questions in a single interaction "
    "and get their answers. Use this to clarify requirements, confirm a decision "
    "among alternatives, or gather preferences before proceeding. Each question "
    "has a short header, two to four labelled options (each with a description), "
    "and an optional multiSelect flag; the user may also type a free-form 'Other' "
    "answer. Do not use it for trivial confirmations. In a non-interactive host "
    "the UI is unavailable and the tool reports so instead of blocking."
)


def _get_plugin_ui(context: Any) -> Any:
    if context is None:
        return None
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        return extra.get("plugin_ui")
    return None


def _parse_questions(raw: Any) -> tuple[list[Question], str | None]:
    """Validate the model payload and build neutral :class:`Question` objects.

    Returns ``(questions, error)``; on success ``error`` is ``None``.
    """
    if not isinstance(raw, list) or not raw:
        return [], "questions must be a non-empty array."
    if len(raw) > MAX_QUESTIONS:
        return [], f"at most {MAX_QUESTIONS} questions are allowed."

    questions: list[Question] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"question {index + 1} must be an object."
        question = str(item.get("question") or "").strip()
        header = str(item.get("header") or "").strip()
        options_raw = item.get("options")
        if not question:
            return [], f"question {index + 1} is missing 'question'."
        if not header:
            return [], f"question {index + 1} is missing 'header'."
        if not isinstance(options_raw, list) or not (
            MIN_OPTIONS <= len(options_raw) <= MAX_OPTIONS
        ):
            return [], (
                f"question {index + 1} must have between {MIN_OPTIONS} and "
                f"{MAX_OPTIONS} options."
            )
        options: list[QuestionOption] = []
        seen: set[str] = set()
        for position, option in enumerate(options_raw):
            if not isinstance(option, dict):
                return [], f"option {position + 1} of question {index + 1} invalid."
            label = str(option.get("label") or "").strip()
            if not label:
                return [], (
                    f"option {position + 1} of question {index + 1} has no label."
                )
            option_id = str(option.get("id") or label)
            if option_id in seen:
                option_id = f"{option_id}-{position + 1}"
            seen.add(option_id)
            description = option.get("description")
            options.append(
                QuestionOption(
                    id=option_id,
                    label=label,
                    description=str(description).strip() if description else None,
                )
            )
        questions.append(
            Question(
                id=str(item.get("id") or f"q{index + 1}"),
                header=header,
                question=question,
                options=tuple(options),
                multi_select=bool(item.get("multiSelect", False)),
                allow_other=bool(item.get("allowOther", True)),
            )
        )
    return questions, None


async def _ask_via_primitive(
    plugin_ui: Any, title: str, questions: list[Question]
) -> QuestionsResult:
    """Use the native ``ask`` primitive when the host provides it."""
    return await plugin_ui.ask(title=title, questions=questions)


async def _ask_via_fallback(
    plugin_ui: Any, title: str, questions: list[Question]
) -> QuestionsResult:
    """Degrade to ``select``/``form`` for hosts without the ``ask`` primitive.

    Multi-select is reduced to a single choice here (a documented limitation of
    older hosts); "Other" is offered as an extra choice backed by a form field.
    """
    selections: dict[str, tuple[str, ...]] = {}
    other_text: dict[str, str] = {}
    for index, question in enumerate(questions):
        choices = [
            Choice(id=o.id, label=o.label, detail=o.description)
            for o in question.options
        ]
        if question.allow_other:
            choices.append(Choice(id=OTHER_ID, label="Other", detail="Type your own"))
        select = getattr(plugin_ui, "select", None)
        if select is None:
            return QuestionsResult(status="unavailable")
        result = await select(
            title=title if index == 0 else question.header,
            message=question.question,
            choices=choices,
        )
        if result.status == "unavailable":
            return QuestionsResult(status="unavailable")
        if result.status == "cancelled":
            continue
        chosen = result.values.get("choice")
        if chosen == OTHER_ID:
            form = getattr(plugin_ui, "form", None)
            if form is not None:
                details = await form(
                    title=question.header,
                    fields=(FormField(id="other", label="Your answer"),),
                )
                if details.status == "submitted" and details.values.get("other"):
                    other_text[question.id] = details.values["other"]
            continue
        if chosen is not None:
            selections[question.id] = (chosen,)
    return QuestionsResult(
        status="submitted", selections=selections, other_text=other_text
    )


def _format_result(questions: list[Question], result: QuestionsResult) -> str:
    lines: list[str] = []
    for question in questions:
        labels: list[str] = []
        for option_id in result.selections.get(question.id, ()):
            match = next(
                (o.label for o in question.options if o.id == option_id), option_id
            )
            labels.append(match)
        other = result.other_text.get(question.id, "")
        if other:
            labels.append(f"Other: {other}")
        answer = ", ".join(labels) if labels else "(no answer)"
        lines.append(f"{question.header}: {answer}")
    return "\n".join(lines)


def build_questions_tool(plugin: "QuestionsPlugin") -> AgentTool:
    """Build the ``questions`` :class:`AgentTool` with an explicit JSON schema."""

    async def handler(args: dict[str, Any], context: Any = None) -> str:
        questions, error = _parse_questions(args.get("questions"))
        if error is not None:
            return f"Invalid questions payload: {error}"

        plugin_ui = _get_plugin_ui(context)
        if plugin_ui is None:
            return (
                "The questions UI is unavailable in this host (non-interactive). "
                "Proceed with a reasonable assumption or ask in plain text."
            )

        title = plugin.title
        if getattr(plugin_ui, "ask", None) is not None:
            result = await _ask_via_primitive(plugin_ui, title, questions)
        else:
            result = await _ask_via_fallback(plugin_ui, title, questions)

        if result.status == "unavailable":
            return (
                "The questions UI is unavailable in this host (non-interactive). "
                "Proceed with a reasonable assumption or ask in plain text."
            )
        if result.status == "cancelled":
            return "The user dismissed the questions without answering."
        return _format_result(questions, result)

    return AgentTool(
        name="questions",
        description=_TOOL_DESCRIPTION,
        parameters=_QUESTIONS_SCHEMA,
        handler=handler,
        metadata={"plugin": "phoson-plugin-questions"},
    )


class QuestionsPlugin(Plugin):
    """Expose the ``questions`` tool for interactive multiple-choice prompts."""

    def __init__(self) -> None:
        self._title = "Questions"
        self._max_questions = MAX_QUESTIONS

    @property
    def name(self) -> str:
        return "phoson-plugin-questions"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return (
            "Ask the user one to four multiple-choice questions in one "
            "interaction (an AskUserQuestion-style tool)."
        )

    @property
    def title(self) -> str:
        """Card title shown by interactive hosts."""
        return self._title

    @property
    def max_questions(self) -> int:
        return self._max_questions

    def configure(self, config: dict[str, Any]) -> None:
        title = config.get("title")
        if isinstance(title, str) and title.strip():
            self._title = title.strip()
        max_questions = config.get("max_questions")
        if isinstance(max_questions, int) and max_questions > 0:
            self._max_questions = min(max_questions, MAX_QUESTIONS)

    def get_tools(self) -> list[AgentTool]:
        return [build_questions_tool(self)]

    def get_tool_render_specs(self):  # noqa: ANN201 - optional host hook
        from phoson_agent import ToolRenderSpec

        return [
            ToolRenderSpec(
                tool_name="questions",
                verb="asking the user",
                icon="?",
            )
        ]


def create_plugin() -> QuestionsPlugin:
    """Factory used by entry points and path-based loading."""
    return QuestionsPlugin()


__all__ = [
    "Question",
    "QuestionOption",
    "QuestionsResult",
    "QuestionsPlugin",
    "build_questions_tool",
    "create_plugin",
    "MAX_QUESTIONS",
    "MIN_OPTIONS",
    "MAX_OPTIONS",
]
