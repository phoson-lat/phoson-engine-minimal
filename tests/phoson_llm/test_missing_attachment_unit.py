"""I-119: a ``file://`` attachment whose file was deleted (e.g. ``/tmp``
cleanup between runs) must degrade to a visible text placeholder instead of
crashing message conversion with ``FileNotFoundError`` when a session is
reloaded (``--resume`` / summarizer retry)."""

import pytest

from phoson_llm.chats import anthropic as anthropic_module
from phoson_llm.chats import _openai_compatible as openai_module
from phoson_llm.utils import missing_attachment_placeholder
from phoson_llm.schemas import (
    Message,
    TextBlock,
    AudioBlock,
    ImageBlock,
    DocumentBlock,
)

GHOST_IMAGE = "/tmp/shot-accepted.png"
GHOST_AUDIO = "/tmp/voice.wav"
GHOST_PDF = "/tmp/spec.pdf"


class TestOpenAICompatibleMissingFile:
    def test_missing_image_block_degrades_to_text(self):
        block = ImageBlock(source=f"file://{GHOST_IMAGE}")
        result = openai_module._convert_content_block(block)
        assert result == {
            "type": "text",
            "text": missing_attachment_placeholder("image", GHOST_IMAGE),
        }

    def test_missing_audio_block_degrades_to_text(self):
        block = AudioBlock(source=f"file://{GHOST_AUDIO}", format="wav")
        result = openai_module._convert_content_block(block)
        assert result == {
            "type": "text",
            "text": missing_attachment_placeholder("audio", GHOST_AUDIO),
        }

    def test_convert_messages_no_crash_no_stderr(self, capfd, caplog):
        """The issue's acceptance criteria: reload-style conversion must not
        crash and must not write anything to stderr."""
        msg = Message(
            role="user",
            content=[
                ImageBlock(source=f"file://{GHOST_IMAGE}"),
                TextBlock(text="what was in this screenshot?"),
            ],
        )
        result = openai_module._convert_messages([msg])
        capfd.readouterr()  # drain anything from the conversion above

        parts = result[0]["content"]
        assert parts[0] == {"type": "text", "text": "what was in this screenshot?"}
        assert parts[1] == {
            "type": "text",
            "text": "[image no longer available: shot-accepted.png]",
        }
        assert "FileNotFoundError" not in capfd.readouterr().err
        assert "attachment file missing" in caplog.text

    def test_message_with_only_missing_image_is_not_empty(self):
        msg = Message(role="user", content=[ImageBlock(source=f"file://{GHOST_IMAGE}")])
        result = openai_module._convert_messages([msg])
        content = result[0]["content"]
        assert isinstance(content, list) and content
        assert content[0]["text"] == "[image no longer available: shot-accepted.png]"

    def test_existing_file_source_still_encodes(self, tmp_path):
        """Regression: a live file:// source keeps the base64 encoding path."""
        f = tmp_path / "ok.png"
        f.write_bytes(b"\x89PNG\r\n")
        block = ImageBlock(source=f"file://{f}")
        result = openai_module._convert_content_block(block)
        assert result["type"] == "image_url"
        assert result["image_url"]["url"].startswith("data:image/png;base64,")


class TestAnthropicMissingFile:
    def test_missing_image_block_degrades_to_text(self):
        block = ImageBlock(source=f"file://{GHOST_IMAGE}")
        result = anthropic_module._convert_content_block(block)
        assert result == {
            "type": "text",
            "text": missing_attachment_placeholder("image", GHOST_IMAGE),
        }

    def test_missing_document_block_degrades_to_text(self):
        block = DocumentBlock(source=f"file://{GHOST_PDF}")
        result = anthropic_module._convert_content_block(block)
        assert result == {
            "type": "text",
            "text": missing_attachment_placeholder("document", GHOST_PDF),
        }

    def test_convert_messages_no_crash(self):
        msg = Message(
            role="user", content=[DocumentBlock(source=f"file://{GHOST_PDF}")]
        )
        result = anthropic_module._convert_messages([msg])
        assert result[0]["content"] == [
            {"type": "text", "text": "[document no longer available: spec.pdf]"}
        ]

    def test_url_source_still_passes_through(self):
        block = ImageBlock(source="https://example.com/photo.jpg")
        result = anthropic_module._convert_content_block(block)
        assert result == {
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/photo.jpg"},
        }


class TestGeminiMissingFile:
    @pytest.fixture(autouse=True)
    def _skip_if_no_sdk(self):
        google = pytest.importorskip("google.genai")
        self.types = google.types

    def test_missing_image_block_degrades_to_text_part(self):
        from phoson_llm.chats.gemini import _convert_block

        part = _convert_block(self.types, ImageBlock(source=f"file://{GHOST_IMAGE}"))
        assert part.text == missing_attachment_placeholder("image", GHOST_IMAGE)

    def test_missing_document_block_degrades_to_text_part(self):
        from phoson_llm.chats.gemini import _convert_block

        part = _convert_block(self.types, DocumentBlock(source=f"file://{GHOST_PDF}"))
        assert part.text == missing_attachment_placeholder("document", GHOST_PDF)

    def test_missing_image_convert_messages_no_crash(self):
        from phoson_llm.chats.gemini import _convert_messages

        msg = Message(
            role="user",
            content=[ImageBlock(source=f"file://{GHOST_IMAGE}")],
        )
        result = _convert_messages([msg])
        assert (
            result[0].parts[0].text == "[image no longer available: shot-accepted.png]"
        )
