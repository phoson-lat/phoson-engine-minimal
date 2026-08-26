"""Tests for IMPROVEMENTS.md E1 — offloading large tool outputs to disk."""

from pathlib import Path

import pytest

from phoson_llm.schemas import ToolCallEvent
from phoson_agent.plugins.offload import (
    DEFAULT_MAX_CHARS,
    OffloadMiddleware,
    offload_output,
    build_offload_stub,
)


def _call(tool_name: str = "bash", tool_call_id: str = "call-1") -> ToolCallEvent:
    return ToolCallEvent(tool_call_id=tool_call_id, tool_name=tool_name, args={})


class TestOffloadOutput:
    def test_small_result_is_untouched(self, tmp_path: Path) -> None:
        text = "small result"
        out = offload_output(
            text,
            tool_name="bash",
            tool_call_id="c1",
            output_dir=tmp_path,
            max_chars=100,
            head_chars=10,
            tail_chars=5,
            error=False,
        )
        assert out == text
        assert list(tmp_path.iterdir()) == []

    def test_large_result_is_offloaded(self, tmp_path: Path) -> None:
        text = "H" * 5_000 + "MIDDLE" + "T" * 5_000
        out = offload_output(
            text,
            tool_name="bash",
            tool_call_id="c2",
            output_dir=tmp_path,
            max_chars=100,
            head_chars=50,
            tail_chars=20,
            error=False,
        )

        # The context stub is small and points at a real file.
        assert len(out) < 500
        assert "offloaded to disk" in out
        assert "10006 chars" in out
        assert "Use read_file" in out
        assert out.startswith("[Large bash output offloaded to disk")

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name.startswith("bash_c2_")
        # The file holds the full, untruncated output.
        assert files[0].read_text(encoding="utf-8") == text

    def test_head_and_tail_present(self, tmp_path: Path) -> None:
        text = "A" * 300 + "B" * 300 + "C" * 300
        out = offload_output(
            text,
            tool_name="read_file",
            tool_call_id="c3",
            output_dir=tmp_path,
            max_chars=50,
            head_chars=30,
            tail_chars=20,
            error=False,
        )
        assert "A" * 30 in out  # head
        assert "C" * 20 in out  # tail
        assert "--- head (30 chars) ---" in out
        assert "--- tail (20 chars) ---" in out

    def test_error_flag_is_surfaced(self, tmp_path: Path) -> None:
        text = "E" * 500
        out = offload_output(
            text,
            tool_name="bash",
            tool_call_id="c4",
            output_dir=tmp_path,
            max_chars=50,
            head_chars=10,
            tail_chars=10,
            error=True,
        )
        assert "Result marked as an error by the tool." in out

    def test_write_failure_falls_back_to_full_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failing file write must not break the run: the middleware is
        # best-effort and returns the original text.
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", _raise)
        text = "X" * 500
        out = offload_output(
            text,
            tool_name="bash",
            tool_call_id="c5",
            output_dir=tmp_path,
            max_chars=50,
            head_chars=10,
            tail_chars=10,
            error=False,
        )
        assert out == text


class TestBuildOffloadStub:
    def test_stub_shape(self) -> None:
        stub = build_offload_stub(
            tool_name="bash",
            tool_call_id="c9",
            original_chars=10_000,
            path="/tmp/out.txt",
            head="first",
            tail="last",
            error=False,
        )
        lines = stub.splitlines()
        assert lines[0] == "[Large bash output offloaded to disk: 10000 chars]"
        assert lines[1] == "Full output: /tmp/out.txt"
        assert lines[2] == "Use read_file on that path to retrieve the full content."
        assert stub.endswith("---")
        assert "first" in stub and "last" in stub


class TestOffloadMiddleware:
    @pytest.mark.asyncio
    async def test_on_after_tool_offloads_large(self, tmp_path: Path) -> None:
        mw = OffloadMiddleware(
            max_chars=100, head_chars=20, tail_chars=10, output_dir=tmp_path
        )
        result = await mw.on_after_tool(_call("bash", "m1"), "Z" * 10_000, False)
        assert result != "Z" * 10_000
        assert "offloaded to disk" in result
        assert len(list(tmp_path.iterdir())) == 1

    @pytest.mark.asyncio
    async def test_on_after_tool_keeps_small(self, tmp_path: Path) -> None:
        mw = OffloadMiddleware(max_chars=DEFAULT_MAX_CHARS, output_dir=tmp_path)
        result = await mw.on_after_tool(_call("read_file", "m2"), "tiny", False)
        assert result == "tiny"
        assert list(tmp_path.iterdir()) == []

    def test_defaults(self) -> None:
        mw = OffloadMiddleware()
        assert mw.max_chars == DEFAULT_MAX_CHARS
        assert mw.output_dir.name == "compacted"
