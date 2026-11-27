"""Code mode (``run_code``): scripts that call tools as functions.

These tests run real worker processes (the running interpreter) — the RPC,
the permission gate on nested calls, structured results, limits and the
OS sandbox are exercised end to end, not mocked.
"""

import sys
import time
import asyncio
from pathlib import Path

import pytest

from phoson_agent import Plugin
from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    TokenEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolResultBlock,
)
from phoson_agent.models import CODE_MODE_KEY, CODE_RESULT_KEY, AgentTool
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.code_mode import CodeRunError, CodeModeConfig
from phoson_agent.middleware import AgentMiddleware
from phoson_agent.permissions import PermissionPolicy, PermissionMiddleware
from phoson_agent.code_mode._bridge import (
    signature,
    script_value,
    assign_python_names,
)
from phoson_agent.code_mode._sandbox import (
    SandboxUnavailableError,
    bwrap_args,
    plan_command,
    detect_backend,
    seatbelt_profile,
)


class _IdleChat(BaseLLMChat):
    async def stream(self, messages, config, tools=None):  # type: ignore[override]
        return
        yield


@tool(parallel_safe=True)
def slow_add(a: int, b: int) -> dict:
    """Add two numbers slowly."""
    time.sleep(0.4)
    return {"sum": a + b}


@tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return text


@tool()
def as_json(n: int) -> str:
    """Return a JSON document as text."""
    return '{"items": [' + ", ".join(str(i) for i in range(n)) + "]}"


@tool()
def secret(name: str) -> str:
    """A tool the test policy denies."""
    return "should never run"


@tool()
def boom() -> str:
    """Always fails."""
    raise ValueError("kaboom")


@tool(parallel_safe=True)
def nap(seconds: float) -> str:
    """Sleep inside the host (tool time)."""
    time.sleep(seconds)
    return "rested"


TOOLS = [slow_add, echo, as_json, secret, boom, nap]


def _engine(config: CodeModeConfig | None = None, **kwargs) -> AgentEngine:
    return AgentEngine(
        chat=_IdleChat(),
        tools=list(TOOLS),
        code_mode=config or CodeModeConfig(sandbox="off"),
        **kwargs,
    )


async def _run(engine: AgentEngine, code: str, **args) -> str:
    run_code = engine._tools_by_name["run_code"]
    return await run_code.handler({"code": code, **args}, engine.context)


# ── registration ─────────────────────────────────────────────────────────


class TestRegistration:
    def test_disabled_by_default(self):
        engine = AgentEngine(chat=_IdleChat(), tools=list(TOOLS))
        assert "run_code" not in engine._tools_by_name

    def test_registered_as_visible_core_tool(self):
        engine = _engine()
        names = [t.name for t in engine.visible_tools()]
        assert "run_code" in names

    def test_description_lists_core_signatures(self):
        description = _engine()._tools_by_name["run_code"].description
        assert "tools.slow_add(a: int, b: int)" in description
        assert "tools.echo(text: str)" in description
        assert "run_code(" not in description

    def test_plugin_tools_are_left_to_search(self):
        plugin_tool = AgentTool(
            name="mcp_srv_lookup",
            description="Look something up.",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx=None: "found",
        )
        engine = _engine(plugins=[_StubPlugin([plugin_tool])])
        description = engine._tools_by_name["run_code"].description
        assert "mcp_srv_lookup" not in description
        assert "+ 1 more tool(s)" in description

    def test_invalid_sandbox_mode_rejected(self):
        with pytest.raises(ValueError):
            CodeModeConfig(sandbox="maybe")


# ── execution ────────────────────────────────────────────────────────────


