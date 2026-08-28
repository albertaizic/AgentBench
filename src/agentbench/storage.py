"""SQLite query/index layer over run evidence.

``result.json`` files remain the source of truth; this module maintains a
derived index for history/comparison/dashboard queries. The design is
deliberately simple:

* one table, keyed by the unique run id (INSERT OR REPLACE keeps re-indexing
  idempotent — duplicate indexing never duplicates runs);
* parameterized SQL only;
* ``PRAGMA user_version`` as the migration marker for an early project;
* indexing failures are reported to callers via exceptions so they can warn
  without touching the underlying JSON evidence.

Keep every query in this module — nothing else in the codebase writes SQL.
"""

from __future__ import annotations

import logging

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_USER_VERSION = 5
# Seconds a write waits for a competing writer before failing. Parallel
# experiment indexing makes brief contention normal, not exceptional.
_SQLITE_BUSY_TIMEOUT = 10.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    schema_version   INTEGER NOT NULL,
    benchmark        TEXT NOT NULL,
    repository       TEXT NOT NULL,
    requested_commit TEXT NOT NULL,
    resolved_commit  TEXT NOT NULL,
    config_hash      TEXT NOT NULL,
    agent            TEXT NOT NULL,
    model            TEXT,
    status           TEXT NOT NULL,
    failure_reason   TEXT,
    trial            INTEGER,
    started_at       TEXT,
    duration_seconds REAL,
    agent_exit_code  INTEGER,
    agent_timed_out  INTEGER NOT NULL DEFAULT 0,
    files_changed    INTEGER,
    insertions       INTEGER,
    deletions        INTEGER,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    total_tokens     INTEGER,
    cost_usd         REAL,
    cost_provenance  TEXT,
    validity         TEXT,
    result_dir       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_benchmark ON runs(benchmark);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_agent_model ON runs(agent, model);
