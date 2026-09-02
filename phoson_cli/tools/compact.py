"""The ``compact_context`` tool — agent-controlled compaction (#147, H-9).

The automatic compaction gate is *reactive*: it fires at a fixed fraction of
the context window, which knows nothing about the task and can interrupt a
reasoning step mid-subtask. The 2026 pattern is to hand control to the agent
with a dedicated tool it can call *strategically* — between tasks, or right
before consuming a large input — on top of the threshold gate, which stays
as the safety net.

The tool carries **no arguments** the model can see: it compacts "as much as
convenient right now" using the same structured handoff the automatic path
and ``/compact`` use. The actual work lives in a callable injected from the
front end (``do_compact``) because it needs the session runtime — the engine's
in-flight history, the chat client for the summary round trip, and the
summarizer to register the event the tree rebase consumes.

**Safety rule (H-9):** only the *structured* handoff is allowed. A single
step of free-form self-rewriting of the context is documented to erode
quality (one report: 18,282 → 122 tokens with accuracy dropping 66.7 → 57.1).
This tool reuses ``build_summary_prompt`` / ``safe_cut_index`` exactly, so it
never rewrites the history freely — it summarizes the old middle into the
fixed handoff document and keeps a tool-pair-safe recent tail.
"""

from typing import Any

from phoson_agent.tool import tool


@tool(inject=["do_compact"])
async def compact_context(*, do_compact: Any) -> str:
    """Compact the conversation now, on your judgement.

    Call this *between* tasks, or right before reading/processing a large
    input, to shrink the context while preserving continuity — instead of
    waiting for the automatic compaction gate to fire mid-task.

    It produces the same structured handoff summary the automatic
    compaction and ``/compact`` do (goal, completed work, key decisions,
    distilled reasoning, open questions, next steps, constraints) and keeps
    a recent tail of the conversation. What survives: the summary document,
    the recent tail, and the system prompt / AGENTS.md. What does not
    survive verbatim: the summarized older turns (their content is folded
    into the summary). Do **not** rely on compaction to preserve critical
    rules — put those in AGENTS.md or the system prompt, which survive
    everything.

    Returns a short report (tokens before/after, messages summarized).
    """
    return await do_compact()
