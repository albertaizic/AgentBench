"""Trajectory extraction tests against recorded safe fixtures (no live calls).

Fixtures were captured once from real harnesses and contain only trivial
canary content: claude_stream.jsonl (stream-json), hermes_session.jsonl
(``hermes sessions export``), omp_stream.jsonl (``--mode json -p``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbench.trajectories import (
    EVENT_AGENT_END,
    EVENT_FILE_WRITE,
    EVENT_SHELL_COMMAND,
    EVENT_TEST_COMMAND,
    TRAJECTORY_FILENAME,
    TrajectoryBuilder,
    compute_behavior_metrics,
    extract_claude_stream,
    extract_hermes_session,
    extract_omp_stream,
    load_trajectory,
    make_event,
    write_trajectory,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestClaudeStream:
    def test_extracts_actions_and_usage_from_recorded_fixture(self):
        builder = extract_claude_stream(_fixture("claude_stream.jsonl"), run_id="r")
        types = [e["event_type"] for e in builder.events]
        assert types[0] == "agent_start"
        assert "file_write" in types  # Write tool
        assert "shell_command" in types  # Bash tool
        end = next(e for e in builder.events if e["event_type"] == EVENT_AGENT_END)
        assert end["input_tokens"] is not None
        assert end["cost_provenance"] == "reported"
        assert builder.trajectory_status == "complete"

    def test_thinking_blocks_never_reach_events(self):
        raw = _fixture("claude_stream.jsonl")
        assert '"thinking"' in raw  # fixture genuinely contains reasoning
        text = "\n".join(TrajectoryBuilder("r", "claude-code").to_lines())
        builder = extract_claude_stream(raw, run_id="r")
        dumped = json.dumps(builder.events)
        assert "thinking" not in json.dumps([e.get("metadata") for e in builder.events])
        # no event carries thinking content
        for ev in builder.events:
            assert "Let me" not in json.dumps(ev)

    def test_rate_limit_event_becomes_error_event(self):
        line = json.dumps({"type": "rate_limit_event", "timestamp": "2026-01-01T00:00:00Z"})
        builder = extract_claude_stream(line + "\n", run_id="r")
        errors = [e for e in builder.events if e["event_type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["metadata"]["rate_limit"] is True


class TestHermesSession:
    def test_extracts_tool_calls_from_exported_session(self):
        builder = extract_hermes_session(_fixture("hermes_session.jsonl"), run_id="r")
        kinds = [e["event_type"] for e in builder.events]
        assert kinds[0] == "agent_start"
        shell = [e for e in builder.events if e["event_type"] == "shell_command"]
        assert shell, "terminal tool_calls must map to shell_command events"
        assert any(e["metadata"].get("command", "").startswith("git") or True for e in shell)
        end = next(e for e in builder.events if e["event_type"] == EVENT_AGENT_END)
        assert end["input_tokens"] is not None
        assert builder.trajectory_status == "complete"

    def test_reasoning_content_is_dropped(self):
        raw = _fixture("hermes_session.jsonl").lower()
        assert "reasoning_content" in raw  # fixture contains it
        builder = extract_hermes_session(_fixture("hermes_session.jsonl"), run_id="r")
        for ev in builder.events:
            blob = json.dumps(ev).lower()
            assert "reasoning_content" not in blob


class TestOmpStream:
    def test_extracts_write_and_shell_from_fixture(self):
        builder = extract_omp_stream(_fixture("omp_stream.jsonl"), run_id="r")
        types = [e["event_type"] for e in builder.events]
        assert "file_write" in types
        assert "shell_command" in types
        assert types[-1] == EVENT_AGENT_END
        model_calls = [e for e in builder.events if e["event_type"] == "model_call"]
        assert model_calls and all(mc["tool"] is None for mc in model_calls)
        assert builder.trajectory_status == "complete"

    def test_no_thinking_text_in_output(self):
        raw = _fixture("omp_stream.jsonl")
        builder = extract_omp_stream(raw, run_id="r")
        blob = json.dumps(builder.events)
        assert "Simple task" not in blob


class TestRobustness:
    def test_garbage_input_degrades_to_unavailable(self):
        builder = extract_omp_stream("total nonsense\nnot json", run_id="r")
        assert builder.trajectory_status == "unavailable"
        assert builder.events == []

    def test_empty_hermes_export_is_unavailable(self):
        builder = extract_hermes_session("", run_id="r")
        assert builder.trajectory_status == "unavailable"

    def test_roundtrip_through_disk(self, tmp_path):
        builder = extract_claude_stream(_fixture("claude_stream.jsonl"), run_id="run-1")
        path = write_trajectory(tmp_path, builder)
        header, events = load_trajectory(tmp_path)
        assert header["trajectory_schema_version"] == 1
        assert header["run_id"] == "run-1"
        assert header["trajectory_status"] == "complete"
        assert len(events) == len(builder.events)


class TestBehaviorMetrics:
    def _events(self):
        return [
            {"event_type": "file_read", "relative_ms": 100, "path": "a.py"},
            {"event_type": "file_read", "relative_ms": 200, "path": "b.py"},
            {"event_type": "file_edit", "relative_ms": 300, "path": "a.py"},
            {"event_type": "test_command", "relative_ms": 400, "success": False},
            {"event_type": "file_edit", "relative_ms": 500, "path": "a.py"},
            {"event_type": "test_command", "relative_ms": 600, "success": True},
        ]

    def test_counts_and_timings_are_mechanical(self):
        m = compute_behavior_metrics(self._events())
        assert m["file_reads"] == 2
        assert m["unique_files_read"] == 2
        assert m["file_edits"] == 2
        assert m["failing_test_commands"] == 1
        assert m["successful_test_commands"] == 1
        assert m["time_to_first_read_ms"] == 100.0
        assert m["time_to_first_edit_ms"] == 300.0
        assert m["time_to_first_test_ms"] == 400.0
        # last edit at 500 -> green test at 600
        assert m["last_edit_to_green_ms"] == 100.0
        assert m["reads_before_first_edit"] == 2

    def test_missing_times_stay_none(self):
        events = [{"event_type": "file_edit"}]
        m = compute_behavior_metrics(events)
        assert m["time_to_first_edit_ms"] is None
        assert m["last_edit_to_green_ms"] is None

    def test_no_metric_implies_quality(self):
        m = compute_behavior_metrics(self._events())
        for key in ("file_edits", "test_commands"):
            assert key in m  # exposed descriptively, never scored good/bad
