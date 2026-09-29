"""Classic (prompt_toolkit) implementation of ConfirmationService.

The safe-mode bash confirmation used to live inside the bash tool,
creating its own ``PromptSession``. The tool now receives a
:class:`~phoson_cli.ui_protocols.ConfirmationService` through engine
context injection; this module is the classic front end's
implementation. A full-screen front end can inject a modal-based
service instead — no tool change required.
"""

from typing import Any
from collections.abc import Callable, Sequence, Coroutine

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from phoson_agent import Choice, Question, FormField, QuestionsResult


class PromptToolkitConfirmationService:
    """Interactive confirmations via prompt_toolkit (classic REPL)."""

    async def confirm_bash(self, command: str) -> bool:
        """Ask the user (asynchronously) whether to run ``command``."""
        session: PromptSession[str] = PromptSession()
        try:
            with patch_stdout():
                answer = await session.prompt_async(
                    f"Run bash command? {command!r} [y/N]:"
                )
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}

    async def confirm_bash_command(
        self,
        command: str,
        *,
        on_always: "Callable[[str], Coroutine[Any, Any, None]] | None" = None,
    ) -> bool:
        """Classic front end: same y/N prompt (no card surface here).

        *Always* needs the full-screen card (T-6) — in the classic REPL
        the user can persist patterns with /permissions instead.
        """
        return await self.confirm_bash(command)

    async def select_plugin(
        self, title: str, message: str, choices: Sequence[Choice]
    ) -> str | None:
        """Ask for a numbered plugin choice; EOF/cancel safely returns None."""
        if not choices:
            return None
        session: PromptSession[str] = PromptSession()
        lines = [title, message]
        lines.extend(
            f"  {index}. {choice.label}"
            + (f" — {choice.detail}" if choice.detail else "")
            for index, choice in enumerate(choices, start=1)
        )
        try:
            with patch_stdout():
                answer = await session.prompt_async(
                    "\n".join(lines) + "\nSelect [Esc]: "
                )
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            selected = int(answer.strip())
        except ValueError:
            return None
        return choices[selected - 1].id if 1 <= selected <= len(choices) else None

    async def form_plugin(
        self, title: str, fields: Sequence[FormField]
    ) -> dict[str, str] | None:
        """Collect a small sequential form without exposing UI widgets to plugins."""
        session: PromptSession[str] = PromptSession()
        values: dict[str, str] = {}
        try:
            with patch_stdout():
                for index, field in enumerate(fields):
                    prefix = title if index == 0 else ""
                    default = f" [{field.default}]" if field.default is not None else ""
                    value = await session.prompt_async(
                        f"{prefix}\n{field.label}{default}: ",
                        is_password=field.kind == "password",
                    )
                    value = value.strip() or (field.default or "")
                    if field.required and not value:
                        return None
                    if field.kind == "integer" and value:
                        int(value)
                    values[field.id] = value
        except (EOFError, KeyboardInterrupt, ValueError):
            return None
        return values

    async def ask_questions_plugin(
        self, title: str, questions: Sequence[Question]
    ) -> QuestionsResult | None:
        """Ask a batch of multiple-choice questions sequentially (classic REPL).

        EOF/Ctrl+C cancels the whole batch (returns ``None``). A blank answer or
        ``s`` skips a question; ``b`` goes back to the previous one; a leading
        ``0`` selects the free-text "Other" fallback when
        :attr:`Question.allow_other` is set.
        """
        if not questions:
            return QuestionsResult(status="submitted")
        session: PromptSession[str] = PromptSession()
        selections: dict[str, tuple[str, ...]] = {}
        other_text: dict[str, str] = {}
        position = 0
        try:
            with patch_stdout():
                while 0 <= position < len(questions):
                    question = questions[position]
                    blocks: list[str] = []
                    if position == 0 and title:
                        blocks.append(title)
                    blocks.append(
                        f"[{question.header}] ({position + 1}/{len(questions)})"
                    )
                    blocks.append(question.question)
                    blocks.extend(
                        f"  {rank}. {option.label}"
                        + (f" — {option.description}" if option.description else "")
                        for rank, option in enumerate(question.options, start=1)
                    )
                    if question.allow_other:
                        blocks.append("  0. Other (write your own)")
                    hint = (
                        " (one or more, comma-separated)"
                        if question.multi_select
                        else ""
                    )
                    nav = " · s skip" + (" · b back" if position > 0 else "")
                    answer = await session.prompt_async(
                        "\n".join(blocks) + f"\nSelect{hint}{nav} [Esc to cancel]: "
                    )
                    raw = answer.strip()
                    if raw.lower() == "b" and position > 0:
                        position -= 1
                        continue
                    selections.pop(question.id, None)
                    other_text.pop(question.id, None)
                    if not raw or raw.lower() == "s":
                        position += 1
                        continue
                    chosen: list[str] = []
                    for pick in (part.strip() for part in raw.split(",")):
                        if not pick:
                            continue
                        if pick == "0" and question.allow_other:
                            text = await session.prompt_async(
                                f"{question.header} — Other: "
                            )
                            text = text.strip()
                            if text:
                                other_text[question.id] = text
                            continue
                        try:
                            number = int(pick)
                        except ValueError:
                            return None
                        if not 1 <= number <= len(question.options):
                            return None
                        chosen.append(question.options[number - 1].id)
                        if not question.multi_select:
                            break
                    if chosen:
                        selections[question.id] = tuple(chosen)
                    position += 1
        except (EOFError, KeyboardInterrupt):
            return None
        return QuestionsResult(
            status="submitted", selections=selections, other_text=other_text
        )
