import datetime

from phoson_cli.session_picker import build_session_picker
from phoson_agent.sessions.models import SessionMeta


def _meta(session_id: str) -> SessionMeta:
    now = datetime.datetime.now(datetime.UTC)
    return SessionMeta(
        id=session_id,
        created_at=now,
        updated_at=now,
        message_count=1,
        total_cost=0.0,
        total_tokens=0,
        step_count=1,
        last_model="test-model",
    )


def test_session_picker_uses_exact_current_id_and_selects_it_initially() -> None:
    sessions = [_meta("same-prefix-first"), _meta("same-prefix-current")]

    picker = build_session_picker(sessions, "same-prefix-current")
    rendered = picker._render()

    selected_rows = [text for style, text in rendered if style == "class:row.selected"]
    active_rows = [text for style, text in rendered if style == "class:row.active"]
    assert len(selected_rows) == 1
    assert "▸  2" in selected_rows[0]
    assert "same-prefi" in selected_rows[0]
    assert active_rows == []


def test_empty_embedded_session_picker_renders_without_indexing_rows() -> None:
    picker = build_session_picker([], "current-id")

    rendered = "".join(text for _style, text in picker._render())

    assert "Saved Sessions" in rendered
