"""Session metrics tracking."""

from dataclasses import field, dataclass


@dataclass
class SessionMetrics:
    """Accumulated metrics for a conversation session."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_tokens: int = 0
    step_count: int = 0

    # Per-step breakdown (type, duration_ms, model/tool name, error)
    steps: list[tuple[str, int, str, str | None]] = field(default_factory=list)

    def add_step(
        self,
        step_type: str,  # "llm" or "tool"
        duration_ms: int,
        name: str,
        error: str | None = None,
    ) -> None:
        self.steps.append((step_type, duration_ms, name, error))
        self.step_count += 1

    def add_llm_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.total_cost_usd += cost_usd
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_tokens += cache_tokens
