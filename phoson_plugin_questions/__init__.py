"""phoson_plugin_questions: an AskUserQuestion-style interactive tool.

Bundled plugin. Importing the package exposes the module-level ``plugin`` instance
(the loader's convention) plus the public symbols for embedding and testing.
"""

from ._plugin import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    MAX_QUESTIONS,
    Question,
    QuestionOption,
    QuestionsPlugin,
    QuestionsResult,
    create_plugin,
    build_questions_tool,
)

#: Module-level plugin instance (the package loader's convention).
plugin = QuestionsPlugin()

__all__ = [
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MIN_OPTIONS",
    "Question",
    "QuestionOption",
    "QuestionsPlugin",
    "QuestionsResult",
    "build_questions_tool",
    "create_plugin",
    "plugin",
]
