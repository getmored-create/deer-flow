"""Run event capture via LangChain callbacks.

RunJournal sits between LangChain's callback mechanism and the pluggable
RunEventStore. It standardizes callback data into RunEvent records and
handles token usage accumulation.

Key design decisions:
- on_llm_new_token is NOT implemented -- only complete messages via on_llm_end
- All LangChain objects serialized via serialize_lc_object (same as worker.py SSE)
- Token usage accumulated in memory, written to RunRow on run completion
- Caller identification via tags injection (lead_agent / subagent:{name} / middleware:{name})
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

if TYPE_CHECKING:
    from deerflow.runtime.events.store.base import RunEventStore

logger = logging.getLogger(__name__)


class RunJournal(BaseCallbackHandler):
    """LangChain callback handler that captures events to RunEventStore."""

    def __init__(
        self,
        run_id: str,
        thread_id: str,
        event_store: RunEventStore,
        *,
        track_token_usage: bool = True,
        flush_threshold: int = 20,
    ):
        super().__init__()
        self.run_id = run_id
        self.thread_id = thread_id
        self._store = event_store
        self._track_tokens = track_token_usage
        self._flush_threshold = flush_threshold

        # Write buffer
        self._buffer: list[dict] = []

        # Token accumulators
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_tokens = 0
        self._llm_call_count = 0
        self._lead_agent_tokens = 0
        self._subagent_tokens = 0
        self._middleware_tokens = 0

        # Convenience fields
        self._last_ai_msg: str | None = None
        self._first_human_msg: str | None = None
        self._msg_count = 0

        # Latency tracking
        self._llm_start_times: dict[str, float] = {}  # langchain run_id -> start time

        # Tool call ID cache
        self._tool_call_ids: dict[str, str] = {}  # langchain run_id -> tool_call_id

    # -- Lifecycle callbacks --

    def on_chain_start(self, serialized: dict, inputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if kwargs.get("parent_run_id") is not None:
            return
        self._put(
            event_type="run_start",
            category="lifecycle",
            metadata={"input_preview": str(inputs)[:500]},
        )

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if kwargs.get("parent_run_id") is not None:
            return
        self._put(event_type="run_end", category="lifecycle", metadata={"status": "success"})
        self._flush_sync()

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if kwargs.get("parent_run_id") is not None:
            return
        self._put(
            event_type="run_error",
            category="lifecycle",
            content=str(error),
            metadata={"error_type": type(error).__name__},
        )
        self._flush_sync()

    # -- LLM callbacks --

    def on_llm_start(self, serialized: dict, prompts: list[str], *, run_id: UUID, **kwargs: Any) -> None:
        self._llm_start_times[str(run_id)] = time.monotonic()
        self._put(
            event_type="llm_start",
            category="trace",
            metadata={"model_name": serialized.get("name", "")},
        )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        from deerflow.runtime.converters import langchain_to_openai_message
        from deerflow.runtime.serialization import serialize_lc_object

        try:
            message = response.generations[0][0].message
        except (IndexError, AttributeError):
            logger.debug("on_llm_end: could not extract message from response")
            return

        caller = self._identify_caller(kwargs)

        # Latency
        start = self._llm_start_times.pop(str(run_id), None)
        latency_ms = int((time.monotonic() - start) * 1000) if start else None

        # Token usage from message
        usage = getattr(message, "usage_metadata", None)
        usage_dict = dict(usage) if usage else {}

        # Trace event: llm_end (every LLM call)
        content = getattr(message, "content", "")
        self._put(
            event_type="llm_end",
            category="trace",
            content=content if isinstance(content, str) else str(content),
            metadata={
                "message": serialize_lc_object(message),
                "caller": caller,
                "usage": usage_dict,
                "latency_ms": latency_ms,
            },
        )

        # Message events: only lead_agent gets message-category events
        tool_calls = getattr(message, "tool_calls", None) or []
        if caller == "lead_agent":
            resp_meta = getattr(message, "response_metadata", None) or {}
            model_name = resp_meta.get("model_name") if isinstance(resp_meta, dict) else None
            if tool_calls:
                # ai_tool_call: agent decided to use tools
                self._put(
                    event_type="ai_tool_call",
                    category="message",
                    content=langchain_to_openai_message(message),
                    metadata={"model_name": model_name, "finish_reason": "tool_calls"},
                )
            elif isinstance(content, str) and content:
                # ai_message: final text reply
                self._put(
                    event_type="ai_message",
                    category="message",
                    content={"role": "assistant", "content": content},
                    metadata={"model_name": model_name, "finish_reason": "stop"},
                )
                self._last_ai_msg = content[:2000]
                self._msg_count += 1

        # Token accumulation
        if self._track_tokens:
            input_tk = usage_dict.get("input_tokens", 0) or 0
            output_tk = usage_dict.get("output_tokens", 0) or 0
            total_tk = usage_dict.get("total_tokens", 0) or 0
            if total_tk == 0:
                total_tk = input_tk + output_tk
            if total_tk > 0:
                self._total_input_tokens += input_tk
                self._total_output_tokens += output_tk
                self._total_tokens += total_tk
                self._llm_call_count += 1
                if caller.startswith("subagent:"):
                    self._subagent_tokens += total_tk
                elif caller.startswith("middleware:"):
                    self._middleware_tokens += total_tk
                else:
                    self._lead_agent_tokens += total_tk

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._llm_start_times.pop(str(run_id), None)
        self._put(event_type="llm_error", category="trace", content=str(error))

    # -- Tool callbacks --

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: UUID, **kwargs: Any) -> None:
        tool_call_id = kwargs.get("tool_call_id")
        if tool_call_id:
            self._tool_call_ids[str(run_id)] = tool_call_id
        self._put(
            event_type="tool_start",
            category="trace",
            metadata={
                "tool_name": serialized.get("name", ""),
                "tool_call_id": tool_call_id,
                "args": str(input_str)[:2000],
            },
        )

    def on_tool_end(self, output: str, *, run_id: UUID, **kwargs: Any) -> None:
        tool_call_id = kwargs.get("tool_call_id") or self._tool_call_ids.pop(str(run_id), None)
        tool_name = kwargs.get("name", "")

        # Trace event (always)
        self._put(
            event_type="tool_end",
            category="trace",
            content=str(output),
            metadata={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "status": "success",
            },
        )

        # Message event: tool_result
        self._put(
            event_type="tool_result",
            category="message",
            content={
                "role": "tool",
                "tool_call_id": tool_call_id or "",
                "content": str(output),
            },
            metadata={"tool_name": tool_name},
        )

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._put(
            event_type="tool_error",
            category="trace",
            content=str(error),
            metadata={
                "tool_name": kwargs.get("name", ""),
                "tool_call_id": kwargs.get("tool_call_id"),
            },
        )

    # -- Custom event callback --

    def on_custom_event(self, name: str, data: Any, *, run_id: UUID, **kwargs: Any) -> None:
        from deerflow.runtime.serialization import serialize_lc_object

        if name == "summarization":
            data_dict = data if isinstance(data, dict) else {}
            self._put(
                event_type="summarization",
                category="trace",
                content=data_dict.get("summary", ""),
                metadata={
                    "replaced_message_ids": data_dict.get("replaced_message_ids", []),
                    "replaced_count": data_dict.get("replaced_count", 0),
                },
            )
            self._put(
                event_type="summary",
                category="message",
                content={"role": "system", "content": data_dict.get("summary", "")},
                metadata={"replaced_count": data_dict.get("replaced_count", 0)},
            )
        else:
            event_data = serialize_lc_object(data) if not isinstance(data, dict) else data
            self._put(
                event_type=name,
                category="trace",
                metadata=event_data if isinstance(event_data, dict) else {"data": event_data},
            )

    # -- Internal methods --

    def _put(self, *, event_type: str, category: str, content: str | dict = "", metadata: dict | None = None) -> None:
        self._buffer.append({
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "event_type": event_type,
            "category": category,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(UTC).isoformat(),
        })
        if len(self._buffer) >= self._flush_threshold:
            self._flush_sync()

    def _flush_sync(self) -> None:
        """Best-effort flush of buffer to RunEventStore.

        BaseCallbackHandler methods are synchronous.  If an event loop is
        running we schedule an async ``put_batch``; otherwise the events
        stay in the buffer and are flushed later by the async ``flush()``
        call in the worker's ``finally`` block.
        """
        if not self._buffer:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop — keep events in buffer for later async flush.
            return
        batch = self._buffer.copy()
        self._buffer.clear()
        loop.create_task(self._flush_async(batch))

    async def _flush_async(self, batch: list[dict]) -> None:
        try:
            await self._store.put_batch(batch)
        except Exception:
            logger.warning("RunJournal: failed to flush %d events", len(batch), exc_info=True)

    def _identify_caller(self, kwargs: dict) -> str:
        for tag in kwargs.get("tags") or []:
            if isinstance(tag, str) and (tag.startswith("subagent:") or tag.startswith("middleware:") or tag == "lead_agent"):
                return tag
        # Default to lead_agent: the main agent graph does not inject
        # callback tags, while subagents and middleware explicitly tag
        # themselves.
        return "lead_agent"

    # -- Public methods (called by worker) --

    def set_first_human_message(self, content: str) -> None:
        """Record the first human message for convenience fields."""
        self._first_human_msg = content[:2000] if content else None

    async def flush(self) -> None:
        """Force flush remaining buffer. Called in worker's finally block."""
        if self._buffer:
            batch = self._buffer.copy()
            self._buffer.clear()
            await self._store.put_batch(batch)

    def get_completion_data(self) -> dict:
        """Return accumulated token and message data for run completion."""
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_tokens,
            "llm_call_count": self._llm_call_count,
            "lead_agent_tokens": self._lead_agent_tokens,
            "subagent_tokens": self._subagent_tokens,
            "middleware_tokens": self._middleware_tokens,
            "message_count": self._msg_count,
            "last_ai_message": self._last_ai_msg,
            "first_human_message": self._first_human_msg,
        }