CREATE INDEX IF NOT EXISTS idx_runs_config ON runs(benchmark, config_hash);
"""

# v2 migration: execution provenance, experiment linkage, diff-shape metrics.
_MIGRATIONS = {
    2: [
        "ALTER TABLE runs ADD COLUMN execution_backend TEXT",
        "ALTER TABLE runs ADD COLUMN image_id TEXT",
        "ALTER TABLE runs ADD COLUMN image_digest TEXT",
        "ALTER TABLE runs ADD COLUMN experiment_id TEXT",
        "ALTER TABLE runs ADD COLUMN config_name TEXT",
        "ALTER TABLE runs ADD COLUMN files_added INTEGER",
        "ALTER TABLE runs ADD COLUMN files_deleted INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_runs_experiment ON runs(experiment_id)",
    ],
    # v3 migration: WHERE a failure happened (see agentbench.stages) and how
    # often protected paths were touched.
    3: [
        "ALTER TABLE runs ADD COLUMN failure_stage TEXT",
        "ALTER TABLE runs ADD COLUMN violation_count INTEGER",
    ],
    # v4 migration: where a cost figure came from (P16 metric provenance).
    4: [
        "ALTER TABLE runs ADD COLUMN cost_provenance TEXT",
    ],
    # v5 migration: orthogonal evaluation-validity grade (P41).
    5: [
        "ALTER TABLE runs ADD COLUMN validity TEXT",
    ],
}


def default_db_path(results_root: Path) -> Path:
    return Path(results_root) / ".agentbench" / "agentbench.db"


logger = logging.getLogger(__name__)


class ResultIndex:
    """Read/write access to the derived SQLite index."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=_SQLITE_BUSY_TIMEOUT)
        # Parallel experiments open several short-lived writers. WAL lets
        # readers proceed during writes and busy_timeout turns rare write
        # collisions into brief waits instead of "database is locked" errors.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT * 1000)}")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_USER_VERSION:
            for target in sorted(_MIGRATIONS):
                if version < target <= SCHEMA_USER_VERSION:
                    for statement in _MIGRATIONS[target]:
                        try:
                            self._conn.execute(statement)
                        except sqlite3.OperationalError:
                            # Column already exists (partially migrated DB):
                            # additive migrations are safe to re-apply.
                            pass
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writing ------------------------------------------------------------

    def index_result(self, payload: dict, *, result_dir: Path) -> None:
        """Index one parsed ``result.json`` payload. Idempotent per run_id."""
        overall = payload.get("overall") or {}
        benchmark = payload.get("benchmark") or {}
        agent = payload.get("agent") or {}
        diff = payload.get("diff") or {}
        usage = payload.get("usage")
        execution = payload.get("execution") or {}
        digests = execution.get("image_digests") or []
        self._conn.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, schema_version, benchmark, repository, requested_commit,
                resolved_commit, config_hash, agent, model, status, failure_reason,
                trial, started_at, duration_seconds, agent_exit_code, agent_timed_out,
                files_changed, insertions, deletions,
                input_tokens, output_tokens, total_tokens, cost_usd,
                cost_provenance, validity,
                execution_backend, image_id, image_digest,
                experiment_id, config_name,
                files_added, files_deleted,
                failure_stage, violation_count,
                result_dir, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload["run_id"]),
                int(payload.get("schema_version", 1)),
                str(benchmark.get("name", "")),
                str(benchmark.get("repository", "")),
                str(benchmark.get("commit", "")),
                str(benchmark.get("resolved_commit", "")),
                str(benchmark.get("config_hash", "")),
                str(agent.get("type", "")),
                agent.get("model"),
                str(overall.get("status", "")),
                _clean_text(overall.get("failure_reason")),
                payload.get("trial"),
                _normalized_timestamp(overall.get("started_at")),
                _as_float(overall.get("duration_seconds")),
                _as_int(agent.get("exit_code")),
                1 if agent.get("timed_out") else 0,
                _as_int(diff.get("files_changed")),
                _as_int(diff.get("insertions")),
                _as_int(diff.get("deletions")),
                _as_int((usage or {}).get("input_tokens")),
                _as_int((usage or {}).get("output_tokens")),
                _as_int((usage or {}).get("total_tokens")),
                *_normalized_cost(usage),
                _normalized_validity(overall),
                execution.get("backend"),
                execution.get("image_id"),
                digests[0] if digests else None,
                payload.get("experiment_id"),
                payload.get("config_name"),
                _list_len_or_none(diff.get("added_files")),
                _list_len_or_none(diff.get("deleted_files")),
                overall.get("failure_stage"),
                _violation_count(payload),
                str(result_dir),
                str(overall.get("finished_at") or overall.get("started_at") or ""),
            ),
        )
        self._conn.commit()

    def scan_results(self, results_root: Path) -> tuple[int, int]:
        """(Re)index every ``<benchmark>/<run>/result.json`` under *results_root*.

        Malformed or unreadable artifacts are skipped, never destructive.
        Returns (indexed, skipped).
        """
        indexed = skipped = 0
        results_root = Path(results_root)
        for result_file in sorted(results_root.glob("*/*/result.json")):
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or "run_id" not in payload:
                    raise ValueError("not a run result")
                self.index_result(payload, result_dir=result_file.parent)
                indexed += 1
            except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
                skipped += 1
                logger.warning("skipped unreadable run artifact %s: %s",
                               result_file, exc)
        return indexed, skipped

    # -- reading ------------------------------------------------------------

    def query(
        self,
        *,
        benchmark: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        status: str | None = None,
        experiment_id: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list = []
        if benchmark is not None:
            sql += " AND benchmark = ?"
            params.append(benchmark)
        if agent is not None:
            sql += " AND agent = ?"
            params.append(agent)
        if model is not None:
            sql += " AND model = ?"
            params.append(model)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if experiment_id is not None:
            sql += " AND experiment_id = ?"
            params.append(experiment_id)
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        params.extend([limit if limit is not None else -1, max(0, offset)])
        return [dict(row) for row in self._conn.execute(sql, params)]

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def benchmarks(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT benchmark FROM runs ORDER BY benchmark"
        ).fetchall()
        return [row["benchmark"] for row in rows]


def _normalized_cost(usage: dict | None) -> tuple:
    """(cost_usd, cost_provenance) with exact-$0 prices treated as missing.

    Providers report 0.0 with an estimated/unknown status for models that
    have no pricing data; recording $0 would fabricate a cross-agent cost
    result downstream. Primary ``result.json`` evidence is never touched —
    this normalization happens only in the derived, rescannable index.
    A genuinely billed run is never exactly zero.
    """
    cost = _as_float((usage or {}).get("cost_usd"))
    provenance = (usage or {}).get("cost_provenance")
    if cost == 0:
        cost = None
        provenance = f"unpriced/{provenance}" if provenance else "unpriced"
    return cost, provenance


def _normalized_validity(overall: dict) -> str:
    """Validity grade of a run; historical rows default to ``valid``."""
    value = overall.get("validity")
    return str(value) if isinstance(value, str) and value else "valid"


def _normalized_timestamp(value) -> str | None:
    """Timestamps are structured fields: anything unparseable is dropped.

    Free-text smuggled into timestamp columns must never reach derived
    surfaces (exports ship these columns verbatim).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _clean_text(value, *, limit: int = 500):
    """Collapse whitespace in human-readable reason fields; keep it short."""
    if not isinstance(value, str):
        return value
    collapsed = " ".join(value.split())
    return collapsed[:limit]


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _list_len_or_none(value) -> int | None:
    return len(value) if isinstance(value, list) else None


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _violation_count(payload: dict) -> int | None:
    protected = payload.get("protected_paths")
    if not isinstance(protected, dict):
        return None
    violations = protected.get("violations")
    return len(violations) if isinstance(violations, list) else 0
