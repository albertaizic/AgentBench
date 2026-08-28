"""Normalized agent-trajectory evidence (v0.6).

Raw ``agent.stdout.log`` / ``agent.stderr.log`` stay preserved byte-for-byte;
this module adds a *normalized*, privacy-aware view of externally observable
agent activity next to them as ``trajectory.jsonl``.

Design rules:

* **No fabrication** — events exist only for what a harness actually exposes.
  If structured output does not describe an event, nothing is invented.
* **No private reasoning** — provider chain-of-thought / reasoning deltas are
  dropped at parse time. Only observable actions (tool calls, commands, file
  operations, usage) are normalized.
* **Provenance on every event** — ``native`` (harness emitted the fact
  directly), ``parsed_stdout``, ``parsed_structured_log``, or ``inferred``
  (derived mechanically from other events).
* **Never fatal** — extraction problems degrade ``trajectory_status``; they
  can never invalidate the underlying benchmark run.

File layout: line 1 is a header object carrying ``trajectory_schema_version``
and run identity; every following line is one event object.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRAJECTORY_SCHEMA_VERSION = 1
TRAJECTORY_FILENAME = "trajectory.jsonl"

#: Event vocabulary (P3). Adapters map native concepts onto these; anything
#: unmatched stays ``unknown`` rather than being force-fit.
EVENT_AGENT_START = "agent_start"
EVENT_AGENT_END = "agent_end"
EVENT_MODEL_CALL = "model_call"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_SHELL_COMMAND = "shell_command"
EVENT_FILE_READ = "file_read"
EVENT_FILE_WRITE = "file_write"
EVENT_FILE_EDIT = "file_edit"
EVENT_TEST_COMMAND = "test_command"
EVENT_GIT_COMMAND = "git_command"
EVENT_SEARCH = "search"
EVENT_ERROR = "error"
EVENT_RETRY = "retry"
EVENT_CHECKPOINT = "checkpoint"
EVENT_MESSAGE = "message"
EVENT_UNKNOWN = "unknown"
ALL_EVENT_TYPES = (
    EVENT_AGENT_START, EVENT_AGENT_END, EVENT_MODEL_CALL, EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT, EVENT_SHELL_COMMAND, EVENT_FILE_READ, EVENT_FILE_WRITE,
    EVENT_FILE_EDIT, EVENT_TEST_COMMAND, EVENT_GIT_COMMAND, EVENT_SEARCH,
    EVENT_ERROR, EVENT_RETRY, EVENT_CHECKPOINT, EVENT_MESSAGE, EVENT_UNKNOWN,
)

PROVENANCE_NATIVE = "native"
PROVENANCE_PARSED_STDOUT = "parsed_stdout"
PROVENANCE_PARSED_STRUCTURED_LOG = "parsed_structured_log"
PROVENANCE_INFERRED = "inferred"

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
STATUS_PARSE_FAILED = "parse_failed"
ALL_STATUSES = (
    STATUS_COMPLETE, STATUS_PARTIAL, STATUS_UNAVAILABLE, STATUS_PARSE_FAILED,
)

_TEST_MARKERS = ("pytest", "npm test", "npx vitest", "vitest", "unittest",
                 "python -m unittest", "go test", "cargo test")
_GIT_PREFIX = "git "


def _command_category(command: str | None) -> str | None:
    """Classify a shell command: test / git / generic shell."""
    if not command:
        return None
    lowered = command.lower()
    if any(marker in lowered for marker in _TEST_MARKERS):
        return "test"
    if lowered.lstrip().startswith(_GIT_PREFIX):
        return "git"
    return "shell"


def _event_type_for_tool(tool: str | None, category: str | None) -> str:
    tool_l = (tool or "").lower()
    if tool_l in ("bash", "shell", "terminal", "powershell"):
        if category == "test":
            return EVENT_TEST_COMMAND
        if category == "git":
            return EVENT_GIT_COMMAND
        return EVENT_SHELL_COMMAND
    if tool_l in ("read", "readfile"):
        return EVENT_FILE_READ
    if tool_l == "write":
        return EVENT_FILE_WRITE
    if tool_l in ("edit", "multiedit", "notebookedit"):
        return EVENT_FILE_EDIT
    if tool_l in ("grep", "glob", "search"):
        return EVENT_SEARCH
    return EVENT_TOOL_CALL


def make_event(
    *,
    event_type: str,
    source: str,
    provenance: str,
    timestamp: str | None = None,
    relative_ms: float | None = None,
    tool: str | None = None,
    path: str | None = None,
    exit_code: int | None = None,
    success: bool | None = None,
    duration_ms: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    cost_provenance: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One normalized event; unknown facts stay ``None`` (never guessed)."""
    command_category = (
        _command_category(metadata.get("command")) if metadata else None
    )
    resolved_type = (
        event_type
        if event_type != EVENT_TOOL_CALL
        else _event_type_for_tool(tool, command_category)
    )
    return {
        "timestamp": timestamp,
        "relative_ms": relative_ms,
        "event_type": resolved_type,
        "source": source,
        "provenance": provenance,
        "tool": tool,
        "command_category": command_category,
        "path": path,
        "exit_code": exit_code,
        "success": success,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "cost_provenance": cost_provenance,
        "metadata": metadata or {},
    }


