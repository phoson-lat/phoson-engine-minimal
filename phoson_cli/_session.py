"""Session-scoped data containers.

:class:`SessionMetrics` and :class:`SessionState` carry no UI or
prompt_toolkit dependencies, so they live here rather than in the
larger ``repl`` module.
"""

from typing import Self
from dataclasses import field, dataclass

from phoson_agent import RunStep
from phoson_agent.sessions import ConversationTree


@dataclass
class SessionMetrics:
    """Accumulated metrics for the current session."""

    total_cost_usd: float = 0.0
    total_credits: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cache_read_tokens: int = 0
    step_count: int = 0
    last_model: str = ""
    steps: list[RunStep] = field(default_factory=list)

    # For phoson_weight calculation
    phoson_weight: float = 1.0

    def add_run_step(self, step: RunStep) -> None:
        """Add a run step and update totals."""
        self.steps.append(step)
        self.step_count += 1

        if step.usage:
            self.total_input_tokens += step.usage.input
            self.total_output_tokens += step.usage.output
            if step.usage.cache_write:
                self.total_cache_write_tokens += step.usage.cache_write
            if step.usage.cache_read:
                self.total_cache_read_tokens += step.usage.cache_read

        self.total_cost_usd += step.cost_usd
        self.total_credits += step.credits
        if step.model:
            self.last_model = step.model

    def reset(self) -> None:
        """Reset all metrics for a new session."""
        self.total_cost_usd = 0.0
        self.total_credits = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_cache_read_tokens = 0
        self.step_count = 0
        self.last_model = ""
        self.steps.clear()

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def avg_cost_per_message(self) -> float:
        if self.step_count == 0:
            return 0.0
        return self.total_cost_usd / self.step_count

    def load_from_meta(self, meta: dict) -> None:
        """Load metrics from session metadata dict."""
        self.total_cost_usd = meta.get("total_cost_usd", 0.0)
        self.total_credits = meta.get("total_credits", 0.0)
        self.total_input_tokens = meta.get("total_input_tokens", 0)
        self.total_output_tokens = meta.get("total_output_tokens", 0)
        self.total_cache_write_tokens = meta.get("total_cache_write_tokens", 0)
        self.total_cache_read_tokens = meta.get("total_cache_read_tokens", 0)
        self.step_count = meta.get("step_count", 0)
        self.last_model = meta.get("last_model", "")

    def to_meta(self) -> dict:
        """Convert to metadata dict for storage."""
        return {
            "total_cost_usd": self.total_cost_usd,
            "total_credits": self.total_credits,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "step_count": self.step_count,
            "last_model": self.last_model,
        }


@dataclass
class SessionState:
    """Mutable per-session fields that reset together on new/load/branch.

    Grouping these three fields makes the session lifecycle explicit:
    every operation that starts a fresh session (new, load, branch)
    operates on this struct rather than scattering assignments across
    :class:`~phoson_cli.repl.PhosonRepl`.

    Attributes:
        tree: The active :class:`ConversationTree`.
        metrics: Accumulated cost, token and step counters.
        current_node_id: ID of the most recently active tree node,
            or ``None`` for an empty session.
    """

    tree: ConversationTree
    metrics: SessionMetrics
    current_node_id: str | None = None

    @classmethod
    def new(cls) -> Self:
        """Create a fresh session with a new tree and zeroed metrics."""
        return cls(tree=ConversationTree.new(), metrics=SessionMetrics())

    def reset(self) -> None:
        """Replace tree and metrics in-place for a brand-new session."""
        self.tree = ConversationTree.new()
        self.metrics = SessionMetrics()
        self.current_node_id = None
