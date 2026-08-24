from phoson_cli.tools import build_tools
from phoson_llm.schemas import ImageBlock
from phoson_agent.models import ImageToolResult
from phoson_cli.tools.view_image import MAX_IMAGE_BYTES, view_image


def test_view_image_is_registered() -> None:
    assert view_image in build_tools()


def test_view_image_missing_file_returns_text_error() -> None:
    result = view_image.handler({"path": "/nonexistent/shot.png"}, None)
    assert isinstance(result, str)
    assert "not found" in result.lower()


def test_view_image_unsupported_extension_returns_text_error(tmp_path) -> None:
    dummy = tmp_path / "notes.txt"
    dummy.write_text("hello")
    result = view_image.handler({"path": str(dummy)}, None)
    assert isinstance(result, str)
    assert "unsupported" in result.lower()


def test_view_image_success_returns_image_tool_result(tmp_path) -> None:
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    result = view_image.handler({"path": str(png)}, None)

    assert isinstance(result, ImageToolResult)
    assert "shot.png" in result.text
    assert isinstance(result.image, ImageBlock)
    assert result.image.source == f"file://{png}"
    assert result.image.media_type == "image/png"


def test_view_image_over_size_limit_returns_text_error(tmp_path) -> None:
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (MAX_IMAGE_BYTES + 1))

    result = view_image.handler({"path": str(big)}, None)

    assert isinstance(result, str)
    assert "too large" in result.lower()