@dataclass
class TrajectoryBuilder:
    """Accumulates events and renders the JSONL document."""

    run_id: str
    agent_type: str
    trajectory_status: str = STATUS_PARTIAL
    events: list[dict[str, Any]] = field(default_factory=list)
    _t0: float | None = None

    def add(self, ev: dict[str, Any]) -> None:
        ts = ev.get("timestamp")
        parsed = self._epoch(ts)
        if parsed is None:
            self.events.append(ev)
            return
        if self._t0 is None or parsed < self._t0:
            # Streams may deliver timestamps out of order (retries, buffered
            # flushes). Re-anchor to the earliest observation and recompute
            # stored relatives — negative event times would poison metrics
            # like time_to_first_edit.
            self._t0 = parsed if self._t0 is None else min(self._t0, parsed)
            for prior in self.events:
                base = self._epoch(prior.get("timestamp"))
                if base is not None:
                    prior["relative_ms"] = round((base - self._t0) * 1000, 1)
        ev["relative_ms"] = round((parsed - self._t0) * 1000, 1)
        self.events.append(ev)

    @staticmethod
    def _epoch(ts: Any) -> float | None:
        """Parse epoch seconds or ISO-8601/Z timestamps; None otherwise."""
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return float(ts)
        if isinstance(ts, str) and ts.endswith("Z"):
            from datetime import datetime

            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        return None

    def to_lines(self) -> list[str]:
        header = {
            "trajectory_schema_version": TRAJECTORY_SCHEMA_VERSION,
            "run_id": self.run_id,
            "agent_type": self.agent_type,
            "trajectory_status": self.trajectory_status,
            "event_count": len(self.events),
        }
        return [
            json.dumps(header, ensure_ascii=False),
            *(json.dumps(ev, ensure_ascii=False) for ev in self.events),
        ]


def write_trajectory(run_dir: Path, builder: TrajectoryBuilder) -> Path:
    target = Path(run_dir) / TRAJECTORY_FILENAME
    target.write_text("\n".join(builder.to_lines()) + "\n", encoding="utf-8", newline="\n")
    return target


