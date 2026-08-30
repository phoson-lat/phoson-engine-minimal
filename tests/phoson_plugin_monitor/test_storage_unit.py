"""Unit tests for the monitor plugin persistence layer.

Covers the registry (atomic JSON) and the wake queue (JSONL) with a
real temp filesystem — no network, no timers, no flakiness.
"""

import json
from pathlib import Path

import pytest

from phoson_plugin_monitor.storage import (
    WakeEvent,
    WakeQueue,
    MonitorDef,
    MonitorStore,
)


def _mk(name: str = "m", kind: str = "interval", **kw) -> MonitorDef:
    defaults = {"spec": {"seconds": 1}, "session_id": "sess"}
    defaults.update(kw)
    return MonitorDef(name=name, kind=kind, **defaults)


# ── MonitorStore ───────────────────────────────────────────────────────────────


class TestMonitorStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        store = MonitorStore(tmp_path)
        store.add(_mk("build", spec={"seconds": 5, "once": True}))
        store.add(_mk("logs", kind="file", spec={"path": "x.log"}))

        reloaded = MonitorStore(tmp_path)
        assert [m.name for m in reloaded.list()] == ["logs", "build"]  # newest first
        m = reloaded.get("build")
        assert m is not None
        assert m.spec == {"seconds": 5, "once": True}
        assert m.state == "running"
        assert m.session_id == "sess"

    def test_duplicate_add_raises(self, tmp_path: Path) -> None:
        store = MonitorStore(tmp_path)
        store.add(_mk("m"))
        with pytest.raises(ValueError):
            store.add(_mk("m"))

    def test_replace_and_remove(self, tmp_path: Path) -> None:
        store = MonitorStore(tmp_path)
        store.add(_mk("m"))
        original = store.get("m")
        assert original is not None
        stopped = MonitorDef(
            name="m",
            kind=original.kind,
            spec=original.spec,
            state="stopped",
            session_id=original.session_id,
            created_at=original.created_at,
        )
        store.replace(stopped)
        assert store.get("m").state == "stopped"
        removed = store.remove("m")
        assert removed.name == "m"
        assert store.get("m") is None
        with pytest.raises(KeyError):
            store.remove("m")
        with pytest.raises(KeyError):
            store.replace(_mk("ghost"))

    def test_mark_fired_persists(self, tmp_path: Path) -> None:
        store = MonitorStore(tmp_path)
        store.add(_mk("m"))
        assert store.mark_fired("m") is not None
        reloaded = MonitorStore(tmp_path)
        m = reloaded.get("m")
        assert m is not None
        assert m.fire_count == 1
        assert m.last_fired_at is not None

    def test_mark_fired_unknown_is_none(self, tmp_path: Path) -> None:
        store = MonitorStore(tmp_path)
        assert store.mark_fired("ghost") is None

    def test_corrupt_registry_starts_empty(self, tmp_path: Path) -> None:
        (tmp_path / "monitors.json").write_text("{not json", encoding="utf-8")
        store = MonitorStore(tmp_path)
        assert store.list() == []

    def test_registry_non_object_starts_empty(self, tmp_path: Path) -> None:
        (tmp_path / "monitors.json").write_text("[1, 2]", encoding="utf-8")
        store = MonitorStore(tmp_path)
        assert store.list() == []

    def test_stale_tmp_cleaned(self, tmp_path: Path) -> None:
        stale = tmp_path / "monitors.json.tmp.99999"
        stale.write_text("{}", encoding="utf-8")
        MonitorStore(tmp_path)
        assert not stale.exists()

    def test_crash_mid_write_leaves_valid_registry(self, tmp_path: Path) -> None:
        store = MonitorStore(tmp_path)
        store.add(_mk("m"))
        # Simulate a torn write: tmp exists, target is still the old file.
        tmp_file = tmp_path / "monitors.json.tmp.12345"
        tmp_file.write_text("{torn", encoding="utf-8")
        reloaded = MonitorStore(tmp_path)  # must survive + clean the orphan
        assert reloaded.get("m") is not None
        assert not tmp_file.exists()


