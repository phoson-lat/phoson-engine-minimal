"""The ``skill`` tool — load a skill's instructions on demand (G5, #52).

This is the *activation* half of the skills system: :mod:`phoson_cli.skills`
advertises a one-line index of every available skill in the system prompt,
and this tool returns the full ``SKILL.md`` body when the model decides one
is relevant.

Why a tool and not a slash command or keyword matching:

- **The model decides, mid-task.** Relevance is only known once the task is
  understood, which is after the user's message — a slash command would put
  the burden on the user, and keyword matching would fire on false
  positives ("the *architecture* of this function") while missing paraphrases.
- **It keeps the prompt cache intact (G2).** A tool result lands in the
  conversation, not in the system prompt's stable prefix, so activating a
  skill on turn 7 does not invalidate the cached prefix. Injecting the body
  into the system prompt instead would.
- **One tool, not one per skill.** Adding a skill must not change the tool
  schemas sent on every request (that is the cost skills exist to avoid),
  so the skill name is an *argument*, and the index in the prompt is what
  enumerates the valid values.

Discovery runs per call, so a skill added mid-session is usable on the next
turn without restarting the CLI.
"""

from phoson_agent.tool import tool
from phoson_cli.skills import find_skill, discover_skills, load_skill_body


def _skill(name: str) -> str:
    """Resolve *name* and return its instructions, or a helpful error."""
    available = discover_skills()
    if not available:
        return (
            "No skills are available. Skills are directories containing a"
            " SKILL.md file, in .phoson/skills/ (project) or"
            " ~/.phoson/skills/ (global)."
        )

    found = find_skill(name, available)
    if found is None:
        names = ", ".join(s.name for s in available)
        return f"Unknown skill {name!r}. Available skills: {names}"

    return load_skill_body(found)


@tool
def skill(name: str) -> str:
    """Load a skill's full instructions by name.

    Call this when a skill listed in the system prompt's skill index matches
    the current task, *before* starting the work. The result is the skill's
    complete instructions plus the location of any bundled scripts or
    reference files, which you can then read with read_file or run with bash.
    """
    return _skill(name)
