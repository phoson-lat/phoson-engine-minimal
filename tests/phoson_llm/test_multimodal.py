"""Tests para la funcionalidad multimodal (imágenes, audio, video, PDF)."""

import pytest

from phoson_llm.chats import anthropic as anthropic_module
from phoson_llm.chats import _openai_compatible as openai_module
from phoson_llm.schemas import (
    Message,
    TextBlock,
    AudioBlock,
    ImageBlock,
    VideoBlock,
    DocumentBlock,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def text_message() -> Message:
    return Message(role="user", content="Hello world")


@pytest.fixture
def image_message() -> Message:
    return Message(
        role="user",
        content=[
            ImageBlock(source="https://example.com/photo.jpg"),
            TextBlock(text="¿Qué ves en esta imagen?"),
        ],
    )


@pytest.fixture
def audio_message() -> Message:
    return Message(
        role="user",
        content=[
            AudioBlock(source="https://example.com/audio.wav", format="wav"),
            TextBlock(text="Transcribe esto."),
        ],
    )


@pytest.fixture
def pdf_message() -> Message:
    return Message(
        role="user",
        content=[DocumentBlock(source="https://example.com/doc.pdf")],
    )


@pytest.fixture
def mixed_message() -> Message:
    return Message(
        role="user",
        content=[
            TextBlock(text="Revisa estos archivos."),
            ImageBlock(source="https://example.com/a.png"),
            ImageBlock(source="https://example.com/b.png"),
            AudioBlock(source="https://example.com/c.mp3", format="mp3"),
        ],
    )


# ==============================================================================
# tests/phoson_llm/schemas/test_inputs.py — blocks multimodales
# ==============================================================================


class TestMultimodalBlocks:
    """Tests para los nuevos ContentBlock multimodales."""

    def test_image_block_defaults(self):
        block = ImageBlock(source="https://example.com/img.png")
        assert block.source == "https://example.com/img.png"
        assert block.detail == "auto"
        assert block.media_type is None

    def test_image_block_full(self):
        block = ImageBlock(
            source="file://photo.jpg",
            detail="high",
            media_type="image/jpeg",
        )
        assert block.detail == "high"
        assert block.media_type == "image/jpeg"

    def test_audio_block_defaults(self):
        block = AudioBlock(source="https://example.com/recording.wav")
        assert block.source == "https://example.com/recording.wav"
        assert block.format == "wav"
        assert block.duration_ms is None

    def test_video_block_defaults(self):
        block = VideoBlock(source="https://example.com/video.mp4")
        assert block.source == "https://example.com/video.mp4"
        assert block.sampling_interval_ms == 2000

    def test_document_block(self):
        block = DocumentBlock(source="file://report.pdf", pages=42)
        assert block.source == "file://report.pdf"
        assert block.pages == 42


# ==============================================================================
# tests/phoson_llm/chats/test_openai_conversion.py
# ==============================================================================


class TestOpenAIMessageConversion:
    """Tests para la conversión de mensajes Phoson → OpenAI."""

    def test_simple_text_message(self, text_message):
        result = openai_module._convert_messages([text_message])
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello world"

    def test_image_message_converts_to_parts(self, image_message):
        result = openai_module._convert_messages([image_message])
        assert len(result) == 1
        assert result[0]["role"] == "user"
        parts = result[0]["content"]
        assert isinstance(parts, list)
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == "¿Qué ves en esta imagen?"
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == "https://example.com/photo.jpg"

    def test_audio_message_converts_to_input_audio(self, audio_message):
        result = openai_module._convert_messages([audio_message])
        assert len(result) == 1
        parts = result[0]["content"]
        audio_part = next(p for p in parts if p["type"] == "input_audio")
        assert audio_part["input_audio"]["format"] == "wav"

    def test_video_message_becomes_text_fallback(self):
        msg = Message(
            role="user",
            content=[VideoBlock(source="https://example.com/v.mp4")],
        )
        result = openai_module._convert_messages([msg])
        parts = result[0]["content"]
        assert parts[0]["type"] == "text"
        assert "Video not directly supported" in parts[0]["text"]

    def test_pdf_message_becomes_text_fallback(self, pdf_message):
        result = openai_module._convert_messages([pdf_message])
        parts = result[0]["content"]
        assert parts[0]["type"] == "text"
        assert (
            "Unsupported content block" in parts[0]["text"]
            or "not directly supported" in parts[0]["text"]
        )

    def test_mixed_multimodal_message(self, mixed_message):
        result = openai_module._convert_messages([mixed_message])
        parts = result[0]["content"]
        types = [p["type"] for p in parts]
        assert "text" in types
        assert "image_url" in types
        assert "input_audio" in types

    def test_image_with_detail_high(self):
        msg = Message(
            role="user",
            content=[ImageBlock(source="https://example.com/img.png", detail="high")],
        )
        result = openai_module._convert_messages([msg])
        assert result[0]["content"][0]["image_url"]["detail"] == "high"

    def test_system_message_not_duplicated(self):
        system_msg = Message(role="system", content="You are helpful.")
        user_msg = Message(role="user", content="Hi")
        result = openai_module._convert_messages([system_msg, user_msg])
        system_msgs = [m for m in result if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "You are helpful."


# ==============================================================================
# tests/phoson_llm/chats/test_anthropic_conversion.py
# ==============================================================================


class TestAnthropicMessageConversion:
    """Tests para la conversión de mensajes Phoson → Anthropic."""

    def test_simple_text_message(self, text_message):
        result = anthropic_module._convert_messages([text_message])
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello world"

    def test_image_message_converts_to_image_block(self, image_message):
        result = anthropic_module._convert_messages([image_message])
        assert len(result) == 1
        assert result[0]["role"] == "user"
        blocks = result[0]["content"]
        image_block = next(b for b in blocks if b["type"] == "image")
        assert image_block["source"]["type"] == "url"
        assert image_block["source"]["url"] == "https://example.com/photo.jpg"

    def test_audio_message_becomes_text_fallback(self, audio_message):
        result = anthropic_module._convert_messages([audio_message])
        blocks = result[0]["content"]
        # El fallback de audio se convierte a text block al final
        fallback_block = next(
            (
                b
                for b in blocks
                if "Audio not supported by Anthropic" in b.get("text", "")
            ),
            None,
        )
        assert fallback_block is not None
        assert "Audio not supported by Anthropic" in fallback_block["text"]

    def test_pdf_message_converts_to_document_block(self, pdf_message):
        result = anthropic_module._convert_messages([pdf_message])
        blocks = result[0]["content"]
        doc_block = next(b for b in blocks if b["type"] == "document")
        assert doc_block["source"]["type"] == "url"
        assert doc_block["source"]["url"] == "https://example.com/doc.pdf"

    def test_video_message_becomes_text_fallback(self):
        msg = Message(
            role="user",
            content=[VideoBlock(source="https://example.com/v.mp4")],
        )
        result = anthropic_module._convert_messages([msg])
        blocks = result[0]["content"]
        fallback_block = next(
            (
                b
                for b in blocks
                if "Video not supported by Anthropic" in b.get("text", "")
            ),
            None,
        )
        assert fallback_block is not None

    def test_system_message_extracted(self):
        system_msg = Message(role="system", content="You are a helpful assistant.")
        user_msg = Message(role="user", content="Hi")
        result = anthropic_module._convert_messages([system_msg, user_msg])
        # The system message must not appear as a conversation message
        roles = [m.get("role") for m in result]
        assert "system" not in roles
        assert "user" in roles

    def test_mixed_multimodal_message(self, mixed_message):
        result = anthropic_module._convert_messages([mixed_message])
        blocks = result[0]["content"]
        types = [b["type"] for b in blocks]
        assert "text" in types
        assert "image" in types


# ==============================================================================
# tests/phoson_cli/test_attachments.py
# ==============================================================================


class TestAttachmentManager:
    """Tests para AttachmentManager."""

    def test_attach_image(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")

        mgr = AttachmentManager()
        mgr.attach(str(img))

        assert len(mgr) == 1
        block = mgr.flush()[0]
        assert isinstance(block, ImageBlock)
        assert block.source.startswith("file://")
        assert block.media_type == "image/png"

    def test_attach_audio(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        audio = tmp_path / "voice.wav"
        audio.write_bytes(b"RIFF")

        mgr = AttachmentManager()
        mgr.attach(str(audio))

        block = mgr.flush()[0]
        assert isinstance(block, AudioBlock)
        assert block.format == "wav"

    def test_attach_pdf(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        mgr = AttachmentManager()
        mgr.attach(str(pdf))

        block = mgr.flush()[0]
        assert isinstance(block, DocumentBlock)

    def test_attach_unsupported_type(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        bad = tmp_path / "file.exe"
        bad.write_bytes(b"")

        mgr = AttachmentManager()
        with pytest.raises(ValueError, match="Unsupported file type"):
            mgr.attach(str(bad))

    def test_attach_file_not_found(self):
        from phoson_cli.attachments import AttachmentManager

        mgr = AttachmentManager()
        with pytest.raises(FileNotFoundError):
            mgr.attach("/nonexistent/path/to/file.png")

    def test_flush_clears_pending(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG")
        aud = tmp_path / "b.wav"
        aud.write_bytes(b"RIFF")

        mgr = AttachmentManager()
        mgr.attach(str(img))
        mgr.attach(str(aud))

        assert len(mgr) == 2
        mgr.flush()
        assert len(mgr) == 0

    def test_clear_keeps_flush_clean(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        img = tmp_path / "x.png"
        img.write_bytes(b"\x89PNG")

        mgr = AttachmentManager()
        mgr.attach(str(img))
        mgr.clear()
        assert len(mgr.flush()) == 0

    def test_attach_multiple_images(self, tmp_path):
        from phoson_cli.attachments import AttachmentManager

        for i in range(3):
            f = tmp_path / f"img{i}.png"
            f.write_bytes(b"\x89PNG")

        mgr = AttachmentManager()
        for i in range(3):
            mgr.attach(str(tmp_path / f"img{i}.png"))

        blocks = mgr.flush()
        assert len(blocks) == 3
        assert all(isinstance(b, ImageBlock) for b in blocks)