class TestExecution:
    async def test_prints_come_back_with_call_summary(self):
        result = await _run(_engine(), "print(tools.echo(text='hello'))")
        assert result.splitlines()[0] == "hello"
        assert "[run_code: 1 tool call — echo×1]" in result

    async def test_positional_args_follow_schema_order(self):
        result = await _run(_engine(), "print(tools.slow_add(2, 3)['sum'])")
        assert result.startswith("5")

    async def test_dict_results_stay_structured(self):
        result = await _run(
            _engine(), "r = tools.slow_add(a=1, b=2); print(type(r).__name__, r['sum'])"
        )
        assert result.startswith("dict 3")

    async def test_json_text_is_decoded(self):
        result = await _run(_engine(), "print(sum(tools.as_json(n=5)['items']))")
        assert result.startswith("10")

    async def test_parallel_runs_parallel_safe_tools_concurrently(self):
        code = (
            "import time\n"
            "t = time.time()\n"
            "r = parallel([lambda i=i: tools.slow_add(a=i, b=i) for i in range(4)])\n"
            "print([x['sum'] for x in r], time.time() - t < 1.2)\n"
        )
        result = await _run(_engine(), code)
        assert result.startswith("[0, 2, 4, 6] True")

    async def test_failed_tool_raises_tool_error(self):
        code = (
            "try:\n"
            "    tools.boom()\n"
            "except ToolError as exc:\n"
            "    print('caught', exc)\n"
        )
        result = await _run(_engine(), code)
        assert "caught kaboom" in result
        assert "1 failed" in result

    async def test_unknown_attribute_points_to_search(self):
        code = "try:\n    tools.nope()\nexcept AttributeError as e:\n    print(e)"
        assert "tools.search" in await _run(_engine(), code)

    async def test_excluded_tools_are_not_callable(self):
        code = (
            "for name in ('run_code', 'discover', 'agents'):\n"
            "    try:\n"
            "        tools.call(name)\n"
            "    except ToolError:\n"
            "        print('blocked', name)\n"
        )
        result = await _run(_engine(), code)
        assert result.count("blocked") == 3

    async def test_metadata_opt_out(self):
        hidden = AgentTool(
            name="private",
            description="Not for scripts.",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx=None: "x",
            metadata={CODE_MODE_KEY: False},
        )
        engine = AgentEngine(
            chat=_IdleChat(), tools=[hidden], code_mode=CodeModeConfig(sandbox="off")
        )
        result = await _run(engine, "print(tools.list())")
        assert result.startswith("[]")

    async def test_search_and_help(self):
        code = (
            "hits = tools.search('add numbers')\n"
            "print(hits[0]['signature'])\n"
            "print('JSON Schema' in tools.help('slow_add'))\n"
        )
        result = await _run(_engine(), code)
        assert "tools.slow_add(a: int, b: int)" in result
        assert "True" in result

    async def test_exception_is_an_error_result_with_traceback(self):
        with pytest.raises(CodeRunError) as info:
            await _run(_engine(), "x = 1\nprint(1 / 0)\n")
        text = str(info.value)
        assert 'File "<run_code>", line 2' in text
        assert "print(1 / 0)" in text
        assert "ZeroDivisionError" in text

    async def test_syntax_error(self):
        with pytest.raises(CodeRunError, match="SyntaxError"):
            await _run(_engine(), "def broken(:\n")

    async def test_empty_code_rejected(self):
        with pytest.raises(CodeRunError):
            await _run(_engine(), "   ")

    async def test_no_output_hint(self):
        assert "no output" in await _run(_engine(), "x = 1")

    async def test_subprocess_output_cannot_corrupt_the_rpc(self):
        code = (
            "import os, subprocess, sys\n"
            "os.system('echo from-shell')\n"
            "subprocess.run([sys.executable, '-c', 'print(\"from-child\")'])\n"
            "print(tools.echo(text='still-works'))\n"
        )
        result = await _run(_engine(), code)
        assert "from-shell" in result
        assert "from-child" in result
        assert "still-works" in result

    async def test_secrets_are_not_in_the_worker_environment(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
        code = "import os; print(os.environ.get('OPENAI_API_KEY'))"
        assert (await _run(_engine(), code)).startswith("None")


# ── limits ───────────────────────────────────────────────────────────────


class TestLimits:
    async def test_timeout_stops_the_script(self):
        with pytest.raises(CodeRunError, match="timed out after 1s"):
            await _run(_engine(), "while True:\n    pass\n", timeout=1)

    async def test_time_inside_tools_does_not_count(self):
        result = await _run(_engine(), "print(tools.nap(seconds=1.5))", timeout=1)
        assert result.startswith("rested")

    async def test_tool_call_limit(self):
        engine = _engine(CodeModeConfig(sandbox="off", max_tool_calls=3))
        code = (
            "n = 0\n"
            "try:\n"
            "    for _ in range(10):\n"
            "        tools.echo(text='x'); n += 1\n"
            "except ToolError as e:\n"
            "    print(n, e)\n"
        )
        result = await _run(engine, code)
        assert result.startswith("3 tool call limit reached")

    async def test_output_is_capped(self):
        engine = _engine(CodeModeConfig(sandbox="off", max_output_bytes=1024))
        result = await _run(engine, "print('x' * 5000)")
        assert "output truncated at 1 KB" in result
        assert result.count("x") <= 1100

    async def test_cancellation_kills_the_worker(self, tmp_path):
        marker = tmp_path / "pid"
        code = (
            "import os, time\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        task = asyncio.create_task(_run(_engine(), code))
        for _ in range(100):
            if marker.exists() and marker.read_text():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(marker.read_text())
        await asyncio.sleep(0.2)
        assert not _alive(pid)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        return False  # os.kill(pid, 0) terminates on Windows; covered on POSIX
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); reaped means gone.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return waited == 0


# ── permission gate ──────────────────────────────────────────────────────


class TestPermissionGate:
    async def test_denied_nested_call_raises_tool_error(self):
        gate = PermissionMiddleware(PermissionPolicy(levels={"secret": "deny"}))
        engine = _engine(middlewares=[gate])
        code = (
            "try:\n"
            "    tools.secret(name='x')\n"
            "except ToolError as e:\n"
            "    print('refused:', e)\n"
        )
        result = await _run(engine, code)
        assert "refused: Blocked: secret was not executed" in result

    async def test_ask_prompts_once_per_nested_call(self):
        asked: list[tuple[str, dict]] = []

        async def on_ask(tool_name, args):
            asked.append((tool_name, args))
            return args.get("text") != "no"

        gate = PermissionMiddleware(
            PermissionPolicy(levels={"echo": "ask"}), on_ask=on_ask
        )
        engine = _engine(middlewares=[gate])
        code = (
            "print(tools.echo(text='yes'))\n"
            "try:\n"
            "    tools.echo(text='no')\n"
            "except ToolError:\n"
            "    print('denied')\n"
        )
        result = await _run(engine, code)
        assert [a["text"] for _, a in asked] == ["yes", "no"]
        assert "yes" in result and "denied" in result

    async def test_middleware_rewrite_applies_to_nested_calls(self):
        class Upper(AgentMiddleware):
            async def on_before_tool(self, call):
                if call.tool_name == "echo":
                    call.args = {"text": call.args["text"].upper()}
                return call

        result = await _run(
            _engine(middlewares=[Upper()]), "print(tools.echo(text='hi'))"
        )
        assert result.startswith("HI")

    async def test_nested_calls_skip_after_hooks(self):
        seen: list[str] = []

        class Spy(AgentMiddleware):
            async def on_after_tool(self, call, result, error):
                seen.append(call.tool_name)
                return result

        await _run(_engine(middlewares=[Spy()]), "tools.echo(text='a')")
        assert seen == []

    def test_run_code_inherits_bash_level(self):
        policy = PermissionPolicy(levels={"bash": "deny"})
        level, _source, intents = policy.evaluate("run_code", None, {"code": "x"})
        assert level == "deny"
        assert "lang_exec" in intents

    def test_run_code_asks_under_safe_defaults(self):
        from phoson_agent.intents import SAFE_DEFAULT_INTENT_LEVELS

        policy = PermissionPolicy(
            default_intent_levels=dict(SAFE_DEFAULT_INTENT_LEVELS)
        )
        assert policy.evaluate("run_code", None, {"code": "x"})[0] == "ask"

    def test_explicit_allow_wins(self):
        from phoson_agent.intents import SAFE_DEFAULT_INTENT_LEVELS

        policy = PermissionPolicy(
            levels={"run_code": "allow", "bash": "ask"},
            default_intent_levels=dict(SAFE_DEFAULT_INTENT_LEVELS),
        )
        assert policy.evaluate("run_code", None, {"code": "x"})[0] == "allow"


# ── through the agent loop ───────────────────────────────────────────────


class _ScriptedChat(BaseLLMChat):
    """Turn 1: call run_code. Turn 2: final answer."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.turn = 0

    async def stream(self, messages, config, tools=None):  # type: ignore[override]
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        self.turn += 1
        if self.turn == 1:
            yield ToolCallEvent(
                index=0,
                tool_call_id="rc1",
                tool_name="run_code",
                args={"code": self.code},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
        else:
            yield TokenEvent(content="done")
            yield LLMDoneEvent(content="done")


class TestAgentLoop:
    async def test_only_the_script_result_enters_history(self):
        chat = _ScriptedChat(
            "total = sum(tools.slow_add(a=i, b=0)['sum'] for i in range(3))\n"
            "print('total', total)\n"
        )
        engine = AgentEngine(
            chat=chat, tools=list(TOOLS), code_mode=CodeModeConfig(sandbox="off")
        )
        result = await engine.run(
            [Message(role="user", content="go")], ModelConfig(model="m")
        )
        results = [
            block
            for message in result.history
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        assert len(results) == 1
        assert results[0].result.startswith("total 3")
        assert not results[0].error
        tool_steps = [s for s in result.steps if s.kind == "tool"]
        assert [s.tool_name for s in tool_steps] == ["run_code"]

    async def test_masked_plugin_tools_are_callable_from_scripts(self):
        plugin_tool = AgentTool(
            name="mcp_srv_lookup",
            description="Look up a thing in the server.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=lambda args, ctx=None: {"value": args["key"] * 2},
        )
        engine = AgentEngine(
            chat=_IdleChat(),
            tools=list(TOOLS),
            plugins=[_StubPlugin([plugin_tool])],
            tool_budget_tokens=1,
            code_mode=CodeModeConfig(sandbox="off"),
        )
        assert engine.masked_tool_count() == 1
        code = (
            "hit = tools.search('lookup')[0]\n"
            "print(tools.call(hit['name'], key='ab')['value'])\n"
            "print(tools.mcp_srv_lookup('cd')['value'])\n"
        )
        result = await _run(engine, code)
        assert result.splitlines()[:2] == ["abab", "cdcd"]


# ── bridge helpers ───────────────────────────────────────────────────────


class TestBridgeHelpers:
    def test_signature_orders_required_first(self):
        params = {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "path": {"type": "string"},
                "flags": {"type": ["string", "null"]},
            },
            "required": ["path"],
        }
        assert (
            signature("f", params)
            == "f(path: str, limit: int = ..., flags: str | None = ...)"
        )

    def test_python_names_are_unique_identifiers(self):
        tools = {
            name: AgentTool(name, "", {}, lambda a, c=None: "")
            for name in ("mcp_x_get-item", "mcp_x_get_item", "class", "search", "1st")
        }
        names = assign_python_names(tools)
        assert names["mcp_x_get-item"] == "mcp_x_get_item"
        assert names["mcp_x_get_item"] == "mcp_x_get_item_2"
        assert names["class"] == "class_tool"
        assert names["search"] == "search_tool"
        assert names["1st"] == "_1st"
        assert all(n.isidentifier() for n in names.values())

    def test_script_value_hook(self):
        tool_ = AgentTool(
            "t",
            "",
            {},
            lambda a, c=None: "",
            metadata={CODE_RESULT_KEY: lambda raw: 42},
        )
        assert script_value(tool_, "anything") == 42

    def test_script_value_plain_text(self):
        assert script_value(None, "not json") == "not json"
        assert script_value(None, "[1, 2]") == [1, 2]
        assert script_value(None, "{broken") == "{broken"


# ── MCP structured results ───────────────────────────────────────────────


class TestMcpCodeResult:
    def _fn(self):
        from phoson_plugin_mcp._plugin import mcp_code_result

        return mcp_code_result

    def test_structured_content_wins(self):
        payload = {
            "success": True,
            "result": [{"type": "text", "text": '{"a": 1}'}],
            "structured_content": {"a": 1, "b": 2},
        }
        assert self._fn()(payload) == {"a": 1, "b": 2}

    def test_single_json_text_is_decoded(self):
        payload = {"success": True, "result": [{"type": "text", "text": "[1, 2]"}]}
        assert self._fn()(payload) == [1, 2]

    def test_multiple_text_blocks_are_joined(self):
        payload = {
            "success": True,
            "result": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        }
        assert self._fn()(payload) == "a\nb"

    def test_is_error_raises(self):
        payload = {
            "success": False,
            "is_error": True,
            "result": [{"type": "text", "text": "no such app"}],
        }
        with pytest.raises(RuntimeError, match="no such app"):
            self._fn()(payload)

    def test_transport_error_raises(self):
        with pytest.raises(RuntimeError, match="connection refused"):
            self._fn()({"error": "connection refused", "error_type": "OSError"})

    def test_non_text_parts_returned_raw(self):
        parts = [{"type": "resource", "resource": {"uri": "x"}}]
        assert self._fn()({"success": True, "result": parts}) == parts


# ── sandbox ──────────────────────────────────────────────────────────────


class TestSandboxPlans:
    def test_off_is_the_bare_command(self, tmp_path):
        plan = plan_command(
            ["python3"], mode="off", scratch=str(tmp_path), cwd=str(tmp_path)
        )
        assert plan.command == ["python3"] and plan.backend is None

    def test_required_without_backend_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "phoson_agent.code_mode._sandbox.detect_backend", lambda: None
        )
        with pytest.raises(SandboxUnavailableError):
            plan_command(["python3"], mode="required", scratch=str(tmp_path), cwd=".")

    def test_auto_without_backend_notes_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "phoson_agent.code_mode._sandbox.detect_backend", lambda: None
        )
        plan = plan_command(["python3"], mode="auto", scratch=str(tmp_path), cwd=".")
        assert plan.backend is None and "not sandboxed" in (plan.note or "")

    def test_seatbelt_profile_policy(self, tmp_path):
        home = tmp_path / "home"
        reallow = str((home / ".phoson" / "bin").resolve())
        profile = seatbelt_profile(scratch=str(tmp_path), home=home, reallow=[reallow])
        assert "(deny network*)" in profile
        assert "(deny file-write*)" in profile
        assert str((home / ".ssh").resolve()) in profile
        # The interpreter's own dir under a masked path is re-allowed last.
        assert profile.rindex("(allow file-read*") > profile.index("(deny file-read*")
        assert reallow in profile

    def test_bwrap_args_policy(self, tmp_path):
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".netrc").write_text("x")
        args = bwrap_args(scratch=str(tmp_path), home=home, reallow=[], cwd="/work")
        joined = " ".join(args)
        assert "--unshare-net" in args
        assert f"--tmpfs {home / '.ssh'}" in joined
        assert f"--ro-bind /dev/null {home / '.netrc'}" in joined
        assert args[-2:] == ["--chdir", "/work"]
        # /tmp is not masked: a workspace under /tmp must stay visible
        # (regression: bwrap could not chdir into it on Linux CI).
        assert "--tmpfs /tmp" not in joined


@pytest.mark.skipif(detect_backend() is None, reason="no OS sandbox on this host")
class TestSandboxEnforced:
    async def test_writes_outside_scratch_are_blocked(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        engine = _engine(CodeModeConfig(sandbox="required"))
        code = (
            "import os, tempfile\n"
            "try:\n"
            "    open('escape.txt', 'w').write('x'); print('WROTE')\n"
            "except OSError:\n"
            "    print('write blocked')\n"
            "p = os.path.join(tempfile.gettempdir(), 'ok.txt')\n"
            "open(p, 'w').write('1'); print('scratch ok')\n"
        )
        result = await _run(engine, code)
        assert "write blocked" in result and "scratch ok" in result
        assert not Path(tmp_path / "escape.txt").exists()

    async def test_network_is_blocked(self):
        engine = _engine(CodeModeConfig(sandbox="required"))
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=2); print('OPEN')\n"
            "except OSError as e:\n"
            "    print('net', type(e).__name__)\n"
        )
        result = await _run(engine, code)
        assert "OPEN" not in result

    async def test_credential_stores_are_unreadable(self):
        home = Path.home()
        (home / ".ssh").mkdir(exist_ok=True)
        (home / ".ssh" / "id_test").write_text("PRIVATE")
        engine = _engine(CodeModeConfig(sandbox="required"))
        code = (
            "import os\n"
            "try:\n"
            "    print(open(os.path.expanduser('~/.ssh/id_test')).read())\n"
            "except OSError:\n"
            "    print('read blocked')\n"
        )
        result = await _run(engine, code)
        assert "PRIVATE" not in result and "read blocked" in result

    async def test_tools_still_work_inside_the_sandbox(self):
        engine = _engine(CodeModeConfig(sandbox="required"))
        assert (await _run(engine, "print(tools.echo(text='ok'))")).startswith("ok")


class _StubPlugin(Plugin):
    def __init__(self, tools: list[AgentTool]) -> None:
        self._tools = tools

    @property
    def name(self) -> str:
        return "stub"

    @property
    def version(self) -> str:
        return "0.0.0"

    @property
    def description(self) -> str:
        return "test stub"

    def configure(self, config) -> None:
        return None

    def initialize(self) -> None:
        return None

    def cleanup(self) -> None:
        return None

    def get_tools(self) -> list[AgentTool]:
        return list(self._tools)

    def get_middlewares(self) -> list:
        return []

    def get_commands(self) -> list:
        return []


def test_worker_source_stays_python38_compatible():
    """The worker may run on a system python3 (the standalone binary has no
    general-purpose interpreter): keep its syntax parseable as 3.8, so an
    autofix targeting 3.12 cannot silently break it."""
    import ast

    from phoson_agent.code_mode import _worker

    source = Path(_worker.__file__).read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 8))
