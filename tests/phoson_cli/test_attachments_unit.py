import pytest

from phoson_cli.attachments import AttachmentManager, _suffix_to_mime


def test_attachment_manager_default_empty() -> None:
    mgr = AttachmentManager()
    assert len(mgr) == 0
    assert bool(mgr) is False


def test_attachment_manager_attach_nonexistent_raises() -> None:
    mgr = AttachmentManager()
    with pytest.raises(FileNotFoundError):
        mgr.attach("/nonexistent/file.txt")


def test_attachment_manager_attach_unsupported_type_raises(tmp_path) -> None:
    mgr = AttachmentManager()
    dummy = tmp_path / "test.xyz"
    dummy.touch()
    with pytest.raises(ValueError, match="Unsupported file type"):
        mgr.attach(str(dummy))


def test_attachment_manager_flush_clears_pending(tmp_path) -> None:
    mgr = AttachmentManager()
    dummy = tmp_path / "test.txt"
    dummy.write_text("hello")
    with pytest.raises(ValueError):
        mgr.attach(str(dummy))
    assert len(mgr) == 0


def test_suffix_to_mime_image_types() -> None:
    assert _suffix_to_mime(".png") == "image/png"
    assert _suffix_to_mime(".jpg") == "image/jpeg"
    assert _suffix_to_mime(".jpeg") == "image/jpeg"
    assert _suffix_to_mime(".gif") == "image/gif"
    assert _suffix_to_mime(".webp") == "image/webp"
    assert _suffix_to_mime(".svg") == "image/svg+xml"
    assert _suffix_to_mime(".bmp") == "image/bmp"


def test_suffix_to_mime_unknown_returns_octet_stream() -> None:
    assert _suffix_to_mime(".xyz") == "application/octet-stream"
    assert _suffix_to_mime(".unknown") == "application/octet-stream"
