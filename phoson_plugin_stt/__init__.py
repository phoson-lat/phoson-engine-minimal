"""Phoson multilingual speech-to-text plugin (Moonshine).

Contributes push-to-talk dictation (bound to ``Ctrl+O`` by the CLI) and the
``transcribe_audio`` agent tool. See :mod:`phoson_plugin_stt._plugin` for the
plugin and :mod:`phoson_plugin_stt.engine` for the Moonshine-backed engine.
"""

from .engine import (
    LANGUAGES,
    SttEngine,
    SttUnavailable,
    parse_model_arch,
    normalize_language,
)
from ._plugin import SttPlugin, create_plugin

__version__ = "0.1.0"

# Export a module-level instance under the conventional name the package
# loader looks for (``phoson-plugin-stt`` -> ``plugin``).
plugin = SttPlugin()

__all__ = [
    "LANGUAGES",
    "SttEngine",
    "SttPlugin",
    "SttUnavailable",
    "create_plugin",
    "normalize_language",
    "parse_model_arch",
    "plugin",
]
