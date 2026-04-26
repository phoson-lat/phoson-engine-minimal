import json

from rich.text import Text
from rich.panel import Panel
from rich.syntax import Syntax
from rich.console import Console
from rich.spinner import Spinner
from rich.markdown import Markdown

from phoson_agent import (
    AgentEvent,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)


class Renderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.session_id: str | None = None
        self._streaming_line = False
        self._spinner = Spinner("dots", text="")

    def set_session(self, session_id: str) -> None:
        self.session_id = session_id

    def flush_line(self) -> None:
        if self._streaming_line:
            self.console.print()
            self._streaming_line = False

    def on_event(self, event: AgentEvent) -> None:
        match event:
            case AgentStartEvent():
                session = self.session_id or "(none)"
                self.console.print(
                    Panel(
                        f"Model: [bold]{event.model}[/bold]\n"
                        f"Session: [bold]{session}[/bold]\n"
                        "Messages: "
                        f"{event.message_count}"
                        f" | Max iterations: {event.max_iterations}",
                        title="phoson_cli",
                    )
                )

            case AgentTokenEvent():
                self.console.print(event.content, end="", soft_wrap=True)
                self._streaming_line = True

            case AgentReasoningEvent():
                self.console.print(
                    Text(event.content, style="dim italic"),
                    end="",
                    soft_wrap=True,
                )
                self._streaming_line = True

            case AgentToolStartEvent():
                self.flush_line()
                args_json = json.dumps(event.args, indent=2, ensure_ascii=True)
                syntax = Syntax(args_json, "json", theme="monokai", word_wrap=True)
                self.console.print(
                    Panel(
                        syntax,
                        title=f"Tool start: {event.tool_name}",
                        subtitle=f"id={event.tool_call_id}",
                    )
                )

            case AgentToolDoneEvent():
                self.flush_line()
                color = "red" if event.error else "green"
                status = "error" if event.error else "ok"
                self.console.print(
                    f"[{color}]✓ {event.tool_name} "
                    f"({event.duration_ms}ms) [{status}][/{color}]"
                )

            case AgentStepDoneEvent():
                return

            case AgentDoneEvent():
                self.flush_line()
                if event.result.total_cost_usd > 0:
                    self.console.print(
                        Text(
                            (
                                f"Cost: ${event.result.total_cost_usd:.6f} | "
                                f"Credits: {event.result.total_credits:.6f}"
                            ),
                            style="dim",
                        )
                    )

            case AgentErrorEvent():
                self.flush_line()
                detail = (
                    f"{event.message}\ncode={event.code} retryable={event.retryable}"
                    if event.code
                    else event.message
                )
                self.console.print(Panel(detail, title="Agent Error", style="red"))

            case _:
                self.console.print(
                    Markdown(f"Unhandled event: `{type(event).__name__}`")
                )
