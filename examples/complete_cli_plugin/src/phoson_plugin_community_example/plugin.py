"""One Plugin instance contributes an engine tool, CLI command, look and UI."""

from typing import Annotated

from phoson_agent import (
    Plugin,
    TodoItem,
    ProgressBlock,
    TodoListBlock,
    CliCommandSpec,
    ThemeExtension,
    ToolRenderSpec,
    CliCommandContext,
    CliCommandInvocation,
    tool,
)


class CommunityExamplePlugin(Plugin):
    @property
    def name(self) -> str:
        return "community-example"

    @property
    def description(self) -> str:
        return "A complete plugin-platform example."

    def get_tools(self):
        @tool(inject=["plugin_ui"])
        async def example_checklist(
            task: Annotated[str, "A task to add to the example checklist"],
            *,
            plugin_ui=None,
        ) -> str:
            """Publish a small checklist card from a community plugin."""
            if plugin_ui is None:
                return "Plugin UI is unavailable in this host."
            plugin_ui.replace(
                "checklist",
                TodoListBlock(
                    id="checklist",
                    title="Community example",
                    items=(
                        TodoItem(id="plugin", title="Plugin loaded", completed=True),
                        TodoItem(id="task", title=task),
                    ),
                ),
            )
            plugin_ui.replace(
                "progress", ProgressBlock("progress", "Checklist ready", 2, 2)
            )
            return "Published the community-example checklist."

        return [example_checklist]

    def get_commands(self) -> list[CliCommandSpec]:
        return [
            CliCommandSpec(
                names=("/example-status",),
                help="Show the community plugin example status",
                handler="handle_status",
                category="Plugins",
            )
        ]

    async def handle_status(
        self, command: CliCommandInvocation, context: CliCommandContext
    ) -> bool:
        context.notify("info", f"community-example active in {context.cwd}")
        context.ui.replace(
            "status",
            ProgressBlock("status", "Example plugin ready", 1, 1),
        )
        return True

    def get_tool_render_specs(self) -> list[ToolRenderSpec]:
        return [
            ToolRenderSpec(
                tool_name="example_checklist",
                verb="updating checklist",
                icon="☑",
            )
        ]

    def get_theme_extension(self) -> ThemeExtension:
        return ThemeExtension(
            name="example-neon",
            description="cyan community example theme",
            tokens={"accent": "cyan", "pt_accent": "cyan"},
        )

    async def aclose(self) -> None:
        # Close async resources here if this plugin owns any.
        return None


def create_plugin() -> CommunityExamplePlugin:
    return CommunityExamplePlugin()
