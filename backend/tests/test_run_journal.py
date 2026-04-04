"""Tests for RunJournal callback handler.

Uses MemoryRunEventStore as the backend for direct event inspection.
"""

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.journal import RunJournal


@pytest.fixture
def journal_setup():
    store = MemoryRunEventStore()
    j = RunJournal("r1", "t1", store, flush_threshold=100)
    return j, store


def _make_llm_response(content="Hello", usage=None, tool_calls=None):
    """Create a mock LLM response with a message."""
    msg = MagicMock()
    msg.type = "ai"
    msg.content = content
    msg.id = f"msg-{id(msg)}"
    msg.tool_calls = tool_calls or []
    msg.response_metadata = {"model_name": "test-model"}
    msg.usage_metadata = usage
    # Provide a real model_dump so serialize_lc_object returns a plain dict
    # (needed for DB-backed tests where json.dumps must succeed).
    msg.model_dump.return_value = {
        "type": "ai",
        "content": content,
        "id": msg.id,
        "tool_calls": tool_calls or [],
        "usage_metadata": usage,
        "response_metadata": {"model_name": "test-model"},
    }

    gen = MagicMock()
    gen.message = msg

    response = MagicMock()
    response.generations = [[gen]]
    return response


class TestLlmCallbacks:
    @pytest.mark.anyio
    async def test_on_llm_end_produces_trace_event(self, journal_setup):
        j, store = journal_setup
        run_id = uuid4()
        j.on_llm_start({}, [], run_id=run_id, tags=["lead_agent"])
        j.on_llm_end(_make_llm_response("Hi"), run_id=run_id, tags=["lead_agent"])
        await j.flush()
        events = await store.list_events("t1", "r1")
        trace_events = [e for e in events if e["event_type"] == "llm_end"]
        assert len(trace_events) == 1
        assert trace_events[0]["category"] == "trace"

    @pytest.mark.anyio
    async def test_on_llm_end_lead_agent_produces_ai_message(self, journal_setup):
        j, store = journal_setup
        run_id = uuid4()
        j.on_llm_start({}, [], run_id=run_id, tags=["lead_agent"])
        j.on_llm_end(_make_llm_response("Answer"), run_id=run_id, tags=["lead_agent"])
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["event_type"] == "ai_message"
        assert messages[0]["content"] == {"role": "assistant", "content": "Answer"}

    @pytest.mark.anyio
    async def test_on_llm_end_with_tool_calls_produces_ai_tool_call(self, journal_setup):
        """LLM response with pending tool_calls should produce ai_tool_call event."""
        j, store = journal_setup
        run_id = uuid4()
        j.on_llm_end(
            _make_llm_response("Let me search", tool_calls=[{"id": "call_1", "name": "search", "args": {}}]),
            run_id=run_id,
            tags=["lead_agent"],
        )
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["event_type"] == "ai_tool_call"

    @pytest.mark.anyio
    async def test_on_llm_end_subagent_no_ai_message(self, journal_setup):
        j, store = journal_setup
        run_id = uuid4()
        j.on_llm_start({}, [], run_id=run_id, tags=["subagent:research"])
        j.on_llm_end(_make_llm_response("Sub answer"), run_id=run_id, tags=["subagent:research"])
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 0

    @pytest.mark.anyio
    async def test_token_accumulation(self, journal_setup):
        j, store = journal_setup
        usage1 = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        usage2 = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
        j.on_llm_end(_make_llm_response("A", usage=usage1), run_id=uuid4(), tags=["lead_agent"])
        j.on_llm_end(_make_llm_response("B", usage=usage2), run_id=uuid4(), tags=["lead_agent"])
        assert j._total_input_tokens == 30
        assert j._total_output_tokens == 15
        assert j._total_tokens == 45
        assert j._llm_call_count == 2

    @pytest.mark.anyio
    async def test_total_tokens_computed_from_input_output(self, journal_setup):
        """If total_tokens is 0, it should be computed from input + output."""
        j, store = journal_setup
        j.on_llm_end(
            _make_llm_response("Hi", usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 0}),
            run_id=uuid4(),
            tags=["lead_agent"],
        )
        assert j._total_tokens == 150
        assert j._lead_agent_tokens == 150

    @pytest.mark.anyio
    async def test_caller_token_classification(self, journal_setup):
        j, store = journal_setup
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        j.on_llm_end(_make_llm_response("A", usage=usage), run_id=uuid4(), tags=["lead_agent"])
        j.on_llm_end(_make_llm_response("B", usage=usage), run_id=uuid4(), tags=["subagent:research"])
        j.on_llm_end(_make_llm_response("C", usage=usage), run_id=uuid4(), tags=["middleware:summarization"])
        assert j._lead_agent_tokens == 15
        assert j._subagent_tokens == 15
        assert j._middleware_tokens == 15

    @pytest.mark.anyio
    async def test_usage_metadata_none_no_crash(self, journal_setup):
        j, store = journal_setup
        j.on_llm_end(_make_llm_response("No usage", usage=None), run_id=uuid4(), tags=["lead_agent"])
        await j.flush()

    @pytest.mark.anyio
    async def test_latency_tracking(self, journal_setup):
        j, store = journal_setup
        run_id = uuid4()
        j.on_llm_start({}, [], run_id=run_id, tags=["lead_agent"])
        j.on_llm_end(_make_llm_response("Fast"), run_id=run_id, tags=["lead_agent"])
        await j.flush()
        events = await store.list_events("t1", "r1")
        llm_end = [e for e in events if e["event_type"] == "llm_end"][0]
        assert "latency_ms" in llm_end["metadata"]
        assert llm_end["metadata"]["latency_ms"] is not None


