"""Unit tests for phoson_llm.utils."""

import os
import base64

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    guess_mime,
    map_error_code,
    load_file_as_base64,
    extract_context_window,
    is_context_length_error,
    missing_attachment_placeholder,
)


class TestGuessMime:
    def test_known_image_extensions(self):
        assert guess_mime("photo.png") == "image/png"
        assert guess_mime("photo.jpg") == "image/jpeg"
        assert guess_mime("photo.jpeg") == "image/jpeg"
        assert guess_mime("photo.gif") == "image/gif"
        assert guess_mime("photo.webp") == "image/webp"

    def test_known_document_extensions(self):
        assert guess_mime("doc.pdf") == "application/pdf"

    def test_known_video_extensions(self):
        assert guess_mime("clip.mp4") == "video/mp4"
        assert guess_mime("clip.webm") == "video/webm"
        assert guess_mime("clip.mov") == "video/quicktime"

    def test_known_audio_extensions(self):
        assert guess_mime("song.mp3") == "audio/mpeg"
        assert guess_mime("song.wav") == "audio/wav"
        assert guess_mime("song.ogg") == "audio/ogg"
        assert guess_mime("song.flac") == "audio/flac"

    def test_unknown_extension_returns_octet_stream(self):
        assert guess_mime("file.xyz") == "application/octet-stream"
        assert guess_mime("file") == "application/octet-stream"

    def test_case_insensitive(self):
        assert guess_mime("photo.PNG") == "image/png"
        assert guess_mime("photo.JPG") == "image/jpeg"

    def test_path_with_directories(self):
        assert guess_mime("/some/path/to/photo.png") == "image/png"


class TestMapErrorCode:
    def test_known_status_codes(self):
        assert map_error_code(401) == "auth"
        assert map_error_code(403) == "permission"
        assert map_error_code(404) == "not_found"
        assert map_error_code(429) == "rate_limit"
        assert map_error_code(500) == "server_error"
        assert map_error_code(503) == "overloaded"
        assert map_error_code(529) == "overloaded"

    def test_unknown_status_code_returns_unknown(self):
        assert map_error_code(418) == "unknown"
        assert map_error_code(200) == "unknown"
        assert map_error_code(301) == "unknown"


class TestLoadFileAsBase64:
    def test_encodes_file_content(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")

        result = load_file_as_base64(str(f))

        expected_b64 = base64.b64encode(b"hello world").decode("ascii")
        assert result == f"data:application/octet-stream;base64,{expected_b64}"

    def test_uses_provided_media_type(self, tmp_path):
        f = tmp_path / "image.dat"
        f.write_bytes(b"\x89PNG")

        result = load_file_as_base64(str(f), media_type="image/png")

        assert result.startswith("data:image/png;base64,")

    def test_guesses_mime_from_extension(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG")

        result = load_file_as_base64(str(f))

        assert result.startswith("data:image/png;base64,")

    def test_missing_file_returns_none_and_warns(self, tmp_path, caplog):
        """I-119: a wiped attachment must degrade, not crash."""
        import logging

        missing = tmp_path / "shot-accepted.png"

        with caplog.at_level(logging.WARNING, logger="phoson_llm.utils"):
            result = load_file_as_base64(str(missing))

        assert result is None
        assert "attachment file missing" in caplog.text
        assert str(missing) in caplog.text

    def test_directory_path_returns_none(self, tmp_path, caplog):
        """I-119: a path that is a directory must also degrade gracefully."""
        import logging

        d = tmp_path / "folder"
        d.mkdir()

        with caplog.at_level(logging.WARNING, logger="phoson_llm.utils"):
            result = load_file_as_base64(str(d))

        assert result is None

    def test_unreadable_file_returns_none_and_warns(self, tmp_path, caplog):
        """I-119: an unreadable file (e.g. permission denied) must degrade too."""
        import logging

        f = tmp_path / "secret.png"
        f.write_bytes(b"\x89PNG")
        f.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="phoson_llm.utils"):
                result = load_file_as_base64(str(f))
            if os.geteuid() != 0:  # root bypasses file permissions
                assert result is None
                assert "unreadable" in caplog.text
        finally:
            f.chmod(0o644)


class TestMissingAttachmentPlaceholder:
    """I-119: the visible text that replaces a missing file:// attachment."""

    def test_includes_kind_and_file_name(self):
        assert (
            missing_attachment_placeholder("image", "/tmp/shot-accepted.png")
            == "[image no longer available: shot-accepted.png]"
        )

    def test_audio_and_document_kinds(self):
        assert (
            missing_attachment_placeholder("audio", "/tmp/voice.wav")
            == "[audio no longer available: voice.wav]"
        )
        assert (
            missing_attachment_placeholder("document", "/home/u/spec.pdf")
            == "[document no longer available: spec.pdf]"
        )

    def test_path_without_basename_keeps_raw_path(self):
        # Trailing-slash / root-ish paths have no name — fall back to raw path.
        assert missing_attachment_placeholder("image", "/") == (
            "[image no longer available: /]"
        )


class TestContextLengthError:
    """I-91: classification of provider context-window 400 errors."""

    def test_openai_style_prompt_too_long(self):
        assert is_context_length_error(
            400, "prompt is too long: 199999 tokens > 198000 maximum"
        )

    def test_anthropic_style(self):
        assert is_context_length_error(
            400, "prompt is too long: 188039 maximum allowed tokens"
        )

    def test_vllm_style(self):
        assert is_context_length_error(
            400, "This model's maximum context length is 8192 tokens"
        )

    def test_generic_exceeds_maximum(self):
        assert is_context_length_error(
            400, "Your input exceeds the model's maximum context"
        )

    def test_ollama_style(self):
        assert is_context_length_error(400, "context length exceeded")

    def test_non_400_is_not_context_error(self):
        assert not is_context_length_error(404, "prompt is too long")
        assert not is_context_length_error(429, "prompt is too long")
        assert not is_context_length_error(500, "context length")

    def test_unknown_400_is_not_context_error(self):
        # A 400 with an unrelated message must NOT trigger compaction.
        assert not is_context_length_error(400, "invalid model name")
        assert not is_context_length_error(400, "invalid request body")
        assert not is_context_length_error(400, "")

    def test_none_status_still_matches_message(self):
        # Some adapters surface the status separately; a None status
        # should still classify on the message text.
        assert is_context_length_error(None, "prompt is too long")

    def test_error_code_constant(self):
        assert CONTEXT_LENGTH_ERROR_CODE == "context_length_exceeded"


class TestExtractContextWindow:
    def test_vllm_maximum_context_length(self):
        assert (
            extract_context_window("This model's maximum context length is 8192 tokens")
            == 8192
        )

    def test_openai_gt_form(self):
        assert (
            extract_context_window("prompt is too long: 199999 tokens > 198000 maximum")
            == 198000
        )

    def test_comma_separated_number(self):
        assert extract_context_window("context length is 128,000 tokens") == 128_000

    def test_no_number_returns_none(self):
        assert extract_context_window("prompt is too long") is None
        assert extract_context_window("") is None
