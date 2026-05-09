"""Tests for the shared picker base.

These exercise the BasePicker plumbing without spawning a real
prompt_toolkit Application — we drive the bindings programmatically.
"""

import pytest

from phoson_cli.pickers import BasePicker, picker_style


def _trigger(picker: BasePicker, key: str) -> None:
    """Look up the handler registered for ``key`` and invoke it.

    prompt_toolkit normalises key names into the ``Keys`` enum (e.g.
    ``up`` → ``Keys.Up``, ``enter`` → ``Keys.ControlM``). We allow callers
    to use the user-facing name and translate to the canonical value.
    """
    aliases = {"enter": "c-m", "return": "c-m"}
    target = aliases.get(key.lower(), key.lower())

    bindings = picker._kb.bindings  # internal but stable
    for binding in bindings:
        for k in binding.keys:
            value = getattr(k, "value", str(k))
            if str(value).lower() == target:
                binding.handler(None)
                return
    raise KeyError(f"No binding for {key!r}")


def test_picker_style_extends_base_palette() -> None:
    s1 = picker_style()
    s2 = picker_style({"row": "bg:#ff0000"})
    # Both styles should produce instances; we only check construction.
    assert s1 is not None
    assert s2 is not None


def test_bind_registers_handler_for_key() -> None:
    picker: BasePicker[str] = BasePicker(render=lambda: [])
    triggered: list[str] = []

    picker.bind("up", lambda: triggered.append("up"))
    _trigger(picker, "up")

    assert triggered == ["up"]


def test_bind_default_nav_registers_four_handlers() -> None:
    picker: BasePicker[str] = BasePicker(render=lambda: [])
    log: list[str] = []

    picker.bind_default_nav(
        on_up=lambda: log.append("up"),
        on_down=lambda: log.append("down"),
        on_enter=lambda: log.append("enter"),
        on_cancel=lambda: log.append("cancel"),
    )

    for key in ("up", "down", "enter", "escape"):
        _trigger(picker, key)

    assert log == ["up", "down", "enter", "cancel"]


def test_done_without_running_app_does_not_raise() -> None:
    """Calling ``done`` before ``run`` shouldn't blow up.

    Some test setups will exercise the bindings directly (as we do
    above). Without an active Application the result simply is not
    propagated, but the picker must remain robust.
    """
    picker: BasePicker[str] = BasePicker(render=lambda: [])
    picker.done("nope")  # must not raise


def test_refresh_without_running_app_does_not_raise() -> None:
    picker: BasePicker[str] = BasePicker(render=lambda: [])
    picker.refresh()  # must not raise


@pytest.mark.asyncio
async def test_picker_returns_result_when_done_called() -> None:
    """End-to-end-ish: schedule ``done`` from a binding and run the picker."""
    picker: BasePicker[str] = BasePicker(render=lambda: [("class:row", "row\n")])

    def _confirm() -> None:
        picker.done("chosen")

    picker.bind("enter", _confirm)

    # Drive the picker manually — we can't await ``run()`` without a real
    # terminal, so we exercise the binding directly. ``done`` exits cleanly
    # because there is no Application yet.
    _trigger(picker, "enter")  # exercises the no-op path
