"""Machine-readable export of run rows (CSV/JSON).

Exports contain flattened, safe metrics only. Raw stdout/stderr and patches
stay in the per-run sidecar artifacts; secrets never enter the index in the
first place.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

EXPORT_COLUMNS = [
    "run_id",
    "experiment_id",
    "config_name",
    "benchmark",
    "requested_commit",
    "resolved_commit",
    "config_hash",
    "agent",
    "model",
    "execution_backend",
    "image_id",
    "status",
    "failure_reason",
    "trial",
    "started_at",
    "duration_seconds",
    "agent_exit_code",
    "agent_timed_out",
    "files_changed",
    "files_added",
    "files_deleted",
    "insertions",
    "deletions",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "result_dir",
]


def flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    """Keep only known-safe columns; missing values become empty strings."""
    flat = {}
    for column in EXPORT_COLUMNS:
        value = row.get(column)
        flat[column] = "" if value is None else value
    return flat


def to_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(flatten_row(row))
    return buffer.getvalue()


def to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps([flatten_row(row) for row in rows], indent=2)


def write_export(
    rows: list[dict[str, Any]],
    *,
    fmt: str,
    output: Path | None,
) -> str | Path:
    if fmt == "csv":
        rendered = to_csv(rows)
    elif fmt == "json":
        rendered = to_json(rows)
    else:
        raise ValueError(f"unsupported export format {fmt!r} (use csv or json)")
    if output is None:
        return rendered
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return output
