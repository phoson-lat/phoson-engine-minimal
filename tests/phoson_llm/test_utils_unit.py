"""Unit tests for phoson_llm.utils."""

import base64

from phoson_llm.utils import guess_mime, map_error_code, load_file_as_base64


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
