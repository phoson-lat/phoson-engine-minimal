"""Session metrics tracking for cost, tokens, and steps."""

from dataclasses import field, dataclass

from phoson_agent.models import RunStep


@dataclass
class SessionMetrics:
    """Aggregated metrics for a conversation session across multiple agent runs."""

    total_cost_usd: float = 0.0
    total_credits: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_steps: int = 0
    step_history: list[RunStep] = field(default_factory=list)

    def add_run(self, steps: list[RunStep], cost_usd: float, credits: float) -> None:
        """Add metrics from a single agent run."""
        self.total_cost_usd += cost_usd
        self.total_credits += credits
        self.total_steps += len(steps)
        self.step_history.extend(steps)

        # Aggregate tokens from all steps
        for step in steps:
            if step.usage:
                self.total_input_tokens += step.usage.input_tokens
                self.total_output_tokens += step.usage.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def io_ratio(self) -> float:
        """Input/output token ratio."""
        if self.total_output_tokens == 0:
            return 0.0
        return self.total_input_tokens / self.total_output_tokens

    def average_cost_per_message(self) -> float:
        """Average cost per user message (≈ total_steps/2 for user turns)."""
        user_turns = max(1, self.total_steps // 2)
        return self.total_cost_usd / user_turns

    def clear(self) -> None:
        """Reset all metrics."""
        self.total_cost_usd = 0.0
        self.total_credits = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_steps = 0
        self.step_history.clear()
