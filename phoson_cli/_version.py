"""Import-light version lookup shared by the CLI and update checks."""

PACKAGE = "phoson-engine-minimal"


def get_current_version() -> str:
    """Installed version, build-injected frozen version, or ``dev``."""
    from importlib.metadata import PackageNotFoundError, version

    from phoson_cli._frozen import is_frozen, frozen_version

    try:
        current = version(PACKAGE)
    except PackageNotFoundError:
        current = "dev"
    if is_frozen():
        return frozen_version(current)
    return current