class TestLifecycleCallbacks:
    @pytest.mark.anyio
    async def test_chain_start_end_produce_lifecycle_events(self, journal_setup):
        j, store = journal_setup
        j.on_chain_start({}, {}, run_id=uuid4(), parent_run_id=None)
        j.on_chain_end({}, run_id=uuid4(), parent_run_id=None)
        await asyncio.sleep(0.05)
        await j.flush()
        events = await store.list_events("t1", "r1")
        types = [e["event_type"] for e in events if e["category"] == "lifecycle"]
        assert "run_start" in types
        assert "run_end" in types

    @pytest.mark.anyio
    async def test_nested_chain_ignored(self, journal_setup):
        j, store = journal_setup
        parent_id = uuid4()
        j.on_chain_start({}, {}, run_id=uuid4(), parent_run_id=parent_id)
        j.on_chain_end({}, run_id=uuid4(), parent_run_id=parent_id)
        await j.flush()
        events = await store.list_events("t1", "r1")
        lifecycle = [e for e in events if e["category"] == "lifecycle"]
        assert len(lifecycle) == 0


class TestToolCallbacks:
    @pytest.mark.anyio
    async def test_tool_start_end_produce_trace(self, journal_setup):
        j, store = journal_setup
        j.on_tool_start({"name": "web_search"}, "query", run_id=uuid4())
        j.on_tool_end("results", run_id=uuid4(), name="web_search")
        await j.flush()
        events = await store.list_events("t1", "r1")
        trace_types = {e["event_type"] for e in events if e["category"] == "trace"}
        assert "tool_start" in trace_types
        assert "tool_end" in trace_types

    @pytest.mark.anyio
    async def test_on_tool_error(self, journal_setup):
        j, store = journal_setup
        j.on_tool_error(TimeoutError("timeout"), run_id=uuid4(), name="web_fetch")
        await j.flush()
        events = await store.list_events("t1", "r1")
        assert any(e["event_type"] == "tool_error" for e in events)


class TestCustomEvents:
    @pytest.mark.anyio
    async def test_summarization_event(self, journal_setup):
        j, store = journal_setup
        j.on_custom_event(
            "summarization",
            {"summary": "Context was summarized.", "replaced_count": 5, "replaced_message_ids": ["a", "b"]},
            run_id=uuid4(),
        )
        await j.flush()
        events = await store.list_events("t1", "r1")
        trace = [e for e in events if e["event_type"] == "summarization"]
        assert len(trace) == 1
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["event_type"] == "summary"
        assert messages[0]["content"] == {"role": "system", "content": "Context was summarized."}

    @pytest.mark.anyio
    async def test_non_summarization_custom_event(self, journal_setup):
        j, store = journal_setup
        j.on_custom_event("task_running", {"task_id": "t1", "status": "running"}, run_id=uuid4())
        await j.flush()
        events = await store.list_events("t1", "r1")
        assert any(e["event_type"] == "task_running" for e in events)


