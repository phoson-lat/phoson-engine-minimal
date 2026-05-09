"""
Unit tests for the plugin system.
"""

from unittest.mock import Mock

import pytest

from phoson_agent import (
    Plugin,
    AgentTool,
    PluginSpec,
    AgentEngine,
    PluginRegistry,
    AgentMiddleware,
    PhosonPluginConfigError,
    PhosonPluginCleanupError,
    tool,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
