"""``GuiConfirmation`` — ``ConfirmationService`` implementation + interactions.

Tools with the ``ask`` permission level (bash in ``safe_mode``) receive this
service via context injection. Each confirmation becomes a ``confirm.request``
notification (with the ``sessionId`` that originated it); the user answers via
``confirm.respond`` and the Future resolves.

The same cycle serves plugin interactions (the engine's ``PluginUiService``):
``SinkPluginUiService`` delegates ``ask``/``select``/``form`` to this object's
``ask_questions_plugin``/``select_plugin``/``form_plugin`` methods. Without
them, the ``questions`` tool (plugin ``phoson_plugin_questions``) degrades to
``status="unavailable"`` and the model cannot ask the user anything.

IMPORTANT (fail-closed): without a ``ConfirmationService`` the engine rejects
``ask`` tools. That is why the sidecar ALWAYS injects this implementation.
"""

from typing import Any
from collections.abc import Callable, Awaitable, Coroutine

from phoson_agent import Question, FormField, QuestionsResult

#: (session_key, kind, payload) -> awaitable resolving to the user's decision.
RequestUser = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _question_payload(question: Question) -> dict[str, Any]:
    """Serialize an engine ``Question`` to send it to the UI."""
    return {
        "id": question.id,
        "header": question.header,
        "question": question.question,
        "multi_select": bool(question.multi_select),
        "allow_other": bool(question.allow_other),
        "options": [
            {"id": option.id, "label": option.label, "description": option.description}
            for option in question.options
        ],
    }


class GuiConfirmation:
    def __init__(self, session_key: str, request_user: RequestUser) -> None:
        self._session_key = session_key
        self._request_user = request_user

    async def confirm_bash(self, command: str) -> bool:
        result = await self._request_user(
            self._session_key, "bash", {"command": command, "actions": ["yes", "no"]}
        )
        return result.get("decision") in ("yes", "always")

    async def confirm_bash_command(
        self,
        command: str,
        *,
        on_always: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> bool:
        result = await self._request_user(
            self._session_key,
            "bash",
            {"command": command, "actions": ["yes", "always", "no"]},
        )
        decision = result.get("decision")
        if decision == "always" and on_always is not None:
            await on_always(command)
        return decision in ("yes", "always")

    # -- plugin interactions (PluginUiService: select / form / ask) -----------
    #
    # All of them reuse the `confirm.request`/`confirm.respond` cycle with their
    # own `kind`; the UI dispatches on `kind` and replies with the payload each
    # method interprets here.

    async def select_plugin(
        self, title: str, message: str, choices: list[Any]
    ) -> str | None:
        """Single choice (fallback for `ask` on older hosts). Returns the chosen id."""
        result = await self._request_user(
            self._session_key,
            "select",
            {
                "title": title,
                "message": message,
                "choices": [
                    {"id": c.id, "label": c.label, "detail": c.detail} for c in choices
                ],
            },
        )
        if result.get("cancelled"):
            return None
        choice = result.get("choice")
        return str(choice) if choice else None

    async def form_plugin(
        self, title: str, fields: list[FormField]
    ) -> dict[str, str] | None:
        """Short form (text fields). Returns {fieldId: value} or None."""
        result = await self._request_user(
            self._session_key,
            "form",
            {
                "title": title,
                "fields": [
                    {
                        "id": field.id,
                        "label": field.label,
                        "kind": field.kind,
                        "required": bool(field.required),
                        "default": field.default,
                        "help": field.help,
                    }
                    for field in fields
                ],
            },
        )
        if result.get("cancelled"):
            return None
        values = result.get("values")
        return dict(values) if isinstance(values, dict) else {}

    async def ask_questions_plugin(
        self, title: str, questions: list[Question]
    ) -> QuestionsResult | None:
        """`questions` tool: 1–4 multiple-choice questions in a single card."""
        result = await self._request_user(
            self._session_key,
            "questions",
            {"title": title, "questions": [_question_payload(q) for q in questions]},
        )
        if result.get("cancelled"):
            return None
        selections: dict[str, tuple[str, ...]] = {}
        raw = result.get("selections")
        if isinstance(raw, dict):
            for question_id, option_ids in raw.items():
                if isinstance(option_ids, (list, tuple)):
                    selections[str(question_id)] = tuple(str(i) for i in option_ids)
        other_text: dict[str, str] = {}
        raw_other = result.get("other")
        if isinstance(raw_other, dict):
            for question_id, text in raw_other.items():
                if text:
                    other_text[str(question_id)] = str(text)
        return QuestionsResult(
            status="submitted", selections=selections, other_text=other_text
        )
