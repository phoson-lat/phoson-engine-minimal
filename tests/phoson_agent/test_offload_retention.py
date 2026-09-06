"""Tests for F-51 — offload retention (TTL + quota) on the offload dir.

Covers :class:`~phoson_agent.plugins.offload.RetentionPolicy`,
:func:`~phoson_agent.plugins.offload.cleanup_offload_dir`, the
middleware's every-N-writes trigger, and the CLI config wiring
(``PHOSON_OFFLOAD_TTL_DAYS`` / ``PHOSON_OFFLOAD_MAX_MB``).
"""

import os
import time
from pathlib import Path

import pytest

from phoson_cli.config import PhosonConfig, load_config
from phoson_llm.schemas import ToolCallEvent
from phoson_cli.session_utils import build_offload
from phoson_agent.plugins.offload import (
    DEFAULT_MAX_CHARS,
    DEFAULT_RETENTION,
    RetentionPolicy,
    OffloadMiddleware,
    cleanup_offload_dir,
)


def _touch(path: Path, mtime: float, size: int = 1) -> None:
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))


def _call(tool_call_id: str) -> ToolCallEvent:
    return ToolCallEvent(tool_call_id=tool_call_id, tool_name="bash", args={})


class TestCleanupOffloadDir:
    def test_ttl_deletes_old_files_only(self, tmp_path: Path) -> None:
        now = time.time()
        old = tmp_path / "old.txt"
        fresh = tmp_path / "fresh.txt"
        _touch(old, now - 8 * 86_400)
        _touch(fresh, now - 1 * 86_400)

        deleted = cleanup_offload_dir(
            tmp_path, RetentionPolicy(max_age_days=7, max_total_mb=0)
        )

        assert deleted == 1
        assert not old.exists()
        assert fresh.exists()

    def test_ttl_zero_disables_age_cleanup(self, tmp_path: Path) -> None:
        now = time.time()
        ancient = tmp_path / "ancient.txt"
        _touch(ancient, now - 100 * 86_400)

        deleted = cleanup_offload_dir(
            tmp_path, RetentionPolicy(max_age_days=0, max_total_mb=0)
        )

        assert deleted == 0
        assert ancient.exists()

    def test_quota_deletes_oldest_first_until_under(self, tmp_path: Path) -> None:
        now = time.time()
        # 4 files of 100 MB each = 400 MB; quota 250 MB → keep the two
        # newest (200 MB), delete the two oldest.
        paths = []
        for i in range(4):
            p = tmp_path / f"f{i}.txt"
            _touch(p, now - (4 - i) * 86_400, size=100 * 1024 * 1024)
            paths.append(p)

        deleted = cleanup_offload_dir(
            tmp_path, RetentionPolicy(max_age_days=0, max_total_mb=250)
        )

        assert deleted == 2
        assert not paths[0].exists()
        assert not paths[1].exists()
        assert paths[2].exists()
        assert paths[3].exists()

    def test_quota_zero_disables_size_cleanup(self, tmp_path: Path) -> None:
        now = time.time()
        big = tmp_path / "big.txt"
        _touch(big, now, size=10 * 1024 * 1024)

        deleted = cleanup_offload_dir(
            tmp_path, RetentionPolicy(max_age_days=0, max_total_mb=0)
        )

        assert deleted == 0
        assert big.exists()

    def test_ttl_and_quota_combined(self, tmp_path: Path) -> None:
        now = time.time()
        # One file is too old (TTL) and the two fresh ones exceed the
        # quota (2 × 100 MB > 150 MB) → oldest of the fresh ones goes too.
        old = tmp_path / "old.txt"
        mid = tmp_path / "mid.txt"
        new = tmp_path / "new.txt"
        _touch(old, now - 30 * 86_400, size=100 * 1024 * 1024)
        _touch(mid, now - 2 * 86_400, size=100 * 1024 * 1024)
        _touch(new, now - 1 * 86_400, size=100 * 1024 * 1024)

        deleted = cleanup_offload_dir(
            tmp_path, RetentionPolicy(max_age_days=7, max_total_mb=150)
        )

        assert deleted == 2
        assert not old.exists()
        assert not mid.exists()
        assert new.exists()

    def test_empty_directory_returns_zero(self, tmp_path: Path) -> None:
        assert cleanup_offload_dir(tmp_path, DEFAULT_RETENTION) == 0

    def test_missing_directory_returns_zero(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert cleanup_offload_dir(missing, DEFAULT_RETENTION) == 0

    def test_subdirectories_are_never_touched(self, tmp_path: Path) -> None:
        now = time.time()
        sub = tmp_path / "nested"
        sub.mkdir()
        nested_file = sub / "inner.txt"
        _touch(nested_file, now - 30 * 86_400)

        deleted = cleanup_offload_dir(
            tmp_path, RetentionPolicy(max_age_days=7, max_total_mb=0)
        )

        assert deleted == 0
        assert nested_file.exists()

    def test_unlink_failure_is_swallowed(self, tmp_path: Path) -> None:
        now = time.time()
        victim = tmp_path / "victim.txt"
        _touch(victim, now - 30 * 86_400)

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError("permission denied")

        original = Path.unlink
        try:
            Path.unlink = _raise  # type: ignore[method-assign]
            deleted = cleanup_offload_dir(
                tmp_path, RetentionPolicy(max_age_days=7, max_total_mb=0)
            )
        finally:
            Path.unlink = original  # type: ignore[method-assign]

        # Best-effort: the failure is swallowed, nothing is counted.
        assert deleted == 0
        assert victim.exists()


class TestRetentionPolicyDefaults:
    def test_default_values(self) -> None:
        assert DEFAULT_RETENTION.max_age_days == 7
        assert DEFAULT_RETENTION.max_total_mb == 500.0
        assert DEFAULT_RETENTION.check_every_n_writes == 50

    def test_middleware_resolves_none_to_default(self, tmp_path: Path) -> None:
        mw = OffloadMiddleware(output_dir=tmp_path)
        assert mw._retention is DEFAULT_RETENTION
        assert mw._write_count == 0

    def test_middleware_keeps_explicit_policy(self, tmp_path: Path) -> None:
        policy = RetentionPolicy(max_age_days=1, max_total_mb=10.0)
        mw = OffloadMiddleware(output_dir=tmp_path, retention=policy)
        assert mw._retention is policy


class TestMiddlewareCleanupTrigger:
    async def _offload_once(self, mw: OffloadMiddleware, call_id: str) -> None:
        # A result larger than max_chars guarantees a real offload write.
        await mw.on_after_tool(_call(call_id), "Z" * 5_000, False)

    async def test_cleanup_runs_every_n_writes(self, tmp_path: Path) -> None:
        n = 3
        mw = OffloadMiddleware(
            max_chars=100,
            head_chars=20,
            tail_chars=10,
            output_dir=tmp_path,
            retention=RetentionPolicy(
                max_age_days=0, max_total_mb=0, check_every_n_writes=n
            ),
        )
        calls = {"count": 0}
        real_cleanup = cleanup_offload_dir

        def _spy(*args: object, **kwargs: object) -> int:
            calls["count"] += 1
            return real_cleanup(*args, **kwargs)  # type: ignore[arg-type]

        import phoson_agent.plugins.offload as offload_mod

        offload_mod.cleanup_offload_dir = _spy  # type: ignore[method-assign]
        try:
            for i in range(n):
                await self._offload_once(mw, f"c{i}")
        finally:
            offload_mod.cleanup_offload_dir = real_cleanup  # type: ignore[method-assign]

        assert calls["count"] == 1
        assert mw._write_count == n

    async def test_cleanup_not_run_before_n_writes(self, tmp_path: Path) -> None:
        mw = OffloadMiddleware(
            max_chars=100,
            head_chars=20,
            tail_chars=10,
            output_dir=tmp_path,
            retention=RetentionPolicy(
                max_age_days=0, max_total_mb=0, check_every_n_writes=5
            ),
        )
        calls = {"count": 0}
        real_cleanup = cleanup_offload_dir

        def _spy(*args: object, **kwargs: object) -> int:
            calls["count"] += 1
            return real_cleanup(*args, **kwargs)  # type: ignore[arg-type]

        import phoson_agent.plugins.offload as offload_mod

        offload_mod.cleanup_offload_dir = _spy  # type: ignore[method-assign]
        try:
            for i in range(4):
                await self._offload_once(mw, f"c{i}")
        finally:
            offload_mod.cleanup_offload_dir = real_cleanup  # type: ignore[method-assign]

        assert calls["count"] == 0
        assert mw._write_count == 4

    async def test_small_results_do_not_count_as_writes(self, tmp_path: Path) -> None:
        mw = OffloadMiddleware(
            max_chars=DEFAULT_MAX_CHARS,
            output_dir=tmp_path,
            retention=RetentionPolicy(
                max_age_days=0, max_total_mb=0, check_every_n_writes=1
            ),
        )
        calls = {"count": 0}
        real_cleanup = cleanup_offload_dir

        def _spy(*args: object, **kwargs: object) -> int:
            calls["count"] += 1
            return real_cleanup(*args, **kwargs)  # type: ignore[arg-type]

        import phoson_agent.plugins.offload as offload_mod

        offload_mod.cleanup_offload_dir = _spy  # type: ignore[method-assign]
        try:
            for i in range(3):
                await mw.on_after_tool(_call(f"s{i}"), "tiny", False)
        finally:
            offload_mod.cleanup_offload_dir = real_cleanup  # type: ignore[method-assign]

        assert calls["count"] == 0
        assert mw._write_count == 0

    async def test_check_every_n_writes_zero_disables_trigger(
        self, tmp_path: Path
    ) -> None:
        mw = OffloadMiddleware(
            max_chars=100,
            head_chars=20,
            tail_chars=10,
            output_dir=tmp_path,
            retention=RetentionPolicy(
                max_age_days=0, max_total_mb=0, check_every_n_writes=0
            ),
        )
        calls = {"count": 0}
        real_cleanup = cleanup_offload_dir

        def _spy(*args: object, **kwargs: object) -> int:
            calls["count"] += 1
            return real_cleanup(*args, **kwargs)  # type: ignore[arg-type]

        import phoson_agent.plugins.offload as offload_mod

        offload_mod.cleanup_offload_dir = _spy  # type: ignore[method-assign]
        try:
            for i in range(5):
                await self._offload_once(mw, f"c{i}")
        finally:
            offload_mod.cleanup_offload_dir = real_cleanup  # type: ignore[method-assign]

        assert calls["count"] == 0
        assert mw._write_count == 5


class TestConfigWiring:
    def _isolate_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        return home

    def test_defaults(self) -> None:
        cfg = PhosonConfig()
        assert cfg.offload_ttl_days == 7
        assert cfg.offload_max_mb == 500

    def test_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("PHOSON_OFFLOAD_TTL_DAYS", "3")
        monkeypatch.setenv("PHOSON_OFFLOAD_MAX_MB", "128")

        cfg = load_config()

        assert cfg.offload_ttl_days == 3
        assert cfg.offload_max_mb == 128

    def test_file_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = self._isolate_home(monkeypatch, tmp_path)
        (home / ".phoson").mkdir()
        (home / ".phoson" / "config.toml").write_text(
            "[defaults]\noffload_ttl_days = 14\noffload_max_mb = 256\n",
            encoding="utf-8",
        )

        cfg = load_config()

        assert cfg.offload_ttl_days == 14
        assert cfg.offload_max_mb == 256

    def test_env_beats_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = self._isolate_home(monkeypatch, tmp_path)
        (home / ".phoson").mkdir()
        (home / ".phoson" / "config.toml").write_text(
            "[defaults]\noffload_ttl_days = 14\n", encoding="utf-8"
        )
        monkeypatch.setenv("PHOSON_OFFLOAD_TTL_DAYS", "2")

        cfg = load_config()

        assert cfg.offload_ttl_days == 2

    def test_build_offload_projects_retention(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("PHOSON_OFFLOAD_TTL_DAYS", "5")
        monkeypatch.setenv("PHOSON_OFFLOAD_MAX_MB", "100")
        cfg = load_config()

        mw = build_offload(cfg)

        assert isinstance(mw._retention, RetentionPolicy)
        assert mw._retention.max_age_days == 5
        assert mw._retention.max_total_mb == 100.0
        assert mw.output_dir == cfg.compacted_dir

    def test_zero_knobs_disable_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("PHOSON_OFFLOAD_TTL_DAYS", "0")
        monkeypatch.setenv("PHOSON_OFFLOAD_MAX_MB", "0")
        cfg = load_config()

        mw = build_offload(cfg)

        assert mw._retention.max_age_days == 0
        assert mw._retention.max_total_mb == 0.0
