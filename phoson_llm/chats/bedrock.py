"""AWS Bedrock adapter using the boto3 Converse API."""

import os
import asyncio
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator

from phoson_llm.utils import normalize_stop_reason
from phoson_llm.pricing import calculate_cost
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolDefinition,
)
from phoson_llm.chats.base import BaseLLMChat

if TYPE_CHECKING:
    pass


class BedrockChat(BaseLLMChat):
    """Adapter for AWS Bedrock using the ``boto3`` Converse API.

    Args:
        region_name: AWS region. Defaults to ``AWS_DEFAULT_REGION`` or ``us-east-1``.
    """

    def __init__(self, region_name: str | None = None) -> None:
        self._region_name = region_name or os.environ.get(
            "AWS_DEFAULT_REGION", "us-east-1"
        )
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "bedrock-runtime", region_name=self._region_name
            )
        return self._client

    def __repr__(self) -> str:
        return f"BedrockChat(region_name={self._region_name!r})"

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        client = self._get_client()

        # Simple conversion for now
        bedrock_messages = []
        for msg in messages:
            if msg.role == "system":
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            bedrock_messages.append(
                {
                    "role": "user" if msg.role == "user" else "assistant",
                    "content": [{"text": content}],
                }
            )

        system = []
        if config.system:
            system.append({"text": config.system})
        else:
            for msg in messages:
                if msg.role == "system" and isinstance(msg.content, str):
                    system.append({"text": msg.content})
                    break

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        try:
            # boto3 is synchronous, so we run in an executor
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.converse(
                    modelId=config.model,
                    messages=bedrock_messages,
                    system=system,
                    inferenceConfig={
                        "temperature": config.temperature or 0.7,
                        "maxTokens": config.max_tokens or 2048,
                    },
                ),
            )

            text = response["output"]["message"]["content"][0]["text"]
            yield TokenEvent(content=text)

            u = response["usage"]
            usage = TokenUsage(input=u["inputTokens"], output=u["outputTokens"])
            cost_usd, cost_known = calculate_cost(
                model=config.model,
                input_tokens=usage.input,
                output_tokens=usage.output,
                provider="bedrock",
            )
            yield UsageEvent(
                model=config.model,
                usage=usage,
                cost_usd=cost_usd,
                cost_known=cost_known,
            )

            yield LLMDoneEvent(
                content=text,
                has_tool_calls=False,
                # F-13: Converse reports the stop reason at the response top
                # level (``end_turn`` / ``max_tokens`` / ``stop_sequence`` /
                # ``tool_use``).
                stop_reason=normalize_stop_reason(
                    response.get("stop_reason"), provider="bedrock"
                ),
            )

        except Exception as e:
            from phoson_llm.schemas import ErrorEvent

            yield ErrorEvent(message=str(e), code="provider_error", retryable=False)
            return
