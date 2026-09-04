"""
Unit tests for the plugin system.
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from phoson_agent import (
    Plugin,
    AgentTool,
    PluginSpec,
    AgentEngine,
    CliCommandSpec,
    PluginRegistry,
    ThemeExtension,
    ToolRenderSpec,
    AgentMiddleware,
    PhosonPluginLoadError,
    PhosonPluginConfigError,
    PhosonPluginCleanupError,
    tool,
    load_plugin,
)


class DummyPlugin(Plugin):
    """Test plugin for unit tests."""

    def __init__(self):
        self.configured = False
        self.initialized = False
        self.cleaned_up = False
        self.config_data = {}

    @property
    def name(self) -> str:
        return "test-plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    def configure(self, config: dict) -> None:
        self.configured = True
        self.config_data = config

    def initialize(self) -> None:
        self.initialized = True

    def cleanup(self) -> None:
        self.cleaned_up = True

    def get_tools(self) -> list[AgentTool]:
        @tool
        def test_tool(x: int) -> int:
            """Test tool."""
            return x * 2

        return [test_tool]

    def get_middlewares(self) -> list[AgentMiddleware]:
        class TestMiddleware(AgentMiddleware):
            pass

        return [TestMiddleware()]


class TestPluginCliExtensionDefaults:
    """The community-CLI hooks remain optional for existing plugins (I-110)."""

    async def test_aclose_default_delegates_to_cleanup_once(self):
        plugin = DummyPlugin()

        await plugin.aclose()

        assert plugin.cleaned_up is True

    def test_cli_extension_hooks_default_to_empty(self):
        plugin = DummyPlugin()

        assert plugin.get_commands() == []
        assert plugin.get_tool_render_specs() == []
        assert plugin.get_theme_extension() is None

    def test_plugin_can_expose_neutral_cli_extension_specs(self):
        class CliPlugin(Plugin):
            @property
            def name(self) -> str:
                return "cli-plugin"

            def get_commands(self) -> list[CliCommandSpec]:
                return [
                    CliCommandSpec(
                        names=("/plugin-status",),
                        help="Show plugin status",
                        handler="handle_status",
                    )
                ]

            def get_tool_render_specs(self) -> list[ToolRenderSpec]:
                return [
                    ToolRenderSpec(
                        tool_name="plugin_status",
                        verb="checking status",
                        icon="◌",
                    )
                ]

            def get_theme_extension(self) -> ThemeExtension:
                return ThemeExtension(
                    name="plugin-night",
                    description="Theme from the plugin",
                    tokens={"accent": "cyan"},
                )

        plugin = CliPlugin()

        assert plugin.get_commands()[0].primary == "/plugin-status"
        assert plugin.get_tool_render_specs()[0].icon == "◌"
        assert plugin.get_theme_extension() is not None
        assert plugin.get_theme_extension().name == "plugin-night"  # type: ignore[union-attr]


class TestPluginSpec:
    """Tests for PluginSpec."""

    def test_from_string(self):
        spec = PluginSpec.from_value("my-plugin")
        assert spec.name == "my-plugin"
        assert spec.config == {}
        assert spec.instance is None

    def test_from_dict(self):
        spec = PluginSpec.from_value({"name": "my-plugin", "config": {"key": "value"}})
        assert spec.name == "my-plugin"
        assert spec.config == {"key": "value"}

    def test_from_dict_without_config(self):
        spec = PluginSpec.from_value({"name": "my-plugin"})
        assert spec.name == "my-plugin"
        assert spec.config == {}

    def test_from_plugin_instance(self):
        plugin = DummyPlugin()
        spec = PluginSpec.from_value(plugin)
        assert spec.name == "test-plugin"
        assert spec.instance is plugin

    def test_from_invalid_type(self):
        with pytest.raises(TypeError):
            PluginSpec.from_value(123)  # type: ignore

    def test_from_dict_without_name(self):
        with pytest.raises(PhosonPluginConfigError):
            PluginSpec.from_value({"config": {}})


class TestPluginRegistry:
    """Tests for PluginRegistry."""

    def test_load_plugin_instance(self):
        registry = PluginRegistry()
        plugin = DummyPlugin()
        spec = PluginSpec.from_value(plugin)

        loaded = registry.load(spec)
        assert loaded is plugin
        assert loaded.configured  # Called even for instances
        assert loaded.initialized  # Called even for instances

    def test_custom_loader(self):
        registry = PluginRegistry()

        def custom_loader(name: str) -> Plugin:
            return DummyPlugin()

        registry.register_loader("custom", custom_loader)

        spec = PluginSpec.from_value("custom:my-plugin")
        loaded = registry.load(spec)

        assert isinstance(loaded, DummyPlugin)
        assert loaded.configured
        assert loaded.initialized

    def test_unknown_loader(self):
        registry = PluginRegistry()
        spec = PluginSpec.from_value("unknown:my-plugin")

        with pytest.raises(PhosonPluginConfigError, match="Unknown plugin loader"):
            registry.load(spec)

    def test_configure_and_initialize(self):
        registry = PluginRegistry()

        def loader(name: str) -> Plugin:
            return DummyPlugin()

        registry.register_loader("test", loader)

        spec = PluginSpec.from_value(
            {"name": "test:my-plugin", "config": {"key": "value"}}
        )
        loaded = registry.load(spec)

        assert loaded.configured
        assert loaded.config_data == {"key": "value"}
        assert loaded.initialized


class TestAgentEngineWithPlugins:
    """Tests for AgentEngine plugin integration."""

    def test_load_plugins_on_init(self):
        plugin = DummyPlugin()
        engine = AgentEngine(
            chat=Mock(),
            plugins=[plugin],
        )

        assert len(engine._loaded_plugins) == 1
        assert engine._loaded_plugins[0] is plugin

    def test_plugins_provide_tools(self):
        plugin = DummyPlugin()
        engine = AgentEngine(
            chat=Mock(),
            plugins=[plugin],
        )

        # Plugin should provide 1 tool
        assert len(engine.tools) == 1
        assert engine.tools[0].name == "test_tool"

    def test_plugins_provide_middlewares(self):
        plugin = DummyPlugin()
        engine = AgentEngine(
            chat=Mock(),
            plugins=[plugin],
        )

        # Plugin should provide 1 middleware
        assert len(engine.middlewares) == 1

    def test_multiple_plugins(self):
        plugin1 = DummyPlugin()
        plugin2 = DummyPlugin()

        engine = AgentEngine(
            chat=Mock(),
            plugins=[plugin1, plugin2],
        )

        assert len(engine._loaded_plugins) == 2
        assert len(engine.tools) == 2  # Each provides 1 tool
        assert len(engine.middlewares) == 2  # Each provides 1 middleware

    def test_cleanup(self):
        plugin = DummyPlugin()
        engine = AgentEngine(
            chat=Mock(),
            plugins=[plugin],
        )

        assert not plugin.cleaned_up
        engine.cleanup()
        assert plugin.cleaned_up

    def test_context_manager(self):
        plugin = DummyPlugin()

        with AgentEngine(chat=Mock(), plugins=[plugin]) as _:
            assert not plugin.cleaned_up

        assert plugin.cleaned_up

    def test_mix_plugins_and_tools(self):
        plugin = DummyPlugin()

        @tool
        def custom_tool(x: str) -> str:
            """Custom tool."""
            return x.upper()

        engine = AgentEngine(
            chat=Mock(),
            tools=[custom_tool],
            plugins=[plugin],
        )

        # Should have both custom tool and plugin tool
        assert len(engine.tools) == 2
        tool_names = {t.name for t in engine.tools}
        assert "custom_tool" in tool_names
        assert "test_tool" in tool_names


class TestPluginLifecycle:
    """Tests for plugin lifecycle."""

    def test_lifecycle_order(self):
        events = []

        class LifecyclePlugin(Plugin):
            @property
            def name(self) -> str:
                return "lifecycle"

            def configure(self, config: dict) -> None:
                events.append("configure")

            def initialize(self) -> None:
                events.append("initialize")

            def cleanup(self) -> None:
                events.append("cleanup")

        plugin = LifecyclePlugin()
        engine = AgentEngine(chat=Mock(), plugins=[plugin])

        assert events == ["configure", "initialize"]

        engine.cleanup()
        assert events == ["configure", "initialize", "cleanup"]

    def test_cleanup_propagates_errors(self):
        """Cleanup raises PhosonPluginCleanupError when plugins fail.

        Library users may want to know about cleanup failures, so we propagate
        them via a typed exception that carries every failure.
        """

        class BrokenPlugin(Plugin):
            @property
            def name(self) -> str:
                return "broken"

            def cleanup(self) -> None:
                raise RuntimeError("Cleanup failed!")

        plugin = BrokenPlugin()
        engine = AgentEngine(chat=Mock(), plugins=[plugin])

        with pytest.raises(PhosonPluginCleanupError) as exc_info:
            engine.cleanup()

        assert len(exc_info.value.failures) == 1
        name, error = exc_info.value.failures[0]
        assert name == "broken"
        assert isinstance(error, RuntimeError)

    def test_context_manager_suppresses_cleanup_errors(self):
        """The context manager protocol swallows cleanup failures.

        Use cleanup() explicitly when you need to react to failures.
        """

        class BrokenPlugin(Plugin):
            @property
            def name(self) -> str:
                return "broken"

            def cleanup(self) -> None:
                raise RuntimeError("Cleanup failed!")

        with AgentEngine(chat=Mock(), plugins=[BrokenPlugin()]):
            pass  # Should not raise on exit


class TestPathPluginLoader:
    """Tests for the ``path:`` loader — sys.path hygiene (issue #26).

    The loader must not leave any trace in ``sys.path`` after loading,
    even when the parent directory was already present or the load fails.
    """

    PLUGIN_SOURCE = """\
from phoson_agent import Plugin


class PathPlugin(Plugin):
    def __init__(self) -> None:
        self.initialized = False

    @property
    def name(self) -> str:
        return "path-plugin"

    def initialize(self) -> None:
        self.initialized = True


plugin = PathPlugin()
"""

    def _write_plugin(self, directory: Path, body: str | None = None) -> Path:
        file_path = directory / "my_path_plugin.py"
        file_path.write_text(body if body is not None else self.PLUGIN_SOURCE)
        return file_path

    def test_load_from_path(self, tmp_path: Path):
        file_path = self._write_plugin(tmp_path)
        plugin = load_plugin(f"path:{file_path}")
        assert plugin.name == "path-plugin"
        assert plugin.initialized is True

    def test_sys_path_not_mutated_after_load(self, tmp_path: Path):
        file_path = self._write_plugin(tmp_path)
        before = list(sys.path)
        load_plugin(f"path:{file_path}")
        assert sys.path == before

    def test_sys_path_exact_restore_when_parent_already_present(self, tmp_path: Path):
        # Parent directory already on sys.path: the guard must restore the
        # exact list (no duplicate entries, original order preserved).
        sys.path.insert(0, str(tmp_path))
        try:
            file_path = self._write_plugin(tmp_path)
            before = list(sys.path)
            load_plugin(f"path:{file_path}")
            assert sys.path == before
        finally:
            sys.path.remove(str(tmp_path))

    def test_sys_path_restored_when_load_fails(self, tmp_path: Path):
        file_path = self._write_plugin(tmp_path, body="VALUE = 42\n")
        before = list(sys.path)
        with pytest.raises(PhosonPluginLoadError):
            load_plugin(f"path:{file_path}")
        assert sys.path == before

    def test_load_same_path_twice(self, tmp_path: Path):
        file_path = self._write_plugin(tmp_path)
        first = load_plugin(f"path:{file_path}")
        second = load_plugin(f"path:{file_path}")
        assert first.name == "path-plugin"
        assert second.name == "path-plugin"

    def test_sibling_imports_resolve_during_load(self, tmp_path: Path):
        # The guard exists so sibling modules next to the plugin file can be
        # imported while it executes. Verify that still works.
        (tmp_path / "sibling_helper.py").write_text("MARKER = 'from-sibling'\n")
        plugin_file = self._write_plugin(
            tmp_path,
            body=(
                "from phoson_agent import Plugin\n"
                "from sibling_helper import MARKER\n"
                "\n"
                "\n"
                "class PathPlugin(Plugin):\n"
                "    @property\n"
                "    def name(self) -> str:\n"
                "        return f'path-plugin-{MARKER}'\n"
                "\n"
                "plugin = PathPlugin()\n"
            ),
        )
        before = list(sys.path)
        plugin = load_plugin(f"path:{plugin_file}")
        assert plugin.name == "path-plugin-from-sibling"
        # And no sys.path residue afterwards.
        assert sys.path == before


class TestAgentEngineBadPluginGracefulDegradation:
    """A single unloadable plugin spec must not brick the engine (report).

    The user's config can legitimately carry a stale spec (an entry point
    that was never installed, or a ``path:`` spec whose file was deleted).
    Loading it must warn and skip, and the *other* configured plugins must
    still load — instead of raising and taking the whole CLI down.
    """

    def test_unloadable_entrypoint_is_skipped_without_crashing(self):
        good = DummyPlugin()
        engine = AgentEngine(
            chat=Mock(),
            plugins=["entrypoint:does-not-exist-12345", good],
        )
        # The bad spec is dropped; the good plugin still loads.
        assert engine._loaded_plugins == [good]
        assert engine.tools and engine.tools[0].name == "test_tool"

    def test_unloadable_path_spec_is_skipped(self):
        good = DummyPlugin()
        engine = AgentEngine(
            chat=Mock(),
            plugins=["path:/nonexistent/nowhere/plugin.py", good],
        )
        assert engine._loaded_plugins == [good]
        assert len(engine.middlewares) == 1

    def test_only_bad_spec_yields_an_engine_with_no_plugins(self):
        # Even with *every* spec bad, the engine still constructs.
        engine = AgentEngine(
            chat=Mock(),
            plugins=["entrypoint:nope-1", "path:/no/such/file.py"],
        )
        assert engine._loaded_plugins == []
        assert engine.tools == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
