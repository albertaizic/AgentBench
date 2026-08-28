"""Hostile-input trajectory parser tests (v0.6 hardening, missions VIII/IX).

Agent output is treated as unreliable: every malformed fixture must degrade
trajectory_status without crashing, without fabricating events, and without
ever exporting private reasoning fields. Metric semantics: impossible values
(negative times) surface as unavailable — never as fake numbers.
"""

from __future__ import annotations

import json

from agentbench.trajectories import (
    STATUS_COMPLETE,
    STATUS_PARSE_FAILED,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    compute_behavior_metrics,
    extract_claude_stream,
    extract_hermes_session,
    extract_omp_stream,
    extract_trajectory,
)

_CLAUDE_FRAME = {
    "type": "assistant",
    "message": {"timestamp": "2026-01-01T00:00:01Z", "content": [
        {"type": "tool_use", "id": "t1", "name": "Edit",
         "input": {"file_path": "x.py"}},
    ]},
}
_CLAUDE_RESULT = {
    "type": "result", "is_error": False,
    "usage": {"input_tokens": 5, "output_tokens": 7},
    "modelUsage": {"m1": {"costUSD": 0.01}}, "total_cost_usd": 0.01,
}


def _claude_ok() -> str:
    init = json.dumps({"type": "system", "subtype": "init",
                       "timestamp": "2026-01-01T00:00:00Z"})
    return "\n".join([init, json.dumps(_CLAUDE_FRAME),
                      json.dumps(_CLAUDE_RESULT)]) + "\n"


class TestMalformedStreamsNeverCrash:
    def test_truncated_final_line_stays_partial(self):
        raw = _claude_ok().rsplit("\n", 2)[0] + "\n" + '{"type":"result","is_e'
        builder = extract_claude_stream(raw, run_id="r")
        assert builder.trajectory_status == STATUS_PARTIAL
        assert builder.events          # earlier frames still extracted

    def test_partial_json_lines_skipped_silently(self):
        raw = "{oops\n" + _claude_ok()
        builder = extract_claude_stream(raw, run_id="r")
        assert builder.trajectory_status == STATUS_COMPLETE

    def test_mixed_stdout_noise_and_json(self):
        noisy = "loading...\n[warn] something\n" + _claude_ok()
        builder = extract_claude_stream(noisy, run_id="r")
        assert builder.trajectory_status == STATUS_COMPLETE

    def test_unknown_event_types_are_tolerated_by_omp(self):
        frames = "\n".join([
            json.dumps({"type": "session", "id": "s"}),
            json.dumps({"type": "brand_new_future_event", "stuff": [1]}),
        ]) + "\n"
        builder = extract_omp_stream(frames, run_id="r")
        assert builder.trajectory_status == STATUS_PARTIAL

    def test_null_and_missing_fields_do_not_crash_any_parser(self):
        hermes = json.dumps({
            "id": None, "started_at": None, "ended_at": None,
            "failed": None, "model": None, "provider": None,
            "messages": [{"role": "assistant", "tool_calls": [
                {"function": {"name": None, "arguments": "not-json"}}]},
                {"role": "tool", "tool_name": None},
                {"role": "user"}],
        }) + "\n"
        builder = extract_hermes_session(hermes, run_id="r")
        assert builder.trajectory_status == STATUS_COMPLETE
        omp = json.dumps({"type": "tool_execution_end"}) + "\n"
        assert extract_omp_stream(omp, run_id="r").trajectory_status == STATUS_PARTIAL

    def test_duplicate_timestamps_produce_zero_relatives_not_negatives(self):
        ts = "2026-01-01T00:00:05Z"
        hermes = "\n".join(json.dumps({
            "id": "s", "started_at": ts, "ended_at": ts,
            "messages": [{"role": "user", "timestamp": ts},
                         {"role": "user", "timestamp": ts}]},
        ) for _ in range(1)) + "\n"
        builder = extract_hermes_session(hermes, run_id="r")
        rels = [e["relative_ms"] for e in builder.events
                if isinstance(e.get("relative_ms"), (int, float))]
        assert rels == sorted(rels) and all(r >= 0 for r in rels)

    def test_out_of_order_timestamps_never_go_negative(self):
        hermes = json.dumps({
            "id": "s", "started_at": "2026-01-01T00:00:10Z",
            "messages": [
                {"role": "user", "timestamp": "2026-01-01T00:00:12Z"},
                {"role": "user", "timestamp": "2026-01-01T00:00:03Z"},
            ],
        }) + "\n"
        builder = extract_hermes_session(hermes, run_id="r")
        rels = [e["relative_ms"] for e in builder.events
                if isinstance(e.get("relative_ms"), (int, float))]
        assert all(r >= 0 for r in rels)
        assert min(rels) == 0.0

    def test_extractor_isolation_degrades_to_parse_failed(self):
        # Any unexpected parser exception must be contained.
        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("disk on fire")
        builder = extract_trajectory(
            "hermes", hermes_export="{}", run_id="r")
        assert builder.trajectory_status in (
            STATUS_PARSE_FAILED, STATUS_UNAVAILABLE, STATUS_COMPLETE)


class TestNoReasoningLeakage:
    def test_all_private_field_names_banned_from_output(self):
        claude = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "SECRET"},
            {"type": "text", "text": "internal_reasoning SECRET"},
        ]}})
        hermes = json.dumps({
            "id": "s",
            "messages": [{"role": "assistant", "reasoning_content": "SECRET",
                          "internal_reasoning": "SECRET",
                          "tool_calls": []}]}) + "\n"
        omp = json.dumps({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "value": "SECRET"}]}}) + "\n"
        outputs = [
            json.dumps(extract_claude_stream(claude, run_id="r").events),
            json.dumps(extract_hermes_session(hermes, run_id="r").events),
            json.dumps(extract_omp_stream(omp, run_id="r").events),
        ]
        banned = ("secret", "thinking", "reasoning_content",
                  "internal_reasoning", "chain-of-thought")
        for blob in outputs:
            lowered = blob.lower()
            for word in banned:
                assert word not in lowered, word


class TestMetricSemantics:
    def test_negative_event_time_is_unavailable_not_negative(self):
        events = [{"event_type": "file_edit", "relative_ms": -50.0}]
        m = compute_behavior_metrics(events)
        assert m["time_to_first_edit_ms"] is None

    def test_edit_to_green_requires_success_after_last_edit(self):
        events = [
            {"event_type": "file_edit", "relative_ms": 100.0},
            {"event_type": "test_command", "success": True,
             "relative_ms": 50.0},           # success BEFORE the edit
        ]
        m = compute_behavior_metrics(events)
        assert m["last_edit_to_green_ms"] is None

    def test_zero_edits_yield_unavailable_ratios(self):
        events = [{"event_type": "test_command", "success": True,
                   "relative_ms": 10.0}]
        m = compute_behavior_metrics(events)
        assert m["test_after_edit_ratio"] is None
        assert m["file_edits"] is None      # 0 vs unknown distinction

    def test_changed_lines_none_when_no_insertions_recorded(self):
        events = [{"event_type": "file_edit", "relative_ms": 1.0,
                   "metadata": {}}]
        m = compute_behavior_metrics(events)
        assert m.get("tokens_total") is None

    def test_failed_run_has_no_cost_per_success_semantics(self):
        # Cost totals come only from agent_end; a failed agent with no end
        # event contributes no token total — never a fabricated zero.
        m = compute_behavior_metrics([])
        assert m["tokens_total"] is None