# ── WakeQueue ──────────────────────────────────────────────────────────────────


class TestWakeQueue:
    def test_append_and_pending(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path)
        e = q.append(WakeEvent.create("m", "interval", "s1", {"a": 1}))
        assert e is not None
        assert q.pending() == [e]
        assert q.pending("s1") == [e]
        assert q.pending("other") == []
        assert q.pending_count() == 1

    def test_dedupe_identical_pending(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path)
        first = q.append(WakeEvent.create("m", "interval", "s1", {"a": 1}))
        assert first is not None
        assert q.append(WakeEvent.create("m", "interval", "s1", {"a": 1})) is None
        assert q.pending_count() == 1

    def test_per_monitor_cap_drops_oldest(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path, max_pending_per_monitor=2)
        for i in range(3):
            q.append(WakeEvent.create("m", "interval", "s1", {"i": i}))
        pending = q.pending()
        assert len(pending) == 2
        assert pending[0].payload == {"i": 1}
        assert pending[1].payload == {"i": 2, "dropped_previous": 1}
        # Other monitors are unaffected by the cap.
        q.append(WakeEvent.create("other", "file", "s1", {}))
        assert q.pending_count() == 3

    def test_cap_survives_reload(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path, max_pending_per_monitor=2)
        q.append(WakeEvent.create("m", "interval", "s1", {"i": 0}))
        q.append(WakeEvent.create("m", "interval", "s1", {"i": 1}))
        q.append(WakeEvent.create("m", "interval", "s1", {"i": 2}))
        reloaded = WakeQueue(tmp_path, max_pending_per_monitor=2)
        pending = reloaded.pending()
        assert len(pending) == 2
        # The dropped event must NOT have been resurrected from disk.
        assert all(e.payload.get("i") != 0 for e in pending)

    def test_consume_specific_ids(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path)
        a = q.append(WakeEvent.create("m", "interval", "s1", {}))
        b = q.append(WakeEvent.create("m", "interval", "s1", {"x": 1}))
        consumed = q.consume([b.id])
        assert [e.id for e in consumed] == [b.id]
        assert [e.id for e in q.pending()] == [a.id]

    def test_consume_all_and_persist(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path)
        q.append(WakeEvent.create("m", "interval", "s1", {}))
        consumed = q.consume()
        assert len(consumed) == 1
        assert q.pending() == []
        # The consumed event is gone from disk, not just memory.
        reloaded = WakeQueue(tmp_path)
        assert reloaded.pending() == []

    def test_reload_lenient_to_truncated_tail(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path)
        q.append(WakeEvent.create("m", "interval", "s1", {}))
        # Simulate a crash mid-rewrite: truncated last line.
        path = tmp_path / "wake.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(lines[0] + "\n{'broken", encoding="utf-8")
        reloaded = WakeQueue(tmp_path)
        assert reloaded.pending_count() == 1  # valid line kept, broken skipped

    def test_wake_event_round_trip(self, tmp_path: Path) -> None:
        q = WakeQueue(tmp_path)
        q.append(WakeEvent.create("m", "file", "s9", {"changed": ["a.log"]}))
        reloaded = WakeQueue(tmp_path)
        e = reloaded.pending()[0]
        assert e.monitor == "m"
        assert e.kind == "file"
        assert e.session_id == "s9"
        assert e.payload == {"changed": ["a.log"]}
        assert e.consumed is False

    def test_malformed_payload_becomes_empty(self, tmp_path: Path) -> None:
        (tmp_path / "wake.jsonl").write_text(
            json.dumps(
                {
                    "id": "x1",
                    "monitor": "m",
                    "kind": "file",
                    "session_id": "s",
                    "fired_at": "2026-01-01T00:00:00+00:00",
                    "payload": "oops",
                    "consumed": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        q = WakeQueue(tmp_path)
        assert q.pending()[0].payload == {}
