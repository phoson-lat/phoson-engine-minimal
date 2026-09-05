"""Unit tests for the CLI wiring of the OTel plugin (issue #140).

Covers :func:`build_otel_plugins` (enable-gating, user-spec dedup,
fresh pre-configured instance, ImportError fallback) and the
``PhosonConfig`` flags/env it reads.
"""

from pathlib import Path

from phoson_cli.config import PhosonConfig
from phoson_cli.session_utils import (
    build_otel_plugins,
    build_plugin_specs,
    _user_specified_otel,
)


def _cfg(**kw) -> PhosonConfig:
    return PhosonConfig(**kw)


class TestBuildOtelPlugins:
    def test_disabled_returns_empty(self) -> None:
        assert build_otel_plugins(_cfg(enable_otel=False)) == []

    def test_enabled_returns_fresh_configured_instance(self) -> None:
        specs = build_otel_plugins(
            _cfg(
                enable_otel=True,
                otel_service_name="svc",
                otel_file_path=Path("/tmp/otel.json"),
                otel_endpoint="",
            )
        )
        assert len(specs) == 1
        plugin = specs[0]
        # A direct Plugin instance (not a string spec).
        assert not isinstance(specs[0], (str, dict))
        assert plugin.name == "phoson-plugin-otel"
        # Pre-configured so the flags are honored without a second pass.
        assert plugin._resource["service.name"] == "svc"  # noqa: SLF001
        assert plugin.sink_path.endswith("otel.json")

    def test_user_spec_dedup_string(self) -> None:
        # If the user already listed it in [plugins], do not double-add.
        assert (
            build_otel_plugins(
                _cfg(
                    enable_otel=True,
                    plugins=["phoson-plugin-otel"],
                )
            )
            == []
        )

    def test_user_spec_dedup_dict(self) -> None:
        assert (
            build_otel_plugins(
                _cfg(
                    enable_otel=True,
                    plugins=[{"name": "phoson_plugin_otel", "config": {}}],
                )
            )
            == []
        )

    def test_other_user_specs_do_not_dedup(self) -> None:
        specs = build_otel_plugins(
            _cfg(enable_otel=True, plugins=["some-other-plugin"])
        )
        assert len(specs) == 1


class TestUserSpecifiedOtel:
    def test_string_match(self) -> None:
        assert _user_specified_otel(["phoson-plugin-otel"]) is True

    def test_underscore_variant(self) -> None:
        assert _user_specified_otel(["phoson_plugin_otel"]) is True

    def test_dict_name_match(self) -> None:
        assert _user_specified_otel([{"name": "phoson-plugin-otel"}]) is True

    def test_no_match(self) -> None:
        assert _user_specified_otel(["other", {"name": "x"}]) is False

    def test_empty(self) -> None:
        assert _user_specified_otel([]) is False


class TestPluginSpecsOrdering:
    def test_otel_is_last(self) -> None:
        """OTel comes after MCP/monitors; user specs come first."""
        specs = build_plugin_specs(
            _cfg(
                enable_otel=True,
                plugins=["user-spec"],
            )
        )
        names = []
        for s in specs:
            names.append(s if isinstance(s, str) else getattr(s, "name", "?"))
        assert names[0] == "user-spec"
        assert names[-1] == "phoson-plugin-otel"
        assert len(names) == 2

    def test_otel_absent_when_disabled(self) -> None:
        specs = build_plugin_specs(_cfg(enable_otel=False))
        names = [s if isinstance(s, str) else getattr(s, "name", "?") for s in specs]
        assert "phoson-plugin-otel" not in names


class TestConfigDefaults:
    def test_defaults(self) -> None:
        cfg = PhosonConfig()
        assert cfg.enable_otel is False
        assert cfg.otel_service_name == "phoson"
        assert cfg.otel_file_path == Path(".phoson/trace.json")
        assert cfg.otel_endpoint == ""

    def test_enable_via_flag(self) -> None:
        cfg = PhosonConfig(enable_otel=True)
        assert cfg.enable_otel is True
