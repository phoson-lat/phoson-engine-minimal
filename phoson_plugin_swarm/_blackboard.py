"""Shared state, message routing and token accounting for a swarm.

Two independent subsystems live here:

* **Blackboard** (:class:`SharedState`, :class:`MessageBroker`) — the
  coordinator's shared medium. It is *not* exposed as tools to the members
  (they run to completion on the LLM, so they cannot call into it mid-run);
  instead the orchestrator *seeds* each member's prompt with the current
  blackboard contents and the member's inbox, and *collects* the member's
  reply back onto the blackboard after each run. That is the practical,
  bounded form of inter-agent communication in a run-to-completion engine, and
  it is exactly what the unit tests exercise (``swarm_message`` → target
  inbox → delivered on the next run).

* **Token accounting** (:class:`TokenBudget`) — a single shared ledger with a
  per-agent cap and a swarm-wide cap, so a runaway member (or the swarm as a
  whole) is stopped *gracefully* rather than burning the whole budget. The
  orchestrator checks :meth:`TokenBudget.allow` between steps while streaming a
  member, and :meth:`TokenBudget.consume` on each LLM step.
"""

import itertools
from typing import Any
from dataclasses import field, dataclass

from ._agent_role import SwarmError


#: A message on the blackboard. ``topic`` is a free-form routing/label key
#: (e.g. a role name or a sub-topic); ``to`` is the intended recipient agent
#: (``"*"`` is a broadcast).
@dataclass
class SwarmMessage:
    sender: str
    recipient: str
    content: str
    topic: str = ""
    seq: int = 0


@dataclass
class TokenBudget:
    """Shared token ledger with a per-agent cap and a swarm-wide cap.

    ``max_per_agent`` / ``max_total`` of ``None`` mean "unbounded" for that
    dimension. Both are checked on :meth:`allow`; :meth:`consume` records what
    was actually used (clamped to the caps so it never over-reports).
    """

    max_per_agent: int | None = None
    max_total: int | None = None
    _per_agent: dict[str, int] = field(default_factory=dict)
    _total: int = 0

    def _cap_hit(self, agent: str, tokens: int) -> bool:
        if self.max_total is not None:
            if self._total + tokens > self.max_total:
                return True
        if self.max_per_agent is not None:
            used = self._per_agent.get(agent, 0)
            if used + tokens > self.max_per_agent:
                return True
        return False

    def allow(self, agent: str, tokens: int) -> bool:
        """Would adding ``tokens`` to ``agent`` stay within both caps?"""
        if tokens <= 0:
            return True
        return not self._cap_hit(agent, tokens)

    def consume(self, agent: str, tokens: int) -> int:
        """Record ``tokens`` used by ``agent``; returns the amount recorded."""
        if tokens <= 0:
            return 0
        recorded = tokens
        if self.max_total is not None:
            remaining_total = max(0, self.max_total - self._total)
            recorded = min(recorded, remaining_total)
        if self.max_per_agent is not None:
            remaining_agent = max(0, self.max_per_agent - self._per_agent.get(agent, 0))
            recorded = min(recorded, remaining_agent)
        self._per_agent[agent] = self._per_agent.get(agent, 0) + recorded
        self._total += recorded
        return recorded

    def used(self, agent: str) -> int:
        return self._per_agent.get(agent, 0)

    def total_used(self) -> int:
        return self._total

    def exhausted(self, agent: str) -> bool:
        """True once either cap would block further spend for ``agent``."""
        return not self.allow(agent, 1)


@dataclass
class SharedState:
    """The blackboard: a string key/value store plus a message log.

    ``write``/``read`` are the shared facts agents exchange between turns;
    ``inbox`` routes messages to a specific agent (or a broadcast to ``"*"``).
    All access is from a single event loop, so no locking is required.
    """

    data: dict[str, Any] = field(default_factory=dict)
    messages: list[SwarmMessage] = field(default_factory=list)
    _inbox: dict[str, list[SwarmMessage]] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def write(self, key: str, value: Any) -> None:
        self.data[key] = value

    def read(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        topic: str = "",
    ) -> SwarmMessage:
        """Publish one message and route it to the recipient's inbox."""
        if not isinstance(content, str) or not content.strip():
            raise SwarmError("swarm_message 'content' must be a non-empty string")
        message = SwarmMessage(
            sender=sender,
            recipient=recipient,
            content=content,
            topic=topic,
            seq=next(self._seq),
        )
        self.messages.append(message)
        self._inbox.setdefault(recipient, []).append(message)
        return message

    def inbox(self, agent: str) -> list[SwarmMessage]:
        """Messages addressed to ``agent`` (plus broadcasts) still pending."""
        own = list(self._inbox.get(agent, []))
        broadcast = [m for m in self._inbox.get("*", [])]
        return own + broadcast

    def drain_inbox(self, agent: str) -> list[SwarmMessage]:
        """Pop and return the messages pending for ``agent`` (incl. broadcasts).

        Only the *named* inbox is cleared; broadcasts (``"*"``) are left in
        place so the other members still receive them. The orchestrator calls
        this after seeding an agent's prompt so repeated runs of the same
        member are not re-fed the same own-addressed messages.
        """
        pending = self.inbox(agent)
        self._inbox[agent] = []
        return pending

    def pending_count(self, agent: str | None = None) -> int:
        """Pending messages for one agent, or across the whole swarm."""
        if agent is None:
            return sum(len(v) for v in self._inbox.values())
        return len(self.inbox(agent))

    def snapshot(self) -> dict[str, Any]:
        """A compact, prompt-friendly view of the blackboard for a member."""
        text_parts: list[str] = []
        if self.data:
            for key in sorted(self.data):
                text_parts.append(f"{key}: {self.data[key]}")
        return {"facts": text_parts, "message_count": len(self.messages)}