def load_trajectory(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a persisted trajectory; returns (header, events)."""
    path = Path(run_dir) / TRAJECTORY_FILENAME
    if not path.exists():
        return {}, []
    header: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if i == 0 and "trajectory_schema_version" in obj:
            header = obj
        else:
            events.append(obj)
    return header, events


# -- Claude Code (stream-json) -------------------------------------------------
#
# ``--print --output-format stream-json --verbose`` emits one JSON object per
# line: system/init, assistant messages (content blocks), user messages whose
# blocks carry tool_results, rate_limit_event notices, and a final ``result``
# envelope identical to the classic single-object format.

_CLAUDE_TOOL_CATEGORY = {
    "Bash": "shell", "PowerShell": "shell", "Read": "read", "Write": "write",
    "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Grep": "search", "Glob": "search",
}


def extract_claude_stream(stdout_text: str, *, run_id: str) -> TrajectoryBuilder:
    builder = TrajectoryBuilder(run_id=run_id, agent_type="claude-code")
    saw_stream_frame = False
    final_result: dict[str, Any] | None = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        saw_stream_frame = True
        kind = rec.get("type")
        if kind == "system" and rec.get("subtype") == "init":
            builder.add(make_event(
                event_type=EVENT_AGENT_START, source="claude-code",
                provenance=PROVENANCE_PARSED_STDOUT, timestamp=rec.get("timestamp"),
                metadata={"session_id": rec.get("session_id"),
                          "tools": rec.get("tools")},
            ))
        elif kind == "rate_limit_event":
            builder.add(make_event(
                event_type=EVENT_ERROR, source="claude-code",
                provenance=PROVENANCE_PARSED_STDOUT, timestamp=rec.get("timestamp"),
                metadata={"rate_limit": True, "payload": rec},
            ))
        elif kind == "assistant":
            message = rec.get("message") or {}
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "thinking":
                    continue  # private model reasoning is never exported
                if btype == "tool_use":
                    tool = str(block.get("name") or "")
                    args = block.get("input") or {}
                    md: dict[str, Any] = {"tool_use_id": block.get("id")}
                    if isinstance(args, dict):
                        md.update({
                            k: args[k] for k in ("command", "file_path", "pattern")
                            if k in args
                        })
                    builder.add(make_event(
                        event_type=EVENT_TOOL_CALL,
                        source="claude-code",
                        provenance=PROVENANCE_PARSED_STDOUT,
                        timestamp=message.get("timestamp"),
                        tool=_CLAUDE_TOOL_CATEGORY.get(tool, tool),
                        path=(args or {}).get("file_path") if isinstance(args, dict) else None,
                        metadata=md,
                    ))
                    builder.add(make_event(
                        event_type=EVENT_MODEL_CALL, source="claude-code",
                        provenance=PROVENANCE_INFERRED,
                        timestamp=message.get("timestamp"),
                        metadata={"emitted_tool_call": True},
                    ))
                # plain text blocks are the visible answer; not events
        elif kind == "user":
            message = rec.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    builder.add(make_event(
                        event_type=EVENT_TOOL_RESULT, source="claude-code",
                        provenance=PROVENANCE_PARSED_STDOUT,
                        timestamp=message.get("timestamp"),
                        success=block.get("is_error") is not True,
                        metadata={"tool_use_id": block.get("tool_use_id")},
                    ))
        elif kind == "result":
            final_result = rec
    if final_result is not None:
        usage = final_result.get("usage") or {}
        total = None
        if isinstance(usage.get("input_tokens"), (int, float)) and \
                isinstance(usage.get("output_tokens"), (int, float)):
            total = (
                usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
        mu = final_result.get("modelUsage") or {}
        main_model, best_cost = None, -1.0
        for name, stats in mu.items():
            cost = float(stats.get("costUSD") or 0.0) if isinstance(stats, dict) else 0.0
            if cost > best_cost:
                main_model, best_cost = name, cost
        builder.add(make_event(
            event_type=EVENT_AGENT_END, source="claude-code",
            provenance=PROVENANCE_PARSED_STDOUT,
            success=final_result.get("is_error") is False,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_usd=final_result.get("total_cost_usd"),
            cost_provenance="reported" if final_result.get("total_cost_usd") is not None else None,
            metadata={"total_tokens": total, "main_model": main_model,
                      "num_turns": final_result.get("num_turns"),
                      "session_id": final_result.get("session_id")},
        ))
    builder.trajectory_status = STATUS_COMPLETE if saw_stream_frame and final_result else (
        STATUS_PARSE_FAILED if not saw_stream_frame else STATUS_PARTIAL
    )
    return builder


# -- Hermes (session export JSONL) ---------------------------------------------
#
# ``hermes sessions export --format jsonl --session-id <id> -`` writes one
# session record whose ``messages`` array holds the conversation. Assistant
# turns carry ``tool_calls``; tool turns carry results. Reasoning content is
# dropped unconditionally.

_HERMES_TOOL_MAP = {"terminal": "bash", "shell": "bash"}


def extract_hermes_session(export_text: str, *, run_id: str) -> TrajectoryBuilder:
    builder = TrajectoryBuilder(run_id=run_id, agent_type="hermes")
    records = []
    for line in export_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    if not records:
        builder.trajectory_status = STATUS_UNAVAILABLE
        return builder
    session = records[0]
    builder.add(make_event(
        event_type=EVENT_AGENT_START, source="hermes",
        provenance=PROVENANCE_PARSED_STRUCTURED_LOG,
        timestamp=session.get("started_at"),
        metadata={"session_id": session.get("id"),
                  "model": session.get("model"),
                  "provider": session.get("provider")},
    ))
    for msg in session.get("messages") or []:
        role = msg.get("role")
        ts = msg.get("timestamp")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                fn = (call.get("function") or {})
                raw_args = fn.get("arguments")
                args: dict[str, Any] = {}
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except ValueError:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                tool_raw = str(fn.get("name") or "")
                builder.add(make_event(
                    event_type=EVENT_TOOL_CALL, source="hermes",
                    provenance=PROVENANCE_PARSED_STRUCTURED_LOG, timestamp=ts,
                    tool=_HERMES_TOOL_MAP.get(tool_raw, tool_raw),
                    path=args.get("path") if isinstance(args, dict) else None,
                    metadata={"command": args.get("command")}
                    if isinstance(args, dict) and "command" in args else {},
                ))
                builder.add(make_event(
                    event_type=EVENT_MODEL_CALL, source="hermes",
                    provenance=PROVENANCE_INFERRED, timestamp=ts,
                    input_tokens=msg.get("token_count"),
                    metadata={"emitted_tool_call": True},
                ))
        elif role == "tool":
            builder.add(make_event(
                event_type=EVENT_TOOL_RESULT, source="hermes",
                provenance=PROVENANCE_PARSED_STRUCTURED_LOG, timestamp=ts,
                tool=_HERMES_TOOL_MAP.get(str(msg.get("tool_name") or ""), msg.get("tool_name")),
                success=None,  # hermes does not expose per-call exit status here
                metadata={"tool_call_id": msg.get("tool_call_id")},
            ))
        elif role == "user":
            builder.add(make_event(
                event_type=EVENT_MESSAGE, source="hermes",
                provenance=PROVENANCE_PARSED_STRUCTURED_LOG, timestamp=ts,
                metadata={"role": "user"},
            ))
    builder.add(make_event(
        event_type=EVENT_AGENT_END, source="hermes",
        provenance=PROVENANCE_PARSED_STRUCTURED_LOG,
        timestamp=session.get("ended_at"),
        success=session.get("failed") is False,
        input_tokens=session.get("input_tokens"),
        output_tokens=session.get("output_tokens"),
        cost_usd=session.get("actual_cost_usd"),
        cost_provenance=(
            f"{session.get('cost_source')}/{session.get('cost_status')}"
            if session.get("cost_source") or session.get("cost_status") else None
        ),
        metadata={
            "session_id": session.get("id"),
            "api_calls": session.get("api_call_count"),
            "end_reason": session.get("end_reason"),
        },
    ))
    builder.trajectory_status = STATUS_COMPLETE
    return builder


# -- OMP (--mode json print stream) --------------------------------------------
#
# OMP prints one JSON object per line directly to stdout: session, agent/
# turn markers, message frames (with per-message usage), tool_execution_
# start/end pairs. Thinking blocks/deltas are skipped entirely.

_OMP_TOOL_MAP = {
    "bash": "bash", "shell": "bash", "write": "write", "edit": "edit",
    "read": "read", "grep": "search", "glob": "search", "ls": "read",
}


def extract_omp_stream(stdout_text: str, *, run_id: str) -> TrajectoryBuilder:
    builder = TrajectoryBuilder(run_id=run_id, agent_type="omp")
    saw_frame = False
    pending: dict[str, dict[str, Any]] = {}
    agent_ended = False
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        saw_frame = True
        kind = rec.get("type")
        if kind == "session":
            builder.add(make_event(
                event_type=EVENT_AGENT_START, source="omp",
                provenance=PROVENANCE_PARSED_STDOUT, timestamp=rec.get("timestamp"),
                metadata={"session_id": rec.get("id"), "cwd": rec.get("cwd")},
            ))
        elif kind == "message_end":
            message = rec.get("message") or {}
            role = message.get("role")
            usage = message.get("usage") or {}
            if isinstance(usage, dict) and usage.get("totalTokens"):
                builder.add(make_event(
                    event_type=EVENT_MODEL_CALL, source="omp",
                    provenance=PROVENANCE_PARSED_STDOUT,
                    timestamp=message.get("timestamp"),
                    input_tokens=usage.get("input"),
                    output_tokens=usage.get("output"),
                    cost_usd=((usage.get("cost") or {}).get("total", 0.0) or None)
                    if isinstance(usage.get("cost"), dict) else None,
                    metadata={"model": message.get("model"),
                              "provider": message.get("provider")},
                ))
            if role == "assistant":
                for block in message.get("content") or []:
                    if block.get("type") == "toolCall":
                        tool_raw = str(block.get("name") or "")
                        args = block.get("arguments") or {}
                        builder.add(make_event(
                            event_type=EVENT_TOOL_CALL, source="omp",
                            provenance=PROVENANCE_PARSED_STDOUT,
                            timestamp=message.get("timestamp"),
                            tool=_OMP_TOOL_MAP.get(tool_raw, tool_raw),
                            path=args.get("path") if isinstance(args, dict) else None,
                            metadata={"command": args.get("command")}
                            if isinstance(args, dict) and "command" in args else {},
                        ))
        elif kind == "tool_execution_start":
            tool_raw = str(rec.get("toolName") or "")
            key = str(rec.get("toolCallId"))
            args = rec.get("args") or {}
            pending[key] = {
                "tool": _OMP_TOOL_MAP.get(tool_raw, tool_raw),
                "args": args if isinstance(args, dict) else {},
                "ts": rec.get("timestamp"),
            }
            builder.add(make_event(
                event_type=EVENT_TOOL_CALL, source="omp",
                provenance=PROVENANCE_PARSED_STDOUT, timestamp=rec.get("timestamp"),
                tool=pending[key]["tool"],
                path=pending[key]["args"].get("path"),
                metadata={"command": pending[key]["args"].get("command")}
                if "command" in pending[key]["args"] else {},
            ))
        elif kind == "tool_execution_end":
            key = str(rec.get("toolCallId"))
            info = pending.pop(key, {})
            result = rec.get("result") or {}
            details = result.get("details") or {} if isinstance(result, dict) else {}
            builder.add(make_event(
                event_type=EVENT_TOOL_RESULT, source="omp",
                provenance=PROVENANCE_PARSED_STDOUT, timestamp=rec.get("timestamp"),
                tool=str(rec.get("toolName") or ""),
                exit_code=details.get("exitCode") if isinstance(details, dict) else None,
                success=(details.get("error") is None) if isinstance(details, dict) else None,
                duration_ms=(
                    details.get("wallTimeMs") if isinstance(details, dict) else None
                ),
                metadata={"tool_call_id": key},
            ))
        elif kind == "agent_end":
            agent_ended = True
            builder.add(make_event(
                event_type=EVENT_AGENT_END, source="omp",
                provenance=PROVENANCE_PARSED_STDOUT, timestamp=rec.get("timestamp"),
                success=True,
                metadata={},
            ))
    builder.trajectory_status = (
        STATUS_COMPLETE if saw_frame and agent_ended
        else STATUS_PARTIAL if saw_frame
        else STATUS_UNAVAILABLE
    )
    return builder


# -- dispatch -------------------------------------------------------------------

def extract_trajectory(
    agent_type: str,
    *,
    claude_stdout: str | None = None,
    hermes_export: str | None = None,
    omp_stdout: str | None = None,
    run_id: str = "?",
) -> TrajectoryBuilder:
    """Extract with full isolation: a parser bug degrades to parse_failed."""
    try:
        if agent_type == "claude-code" and claude_stdout is not None:
            return extract_claude_stream(claude_stdout, run_id=run_id)
        if agent_type == "hermes" and hermes_export is not None:
            return extract_hermes_session(hermes_export, run_id=run_id)
        if agent_type == "omp" and omp_stdout is not None:
            return extract_omp_stream(omp_stdout, run_id=run_id)
    except Exception:  # noqa: BLE001 - trajectory must never break a run
        builder = TrajectoryBuilder(run_id=run_id, agent_type=agent_type)
        builder.trajectory_status = STATUS_PARSE_FAILED
        return builder
    builder = TrajectoryBuilder(run_id=run_id, agent_type=agent_type)
    builder.trajectory_status = STATUS_UNAVAILABLE
    return builder


# -- behavioral metrics (P5) ----------------------------------------------------
#
# Purely descriptive. Nothing here implies "better"; e.g. fewer edits is not
# inherently good and more tests is not inherently good.

def _iter(events: list[dict], *types: str):
    want = set(types)
    for ev in events:
        if ev.get("event_type") in want:
            yield ev


def _first_relative_ms(events, *types) -> float | None:
    for ev in _iter(events, *types):
        ms = _first_rel(ev)
        if ms is not None:
            return ms
    return None


def compute_behavior_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    tool_calls = list(_iter(events, *[t for t in ALL_EVENT_TYPES if t.startswith((
        EVENT_SHELL_COMMAND, EVENT_FILE_READ, EVENT_FILE_WRITE, EVENT_FILE_EDIT,
        EVENT_TEST_COMMAND, EVENT_GIT_COMMAND, EVENT_SEARCH))]))
    edits = list(_iter(events, EVENT_FILE_EDIT, EVENT_FILE_WRITE))
    reads = list(_iter(events, EVENT_FILE_READ))
    tests = list(_iter(events, EVENT_TEST_COMMAND))
    failing_tests = [ev for ev in tests if ev.get("success") is False]
    successful_tests = [ev for ev in tests if ev.get("success") is True]
    errors = list(_iter(events, EVENT_ERROR))

    reads_before_first_edit = sum(
        1 for ev in reads
        if _first_rel(ev) is not None and _before(ev, edits)
    )
    unique_files_read = len({ev.get("path") for ev in reads if ev.get("path")})
    unique_files_edited = len({ev.get("path") for ev in edits if ev.get("path")})

    last_edit_ms = max((_first_rel(e) for e in edits), default=None)
    last_success_test_ms = None
    for ev in reversed(successful_tests):
        ms = _first_rel(ev)
        if ms is not None:
            last_success_test_ms = ms
            break
    edit_to_green_ms = (
        round(last_success_test_ms - last_edit_ms, 1)
        if last_edit_ms is not None and last_success_test_ms is not None
        and last_success_test_ms >= last_edit_ms else None
    )

    end = next(reversed(list(_iter(events, EVENT_AGENT_END))), None)
    total_tokens = None
    if end is not None:
        it, ot = end.get("input_tokens"), end.get("output_tokens")
        if isinstance(it, (int, float)) and isinstance(ot, (int, float)):
            total_tokens = int(it + ot)

    changed_lines = sum(
        int(ev.get("metadata", {}).get("insertions") or 0) for ev in edits
    ) or None

    return {
        "total_actions": len(tool_calls) or None,
        "shell_commands": len([e for e in tool_calls if e["event_type"] == EVENT_SHELL_COMMAND]) or None,
        "file_reads": len(reads) or None,
        "file_edits": len(edits) or None,
        "unique_files_read": unique_files_read or None,
        "unique_files_edited": unique_files_edited or None,
        "test_commands": len(tests) or None,
        "failing_test_commands": len(failing_tests) or None,
        "successful_test_commands": len(successful_tests) or None,
        "time_to_first_read_ms": _first_relative_ms(events, EVENT_FILE_READ),
        "time_to_first_edit_ms": _first_relative_ms(events, EVENT_FILE_EDIT, EVENT_FILE_WRITE),
        "time_to_first_test_ms": _first_relative_ms(events, EVENT_TEST_COMMAND),
        "last_edit_to_green_ms": edit_to_green_ms,
        "tool_errors": len(errors) or None,
        "command_failures": len([e for e in _iter(events, EVENT_TOOL_RESULT)
                                 if e.get("success") is False]) or None,
        "model_turns": len(list(_iter(events, EVENT_MODEL_CALL))) or None,
        "tokens_total": total_tokens,
        "reads_before_first_edit": reads_before_first_edit or None,
        "test_after_edit_ratio": (
            round(len(successful_tests) / len(edits), 3) if edits and successful_tests else None
        ),
    }


def _first_rel(ev: dict) -> float | None:
    ms = ev.get("relative_ms")
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    # Historical trajectories may contain negative relatives from
    # out-of-order timestamps; a negative duration is meaningless, so the
    # metric reports "unavailable" rather than a fabricated number.
    return float(ms) if ms >= 0 else None


def _before(ev: dict, others: list[dict]) -> bool:
    mine = _first_rel(ev)
    if mine is None:
        return False
    other_rels = [_first_rel(o) for o in others]
    other_rels = [r for r in other_rels if r is not None]
    return not other_rels or mine < min(other_rels)
