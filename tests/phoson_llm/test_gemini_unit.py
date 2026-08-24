import pytest

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.gemini import GeminiChat

try:
    import google.genai as google
except ImportError:
    google = None

pytestmark = pytest.mark.skipif(google is None, reason="google-genai not installed")


def test_is_base_llm_chat_subclass():
    chat = GeminiChat(api_key="test-key")
    assert isinstance(chat, BaseLLMChat)


def test_default_api_key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    chat = GeminiChat()
    assert chat._api_key == "env-key"


def test_repr_includes_gemini():
    chat = GeminiChat(api_key="test-key")
    assert "Gemini" in repr(chat)


def _convert(messages):
    from phoson_llm.chats.gemini import _convert_messages

    return _convert_messages(messages)


def test_local_image_becomes_inline_base64(tmp_path):
    """Regression for #53: file:// images must be read and inlined, not
    passed as a local path to Part.from_uri (Gemini can't fetch those)."""
    import base64

    from phoson_llm.schemas import Message, ImageBlock

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNGfake")

    out = _convert(
        [Message(role="user", content=[
            ImageBlock(source=f"file://{img}", media_type="image/png"),
        ])]
    )
    part = out[0].parts[0]
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "image/png"
    assert base64.b64decode(part.inline_data.data) == b"\x89PNGfake"


def test_hosted_uri_passes_through_as_file_uri():
    from phoson_llm.schemas import Message, ImageBlock

    out = _convert(
        [Message(role="user", content=[
            ImageBlock(source="gs://bucket/pic.png", media_type="image/png"),
        ])]
    )
    assert out[0].parts[0].file_data.file_uri == "gs://bucket/pic.png"


def test_local_pdf_becomes_inline_base64(tmp_path):
    from phoson_llm.schemas import Message, DocumentBlock

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-fake")

    out = _convert(
        [Message(role="user", content=[
            DocumentBlock(source=f"file://{pdf}"),
        ])]
    )
    part = out[0].parts[0]
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "application/pdf"


def test_audio_and_video_get_text_placeholder():
    """Regression for #53: unsupported blocks must not be silently dropped."""
    from phoson_llm.schemas import Message, AudioBlock, VideoBlock

    out = _convert(
        [Message(role="user", content=[
            AudioBlock(source="file:///tmp/a.mp3", format="mp3"),
            VideoBlock(source="file:///tmp/v.mp4"),
        ])]
    )
    texts = [p.text for p in out[0].parts]
    assert len(texts) == 2
    assert "Audio not supported by Gemini" in texts[0]
    assert "Video not supported by Gemini" in texts[1]


def test_tool_blocks_raise():
    import pytest

    from phoson_llm.schemas import Message, ToolUseBlock

    with pytest.raises(TypeError):
        _convert(
            [Message(role="assistant", content=[
                ToolUseBlock(id="t1", name="bash", arguments={}),
            ])]
        )
