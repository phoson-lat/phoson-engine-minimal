"""``phoson-cli --self-test``: verify a build can do real work, offline.

``--version``/``--help`` never touch most of the program, so a bundle that is
missing a dynamically imported module or data file (PyInstaller cannot see
those) passes them and then fails on the first conversation. The 0.57/0.58
binaries shipped exactly that way: tiktoken's ``tiktoken_ext`` plugin was not
bundled and every token count raised "Unknown encoding cl100k_base".

This runs, without network or credentials:

* one complete agent turn (tool call + answer) against an in-process fake
  model, which exercises the engine loop, tool dispatch and token counting;
* every tiktoken encoding the CLI uses;
* an import of every provider adapter registered in ``build_chat``;
* an import and manifest check of every official plugin;
* the bundled assets.

The release workflow runs it on every built bundle before publishing.
"""

import sys
import asyncio
import importlib
from collections.abc import Callable

#: Optional extras whose provider adapters may legitimately be absent from a
#: source install; a frozen bundle is built with every extra and must have them.
_OPTIONAL_PROVIDERS = {"gemini", "google", "mistral", "bedrock", "aws"}
#: Every official in-tree plugin (tests/phoson_cli/test_self_test.py keeps this
#: in sync with the phoson_plugin_* directories).
OFFICIAL_PLUGIN_PACKAGES = (
    "phoson_plugin_bgjobs",
    "phoson_plugin_checkpoint",
    "phoson_plugin_computeruse",
    "phoson_plugin_mcp",
    "phoson_plugin_memory",
    "phoson_plugin_monitor",
    "phoson_plugin_otel",
    "phoson_plugin_peers",
    "phoson_plugin_questions",
    "phoson_plugin_ssh",
    "phoson_plugin_stt",
    "phoson_plugin_swarm",
)
_OPTIONAL_PLUGIN_DEPS = {
    "phoson_plugin_memory",
    "phoson_plugin_checkpoint",
    "phoson_plugin_stt",
    "phoson_plugin_computeruse",
}


def _check(
    results: list[tuple[str, str | None]], name: str, fn: Callable[[], None]
) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        results.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        results.append((name, None))


def _agent_turn() -> None:
    from phoson_agent import AgentEngine, AgentDoneEvent, tool
    from phoson_llm.schemas import (
        Message,
        TokenEvent,
        ModelConfig,
        LLMDoneEvent,
        LLMStartEvent,
        ToolCallEvent,
    )
    from phoson_llm.chats.base import BaseLLMChat

    class _ScriptedChat(BaseLLMChat):
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, messages, config, tools=None):  # type: ignore[override]
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            self.turn += 1
            if self.turn == 1:
                yield ToolCallEvent(
                    index=0, tool_call_id="t1", tool_name="echo", args={"text": "ok"}
                )
                yield LLMDoneEvent(content="", has_tool_calls=True)
            else:
                yield TokenEvent(content="done")
                yield LLMDoneEvent(content="done")

    @tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    async def run() -> None:
        engine = AgentEngine(chat=_ScriptedChat(), tools=[echo])
        events = [
            event
            async for event in engine.stream(
                [Message(role="user", content="self-test")],
                ModelConfig(model="self-test"),
            )
        ]
        last = events[-1]
        if not isinstance(last, AgentDoneEvent) or last.result.final_content != "done":
            raise RuntimeError(f"unexpected final event: {last!r}")

    asyncio.run(run())


def _tokenizer(encoding: str) -> Callable[[], None]:
    def check() -> None:
        import tiktoken

        if not tiktoken.get_encoding(encoding).encode("phoson self-test"):
            raise RuntimeError("empty encoding")

    return check


def _provider(module: str, class_name: str) -> Callable[[], None]:
    def check() -> None:
        getattr(importlib.import_module(module, "phoson_llm"), class_name)

    return check


def _plugin(package: str) -> Callable[[], None]:
    def check() -> None:
        from phoson_agent.plugin_manifest import load_manifest, find_manifest_for_module

        importlib.import_module(package)
        path = find_manifest_for_module(package)
        if path is None:
            raise RuntimeError("phoson-plugin.toml not found")
        load_manifest(path)

    return check


def _assets() -> None:
    from phoson_cli._frozen import asset_path

    asset_path("phos-ascii.txt").read_text(encoding="utf-8")


def run_self_test(stream=None) -> int:
    """Run every check; print one line each; return a process exit code."""
    from phoson_cli._frozen import is_frozen
    from phoson_llm.factory import _PROVIDERS

    out = stream or sys.stdout
    frozen = is_frozen()
    results: list[tuple[str, str | None]] = []
    _check(results, "agent turn (fake model, tool call)", _agent_turn)
    for encoding in ("cl100k_base", "o200k_base"):
        _check(results, f"tokenizer {encoding}", _tokenizer(encoding))
    seen: set[str] = set()
    for provider, (module, class_name) in sorted(_PROVIDERS.items()):
        if module in seen:
            continue
        seen.add(module)
        _check(results, f"provider {provider}", _provider(module, class_name))
    for package in OFFICIAL_PLUGIN_PACKAGES:
        _check(results, f"plugin {package}", _plugin(package))
    _check(results, "assets", _assets)

    failures = 0
    for name, error in results:
        optional = not frozen and (
            name.removeprefix("provider ") in _OPTIONAL_PROVIDERS
            or name.removeprefix("plugin ") in _OPTIONAL_PLUGIN_DEPS
        )
        if error is None:
            print(f"ok    {name}", file=out)
        elif optional:
            print(f"skip  {name} (optional extra not installed: {error})", file=out)
        else:
            failures += 1
            print(f"FAIL  {name}: {error}", file=out)
    total = len(results)
    print(f"{total - failures}/{total} checks passed", file=out)
    return 1 if failures else 0
