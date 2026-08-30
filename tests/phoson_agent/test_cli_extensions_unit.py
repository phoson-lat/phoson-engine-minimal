"""Unit tests for the UI-neutral public plugin extension contracts (I-110)."""

import inspect

from phoson_agent.cli_extensions import (
    Choice,
    TodoItem,
    FormField,
    NoticeBlock,
    KeyValueBlock,
    ProgressBlock,
    TodoListBlock,
    CliCommandSpec,
    ThemeExtension,
    ToolRenderSpec,
    InteractionResult,
    CliCommandInvocation,
)


def test_extension_contract_module_has_no_cli_or_ui_toolkit_dependencies() -> None:
    """The engine-level API must remain usable by non-CLI plugin hosts."""
    import phoson_agent.cli_extensions as extensions

    source = inspect.getsource(extensions)

    # The explanatory docstrings deliberately mention the forbidden packages;
    # imports, rather than prose, determine the module's dependency surface.
    assert "import phoson_cli" not in source
    assert "from phoson_cli" not in source
    assert "import rich" not in source
    assert "from rich" not in source
    assert "import prompt_toolkit" not in source
    assert "from prompt_toolkit" not in source


def test_command_spec_and_invocation_are_immutable_declarative_data() -> None:
    spec = CliCommandSpec(
        names=("/example", "/ex"),
        help="Run the example",
        handler="handle_example",
    )
    invocation = CliCommandInvocation(name="/example", args="one two")

    assert spec.primary == "/example"
    assert spec.category == "Plugins"
    assert invocation.args == "one two"


def test_tool_render_and_theme_specs_keep_only_neutral_values() -> None:
    render = ToolRenderSpec(tool_name="example", verb="running example", icon="◌")
    theme = ThemeExtension(
        name="example-night",
        description="Example theme",
        tokens={"accent": "cyan"},
        extra_tokens={"example": "blue"},
    )

    assert render.detail_handler is None
    assert theme.base == "dark"
    assert theme.extra_tokens["example"] == "blue"


def test_ui_blocks_and_interaction_result_are_data_only() -> None:
    blocks = (
        NoticeBlock(id="notice", kind="info", message="Ready"),
        KeyValueBlock(id="details", title="Details", items=(("status", "ok"),)),
        TodoListBlock(
            id="todos",
            title="Tasks",
            items=(TodoItem(id="one", title="First", completed=True),),
        ),
        ProgressBlock(id="progress", label="Installing", completed=1, total=2),
    )
    result = InteractionResult(
        status="submitted",
        values={"choice": Choice(id="yes", label="Yes").id},
    )
    field = FormField(id="token", label="Token", kind="password")

    assert len(blocks) == 4
    assert blocks[2].items[0].completed is True
    assert result.values == {"choice": "yes"}
    assert field.required is True
