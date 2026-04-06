"""Background agent execution.

Runs an agent graph inside an ``asyncio.Task``, publishing events to
a :class:`StreamBridge` as they are produced.

Uses ``graph.astream(stream_mode=[...])`` which gives correct full-state
snapshots for ``values`` mode, proper ``{node: writes}`` for ``updates``,
and ``(chunk, metadata)`` tuples for ``messages`` mode.

Note: ``events`` mode is not supported through the gateway — it requires
``graph.astream_events()`` which cannot simultaneously produce ``values``
snapshots.  The JS open-source LangGraph API server works around this via
internal checkpoint callbacks that are not exposed in the Python public API.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from langchain_core.messages import HumanMessage

from deerflow.runtime.serialization import serialize
from deerflow.runtime.stream_bridge import StreamBridge

from .manager import RunManager, RunRecord
from .schemas import RunStatus

logger = logging.getLogger(__name__)

# Valid stream_mode values for LangGraph's graph.astream()
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}


@dataclass(frozen=True)
class RunContext:
    """Infrastructure dependencies for a single agent run.

    Groups checkpointer, store, and persistence-related singletons so that
    ``run_agent`` (and any future callers) receive one object instead of a
    growing list of keyword arguments.
    """

    checkpointer: Any
    store: Any | None = field(default=None)
    event_store: Any | None = field(default=None)
    run_events_config: Any | None = field(default=None)
    thread_meta_repo: Any | None = field(default=None)
    follow_up_to_run_id: str | None = field(default=None)


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    ctx: RunContext,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None:
    """Execute an agent in the background, publishing events to *bridge*."""

    # Unpack infrastructure dependencies from RunContext.
    checkpointer = ctx.checkpointer
    store = ctx.store
    event_store = ctx.event_store
    run_events_config = ctx.run_events_config
    thread_meta_repo = ctx.thread_meta_repo
    follow_up_to_run_id = ctx.follow_up_to_run_id

    run_id = record.run_id
    thread_id = record.thread_id
    requested_modes: set[str] = set(stream_modes or ["values"])

    # Initialize RunJournal for event capture
    journal = None
    if event_store is not None:
        from deerflow.runtime.journal import RunJournal

        journal = RunJournal(
            run_id=run_id,
            thread_id=thread_id,
            event_store=event_store,
            track_token_usage=getattr(run_events_config, "track_token_usage", True),
        )

        # Write human_message event (model_dump format, aligned with checkpoint)
        human_msg = _extract_human_message(graph_input)
        if human_msg is not None:
            msg_metadata = {}
            if follow_up_to_run_id:
                msg_metadata["follow_up_to_run_id"] = follow_up_to_run_id
            await event_store.put(
                thread_id=thread_id,
                run_id=run_id,
                event_type="human_message",
                category="message",
                content=human_msg.model_dump(),
                metadata=msg_metadata or None,
            )
            content = human_msg.content
            journal.set_first_human_message(content if isinstance(content, str) else str(content))

    # Track whether "events" was requested but skipped
    if "events" in requested_modes:
        logger.info(
            "Run %s: 'events' stream_mode not supported in gateway (requires astream_events + checkpoint callbacks). Skipping.",
            run_id,
        )

    try:
        # 1. Mark running
        await run_manager.set_status(run_id, RunStatus.running)

        # Record pre-run checkpoint_id to support rollback (Phase 2).
        pre_run_checkpoint_id = None
        try:
            config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
            if ckpt_tuple is not None:
                pre_run_checkpoint_id = getattr(ckpt_tuple, "config", {}).get("configurable", {}).get("checkpoint_id")
        except Exception:
            logger.debug("Could not get pre-run checkpoint_id for run %s", run_id)

        # 2. Publish metadata — useStream needs both run_id AND thread_id
        await bridge.publish(
            run_id,
            "metadata",
            {
                "run_id": run_id,
                "thread_id": thread_id,
            },
        )

        # 3. Build the agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        # Inject runtime context so middlewares can access thread_id
        # (langgraph-cli does this automatically; we must do it manually)
        runtime = Runtime(context={"thread_id": thread_id}, store=store)
        # If the caller already set a ``context`` key (LangGraph >= 0.6.0
        # prefers it over ``configurable`` for thread-level data), make
        # sure ``thread_id`` is available there too.
        if "context" in config and isinstance(config["context"], dict):
            config["context"].setdefault("thread_id", thread_id)
        config.setdefault("configurable", {})["__pregel_runtime"] = runtime

        # Inject RunJournal as a LangChain callback handler.
        # on_llm_end captures token usage; on_chain_start/end captures lifecycle.
        if journal is not None:
            config.setdefault("callbacks", []).append(journal)

        runnable_config = RunnableConfig(**config)
        agent = agent_factory(config=runnable_config)

        # 4. Attach checkpointer and store
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

        # 5. Set interrupt nodes
        if interrupt_before:
            agent.interrupt_before_nodes = interrupt_before
        if interrupt_after:
            agent.interrupt_after_nodes = interrupt_after

        # 6. Build LangGraph stream_mode list
        #    "events" is NOT a valid astream mode — skip it
        #    "messages-tuple" maps to LangGraph's "messages" mode
        lg_modes: list[str] = []
        for m in requested_modes:
            if m == "messages-tuple":
                lg_modes.append("messages")
            elif m == "events":
                # Skipped — see log above
                continue
            elif m in _VALID_LG_MODES:
                lg_modes.append(m)
        if not lg_modes:
            lg_modes = ["values"]

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for m in lg_modes:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        lg_modes = deduped

        logger.info("Run %s: streaming with modes %s (requested: %s)", run_id, lg_modes, requested_modes)

        # 7. Stream using graph.astream
        if len(lg_modes) == 1 and not stream_subgraphs:
            # Single mode, no subgraphs: astream yields raw chunks
            single_mode = lg_modes[0]
            async for chunk in agent.astream(graph_input, config=runnable_config, stream_mode=single_mode):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break
                sse_event = _lg_mode_to_sse_event(single_mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
        else:
            # Multiple modes or subgraphs: astream yields tuples
            async for item in agent.astream(
                graph_input,
                config=runnable_config,
                stream_mode=lg_modes,
                subgraphs=stream_subgraphs,
            ):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break

                mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
                if mode is None:
                    continue

                sse_event = _lg_mode_to_sse_event(mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))

        # 8. Final status
        if record.abort_event.is_set():
            action = record.abort_action
            if action == "rollback":
                await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
                # TODO(Phase 2): Implement full checkpoint rollback.
                # Use pre_run_checkpoint_id to revert the thread's checkpoint
                # to the state before this run started. Requires a
                # checkpointer.adelete() or equivalent API.
                try:
                    if checkpointer is not None and pre_run_checkpoint_id is not None:
                        # Phase 2: roll back to pre_run_checkpoint_id
                        pass
                    logger.info("Run %s rolled back", run_id)
                except Exception:
                    logger.warning("Failed to rollback checkpoint for run %s", run_id)
            else:
                await run_manager.set_status(run_id, RunStatus.interrupted)
        else:
            await run_manager.set_status(run_id, RunStatus.success)

    except asyncio.CancelledError:
        action = record.abort_action
        if action == "rollback":
            await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
            logger.info("Run %s was cancelled (rollback)", run_id)
        else:
            await run_manager.set_status(run_id, RunStatus.interrupted)
            logger.info("Run %s was cancelled", run_id)

    except Exception as exc:
        error_msg = f"{exc}"
        logger.exception("Run %s failed: %s", run_id, error_msg)
        await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        await bridge.publish(
            run_id,
            "error",
            {
                "message": error_msg,
                "name": type(exc).__name__,
            },
        )

    finally:
        # Flush any buffered journal events and persist completion data
        if journal is not None:
            try:
                await journal.flush()
            except Exception:
                logger.warning("Failed to flush journal for run %s", run_id, exc_info=True)

            # Persist token usage + convenience fields to RunStore
            completion = journal.get_completion_data()
            await run_manager.update_run_completion(run_id, status=record.status.value, **completion)

        # Sync title from checkpoint to threads_meta.display_name
        if thread_meta_repo is not None and checkpointer is not None:
            try:
                ckpt_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(ckpt_config)
                if ckpt_tuple is not None:
                    ckpt = getattr(ckpt_tuple, "checkpoint", {}) or {}
                    title = ckpt.get("channel_values", {}).get("title")
                    if title:
                        await thread_meta_repo.update_display_name(thread_id, title)
            except Exception:
                logger.debug("Failed to sync title for thread %s (non-fatal)", thread_id)

        # Update threads_meta status based on run outcome
        if thread_meta_repo is not None:
            try:
                final_status = "idle" if record.status == RunStatus.success else record.status.value
                await thread_meta_repo.update_status(thread_id, final_status)
            except Exception:
                logger.debug("Failed to update thread_meta status for %s (non-fatal)", thread_id)

        await bridge.publish_end(run_id)
        asyncio.create_task(bridge.cleanup(run_id, delay=60))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lg_mode_to_sse_event(mode: str) -> str:
    """Map LangGraph internal stream_mode name to SSE event name.

    LangGraph's ``astream(stream_mode="messages")`` produces message
    tuples.  The SSE protocol calls this ``messages-tuple`` when the
    client explicitly requests it, but the default SSE event name used
    by LangGraph Platform is simply ``"messages"``.
    """
    # All LG modes map 1:1 to SSE event names — "messages" stays "messages"
    return mode


def _extract_human_message(graph_input: dict) -> HumanMessage | None:
    """Extract or construct a HumanMessage from graph_input for event recording.

    Returns a LangChain HumanMessage so callers can use .model_dump() to get
    the checkpoint-aligned serialization format.
    """
    from langchain_core.messages import HumanMessage

    messages = graph_input.get("messages")
    if not messages:
        return None
    last = messages[-1] if isinstance(messages, list) else messages
    if isinstance(last, HumanMessage):
        return last
    if isinstance(last, str):
        return HumanMessage(content=last) if last else None
    if hasattr(last, "content"):
        content = last.content
        return HumanMessage(content=content)
    if isinstance(last, dict):
        content = last.get("content", "")
        return HumanMessage(content=content) if content else None
    return None


def _unpack_stream_item(
    item: Any,
    lg_modes: list[str],
    stream_subgraphs: bool,
) -> tuple[str | None, Any]:
    """Unpack a multi-mode or subgraph stream item into (mode, chunk).

    Returns ``(None, None)`` if the item cannot be parsed.
    """
    if stream_subgraphs:
        if isinstance(item, tuple) and len(item) == 3:
            _ns, mode, chunk = item
            return str(mode), chunk
        if isinstance(item, tuple) and len(item) == 2:
            mode, chunk = item
            return str(mode), chunk
        return None, None

    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = item
        return str(mode), chunk

    # Fallback: single-element output from first mode
    return lg_modes[0] if lg_modes else None, item