class TestBufferFlush:
    @pytest.mark.anyio
    async def test_flush_threshold(self, journal_setup):
        j, store = journal_setup
        j._flush_threshold = 3
        j.on_tool_start({"name": "a"}, "x", run_id=uuid4())
        j.on_tool_start({"name": "b"}, "x", run_id=uuid4())
        assert len(j._buffer) == 2
        j.on_tool_start({"name": "c"}, "x", run_id=uuid4())
        await asyncio.sleep(0.1)
        events = await store.list_events("t1", "r1")
        assert len(events) >= 3

    @pytest.mark.anyio
    async def test_events_retained_when_no_loop(self, journal_setup):
        """Events buffered in a sync (no-loop) context should survive
        until the async flush() in the finally block."""
        j, store = journal_setup
        j._flush_threshold = 1

        original = asyncio.get_running_loop

        def no_loop():
            raise RuntimeError("no running event loop")

        asyncio.get_running_loop = no_loop
        try:
            j._put(event_type="llm_end", category="trace", content="test")
        finally:
            asyncio.get_running_loop = original

        assert len(j._buffer) == 1
        await j.flush()
        events = await store.list_events("t1", "r1")
        assert any(e["event_type"] == "llm_end" for e in events)


class TestIdentifyCaller:
    def test_lead_agent_tag(self, journal_setup):
        j, _ = journal_setup
        assert j._identify_caller({"tags": ["lead_agent"]}) == "lead_agent"

    def test_subagent_tag(self, journal_setup):
        j, _ = journal_setup
        assert j._identify_caller({"tags": ["subagent:research"]}) == "subagent:research"

    def test_middleware_tag(self, journal_setup):
        j, _ = journal_setup
        assert j._identify_caller({"tags": ["middleware:summarization"]}) == "middleware:summarization"

    def test_no_tags_returns_lead_agent(self, journal_setup):
        j, _ = journal_setup
        assert j._identify_caller({"tags": []}) == "lead_agent"
        assert j._identify_caller({}) == "lead_agent"


class TestChainErrorCallback:
    @pytest.mark.anyio
    async def test_on_chain_error_writes_run_error(self, journal_setup):
        j, store = journal_setup
        j.on_chain_error(ValueError("boom"), run_id=uuid4(), parent_run_id=None)
        await asyncio.sleep(0.05)
        await j.flush()
        events = await store.list_events("t1", "r1")
        error_events = [e for e in events if e["event_type"] == "run_error"]
        assert len(error_events) == 1
        assert "boom" in error_events[0]["content"]
        assert error_events[0]["metadata"]["error_type"] == "ValueError"


class TestTokenTrackingDisabled:
    @pytest.mark.anyio
    async def test_track_token_usage_false(self):
        store = MemoryRunEventStore()
        j = RunJournal("r1", "t1", store, track_token_usage=False, flush_threshold=100)
        j.on_llm_end(
            _make_llm_response("X", usage={"input_tokens": 50, "output_tokens": 50, "total_tokens": 100}),
            run_id=uuid4(),
            tags=["lead_agent"],
        )
        data = j.get_completion_data()
        assert data["total_tokens"] == 0
        assert data["llm_call_count"] == 0


class TestConvenienceFields:
    @pytest.mark.anyio
    async def test_last_ai_message_tracks_latest(self, journal_setup):
        j, store = journal_setup
        j.on_llm_end(_make_llm_response("First"), run_id=uuid4(), tags=["lead_agent"])
        j.on_llm_end(_make_llm_response("Second"), run_id=uuid4(), tags=["lead_agent"])
        data = j.get_completion_data()
        assert data["last_ai_message"] == "Second"
        assert data["message_count"] == 2

    @pytest.mark.anyio
    async def test_first_human_message_via_set(self, journal_setup):
        j, _ = journal_setup
        j.set_first_human_message("What is AI?")
        data = j.get_completion_data()
        assert data["first_human_message"] == "What is AI?"

    @pytest.mark.anyio
    async def test_get_completion_data(self, journal_setup):
        j, _ = journal_setup
        j._total_tokens = 100
        j._msg_count = 5
        data = j.get_completion_data()
        assert data["total_tokens"] == 100
        assert data["message_count"] == 5


class TestUnknownCallerTokens:
    @pytest.mark.anyio
    async def test_unknown_caller_tokens_go_to_lead(self, journal_setup):
        j, store = journal_setup
        j.on_llm_end(
            _make_llm_response("X", usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            run_id=uuid4(),
            tags=[],
        )
        assert j._lead_agent_tokens == 15


# ---------------------------------------------------------------------------
# SQLite-backed end-to-end test
# ---------------------------------------------------------------------------


class TestDbBackedLifecycle:
    @pytest.mark.anyio
    async def test_full_lifecycle_with_sqlite(self, tmp_path):
        """Full lifecycle with SQLite-backed RunRepository + DbRunEventStore."""
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
        from deerflow.persistence.repositories.run_repo import RunRepository
        from deerflow.runtime.events.store.db import DbRunEventStore
        from deerflow.runtime.runs.manager import RunManager

        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        sf = get_session_factory()

        run_store = RunRepository(sf)
        event_store = DbRunEventStore(sf)
        mgr = RunManager(store=run_store)

        # Create run
        record = await mgr.create("t1", "lead_agent")
        run_id = record.run_id

        # Write human_message
        await event_store.put(thread_id="t1", run_id=run_id, event_type="human_message", category="message", content={"role": "user", "content": "Hello DB"})

        # Simulate journal
        journal = RunJournal(run_id, "t1", event_store, flush_threshold=100)
        journal.set_first_human_message("Hello DB")

        journal.on_chain_start({}, {}, run_id=uuid4(), parent_run_id=None)
        llm_rid = uuid4()
        journal.on_llm_start({"name": "test"}, [], run_id=llm_rid, tags=["lead_agent"])
        journal.on_llm_end(
            _make_llm_response("DB response", usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            run_id=llm_rid,
            tags=["lead_agent"],
        )
        journal.on_chain_end({}, run_id=uuid4(), parent_run_id=None)
        await asyncio.sleep(0.05)
        await journal.flush()

        # Verify run persisted
        row = await run_store.get(run_id)
        assert row is not None
        assert row["status"] == "pending"

        # Update completion
        completion = journal.get_completion_data()
        await run_store.update_run_completion(run_id, status="success", **completion)
        row = await run_store.get(run_id)
        assert row["status"] == "success"
        assert row["total_tokens"] == 15

        # Verify messages from DB
        messages = await event_store.list_messages("t1")
        assert len(messages) == 2
        assert messages[0]["event_type"] == "human_message"
        assert messages[1]["event_type"] == "ai_message"
        assert messages[1]["content"] == {"role": "assistant", "content": "DB response"}

        # Verify events from DB
        events = await event_store.list_events("t1", run_id)
        event_types = {e["event_type"] for e in events}
        assert "run_start" in event_types
        assert "llm_end" in event_types
        assert "run_end" in event_types

        await close_engine()


class TestDictContentFlag:
    """Verify that content_is_dict metadata flag controls deserialization."""

    @pytest.mark.anyio
    async def test_db_store_str_starting_with_brace_not_deserialized(self, tmp_path):
        """Plain string content starting with { should NOT be deserialized."""
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
        from deerflow.runtime.events.store.db import DbRunEventStore

        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        sf = get_session_factory()
        store = DbRunEventStore(sf)

        record = await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="tool_end",
            category="trace",
            content="{not json, just a string}",
        )
        events = await store.list_events("t1", "r1")
        assert events[0]["content"] == "{not json, just a string}"
        assert isinstance(events[0]["content"], str)

        await close_engine()

    @pytest.mark.anyio
    async def test_db_store_str_starting_with_bracket_not_deserialized(self, tmp_path):
        """Plain string content like '[1, 2, 3]' should NOT be deserialized."""
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
        from deerflow.runtime.events.store.db import DbRunEventStore

        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        sf = get_session_factory()
        store = DbRunEventStore(sf)

        record = await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="tool_end",
            category="trace",
            content="[1, 2, 3]",
        )
        events = await store.list_events("t1", "r1")
        assert events[0]["content"] == "[1, 2, 3]"
        assert isinstance(events[0]["content"], str)

        await close_engine()


class TestDictContent:
    """Verify that store backends accept str | dict content."""

    @pytest.mark.anyio
    async def test_memory_store_dict_content(self):
        store = MemoryRunEventStore()
        record = await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="ai_message",
            category="message",
            content={"role": "assistant", "content": "Hello"},
        )
        assert record["content"] == {"role": "assistant", "content": "Hello"}
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"] == {"role": "assistant", "content": "Hello"}

    @pytest.mark.anyio
    async def test_memory_store_str_content_unchanged(self):
        store = MemoryRunEventStore()
        record = await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="ai_message",
            category="message",
            content="plain string",
        )
        assert record["content"] == "plain string"
        assert isinstance(record["content"], str)

    @pytest.mark.anyio
    async def test_db_store_dict_content_roundtrip(self, tmp_path):
        """Dict content survives DB roundtrip (JSON serialize on write, deserialize on read)."""
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
        from deerflow.runtime.events.store.db import DbRunEventStore

        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        sf = get_session_factory()
        store = DbRunEventStore(sf)

        nested = {"role": "assistant", "content": "Hi", "metadata": {"model": "gpt-4", "tokens": [1, 2, 3]}}
        record = await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="ai_message",
            category="message",
            content=nested,
        )
        assert record["content"] == nested

        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"] == nested

        await close_engine()

    @pytest.mark.anyio
    async def test_db_store_trace_dict_truncation(self, tmp_path):
        """Large dict trace content is truncated with metadata flag."""
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
        from deerflow.runtime.events.store.db import DbRunEventStore

        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        sf = get_session_factory()
        store = DbRunEventStore(sf, max_trace_content=100)

        large_dict = {"role": "assistant", "content": "x" * 200}
        record = await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="llm_end",
            category="trace",
            content=large_dict,
        )
        assert record["metadata"].get("content_truncated") is True
        # Content should be a truncated string (serialized JSON was too long)
        assert isinstance(record["content"], str)
        assert len(record["content"]) <= 100

        await close_engine()


class TestOpenAIHumanMessage:
    @pytest.mark.anyio
    async def test_human_message_openai_format(self):
        store = MemoryRunEventStore()
        await store.put(
            thread_id="t1",
            run_id="r1",
            event_type="human_message",
            category="message",
            content={"role": "user", "content": "What is AI?"},
            metadata={"message_id": "msg_001"},
        )
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"] == {"role": "user", "content": "What is AI?"}
        assert messages[0]["content"]["role"] == "user"


class TestOpenAIMessageFormat:
    @pytest.mark.anyio
    async def test_ai_message_openai_format(self, journal_setup):
        """ai_message content should be OpenAI assistant message dict."""
        j, store = journal_setup
        j.on_llm_end(_make_llm_response("Answer"), run_id=uuid4(), tags=["lead_agent"])
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"] == {"role": "assistant", "content": "Answer"}

    @pytest.mark.anyio
    async def test_ai_tool_call_event(self, journal_setup):
        """LLM response with tool_calls should produce ai_tool_call message event."""
        j, store = journal_setup
        tool_calls = [{"id": "call_1", "name": "search", "args": {"query": "test"}}]
        j.on_llm_end(
            _make_llm_response("Let me search", tool_calls=tool_calls),
            run_id=uuid4(),
            tags=["lead_agent"],
        )
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["event_type"] == "ai_tool_call"
        assert messages[0]["content"]["role"] == "assistant"
        assert messages[0]["content"]["content"] == "Let me search"
        assert len(messages[0]["content"]["tool_calls"]) == 1
        tc = messages[0]["content"]["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"

    @pytest.mark.anyio
    async def test_ai_tool_call_only_from_lead_agent(self, journal_setup):
        """ai_tool_call should only be emitted for lead_agent, not subagents."""
        j, store = journal_setup
        tool_calls = [{"id": "call_1", "name": "search", "args": {}}]
        j.on_llm_end(
            _make_llm_response("searching", tool_calls=tool_calls),
            run_id=uuid4(),
            tags=["subagent:research"],
        )
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 0


class TestToolResultMessage:
    @pytest.mark.anyio
    async def test_tool_end_produces_tool_result_message(self, journal_setup):
        j, store = journal_setup
        run_id = uuid4()
        j.on_tool_start({"name": "web_search"}, '{"query": "test"}', run_id=run_id, tool_call_id="call_abc")
        j.on_tool_end("search results here", run_id=run_id, name="web_search", tool_call_id="call_abc")
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["event_type"] == "tool_result"
        assert messages[0]["content"] == {
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "search results here",
        }

    @pytest.mark.anyio
    async def test_tool_result_missing_tool_call_id(self, journal_setup):
        j, store = journal_setup
        run_id = uuid4()
        j.on_tool_start({"name": "bash"}, "ls", run_id=run_id)
        j.on_tool_end("file1.txt", run_id=run_id, name="bash")
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"]["role"] == "tool"

    @pytest.mark.anyio
    async def test_tool_error_no_tool_result_message(self, journal_setup):
        j, store = journal_setup
        j.on_tool_error(TimeoutError("timeout"), run_id=uuid4(), name="web_fetch", tool_call_id="call_1")
        await j.flush()
        messages = await store.list_messages("t1")
        assert len(messages) == 0
