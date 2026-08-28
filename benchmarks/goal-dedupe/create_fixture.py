"""Deterministic generator for the goal-dedupe fixture (duplicate-processing bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "ingestkit"\nversion = "0.4.1"\n',
    "ingest/__init__.py": (
        '"""Ingestion toolkit: pipeline, dedupe bookkeeping, result store."""\n'
        "\n"
        "from ingest.dedupe import Deduper\n"
        "from ingest.pipeline import Pipeline, Record, process_record\n"
        "from ingest.store import RecordStore\n"
        "\n"
        '__all__ = ["Deduper", "Pipeline", "Record", "RecordStore", "process_record"]\n'
    ),
    # BUG (pipeline): a fresh Deduper is built for every submit() call, so
    # dedupe state dies with the batch. Any record id already handled by an
    # earlier batch (or a sibling Pipeline sharing the same store) is
    # processed again from scratch. Under load -- many overlapping batches --
    # hot record ids are executed once per containing batch instead of once
    # ever. Results stay CORRECT (deterministic transform), only redundant
    # work and audit noise betray the defect.
    "ingest/pipeline.py": (
        '"""Submission pipeline turning raw records into processed results."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "from dataclasses import dataclass\n"
        "\n"
        "from ingest.dedupe import Deduper\n"
        "from ingest.store import RecordStore\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Record:\n"
        '    record_id: str\n'
        "    kind: str\n"
        "    payload: str\n"
        "\n"
        "\n"
        "def process_record(record: Record) -> str:\n"
        '    """Deterministic transformation; identical inputs yield identical outputs."""\n'
        '    return f"{record.kind}:{record.payload.upper()}"\n'
        "\n"
        "\n"
        "class Pipeline:\n"
        '    """Accepts ordered batches of records and yields processed outputs."""\n'
        "\n"
        "    def __init__(self, store: RecordStore | None = None) -> None:\n"
        "        self.store = store if store is not None else RecordStore()\n"
        "\n"
        "    def submit(self, records: list[Record]) -> list[str]:\n"
        "        # BUG: dedupe state is recreated per submission, so cross-batch\n"
        "        # repeats of a record id are re-executed instead of short-circuited.\n"
        "        deduper = Deduper()\n"
        "        outputs: list[str] = []\n"
        "        for record in records:\n"
        "            if deduper.already_processed(record.record_id):\n"
        '                outputs.append(self.store.get(record.record_id, ""))\n'
        "                continue\n"
        "            self.store.note_execution(record.record_id)\n"
        "            value = process_record(record)\n"
        "            self.store.save(record.record_id, value)\n"
        "            deduper.mark_processed(record.record_id)\n"
        "            outputs.append(value)\n"
        "        return outputs\n"
    ),
    "ingest/dedupe.py": (
        '"""Bookkeeping of which record ids a pipeline has already processed."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "\n"
        "class Deduper:\n"
        '    """Remembers record ids processed so far (in-memory, per owner)."""\n'
        "\n"
        "    def __init__(self) -> None:\n"
        "        self._seen: set[str] = set()\n"
        "\n"
        "    def already_processed(self, record_id: str) -> bool:\n"
        "        return record_id in self._seen\n"
        "\n"
        "    def mark_processed(self, record_id: str) -> None:\n"
        "        self._seen.add(record_id)\n"
    ),
    "ingest/store.py": (
        '"""Durable record results with an execution audit trail."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "\n"
        "class RecordStore:\n"
        '    """Stores processed results keyed by record id and audits executions."""\n'
        "\n"
        "    def __init__(self) -> None:\n"
        "        self._results: dict[str, str] = {}\n"
        "        self._events: list[tuple[str, str]] = []\n"
        "\n"
        "    # -- results -------------------------------------------------------\n"
        "    def has(self, record_id: str) -> bool:\n"
        "        return record_id in self._results\n"
        "\n"
        "    def get(self, record_id: str, default: str | None = None) -> str | None:\n"
        "        return self._results.get(record_id, default)\n"
        "\n"
        "    def save(self, record_id: str, value: str) -> None:\n"
        "        self._results[record_id] = value\n"
        '        self._events.append(("save", record_id))\n'
        "\n"
        "    # -- audit trail ---------------------------------------------------\n"
        "    def note_execution(self, record_id: str) -> None:\n"
        '        """Audit hook: called once per actual processing run."""\n'
        '        self._events.append(("execute", record_id))\n'
        "\n"
        "    def execution_count(self, record_id: str) -> int:\n"
        '        return sum(1 for kind, rid in self._events if kind == "execute" and rid == record_id)\n'
        "\n"
        "    def duplicate_executions(self) -> int:\n"
        '        """Total executions minus one per distinct id ever executed."""\n'
        "        counts: dict[str, int] = {}\n"
        "        for kind, rid in self._events:\n"
        '            if kind == "execute":\n'
        "                counts[rid] = counts.get(rid, 0) + 1\n"
        "        return sum(n - 1 for n in counts.values())\n"
        "\n"
        "    def events(self) -> list[tuple[str, str]]:\n"
        "        return list(self._events)\n"
    ),
    "tests/test_ingestion_public.py": (
        '"""Public tests for the ingestion pipeline contract."""\n'
        "\n"
        "from ingest import Deduper, Pipeline, Record, RecordStore, process_record\n"
        "\n"
        "\n"
        "def test_process_record_is_deterministic():\n"
        '    rec = Record("x1", "scan", "invoice-42")\n'
        '    assert process_record(rec) == "scan:INVOICE-42"\n'
        "    assert process_record(rec) == process_record(rec)\n"
        "\n"
        "\n"
        "def test_outputs_preserve_submission_order():\n"
        "    pipe = Pipeline()\n"
        "    out = pipe.submit(\n"
        "        [\n"
        '            Record("a", "k", "1"),\n'
        '            Record("b", "k", "2"),\n'
        '            Record("c", "k", "3"),\n'
        "        ]\n"
        "    )\n"
        '    assert out == ["k:1", "k:2", "k:3"]\n'
        "\n"
        "\n"
        "def test_empty_batch_returns_empty_and_executes_nothing():\n"
        "    store = RecordStore()\n"
        "    pipe = Pipeline(store)\n"
        "    assert pipe.submit([]) == []\n"
        "    assert store.events() == []\n"
        "\n"
        "\n"
        "def test_duplicate_within_single_batch_executes_once():\n"
        "    store = RecordStore()\n"
        "    pipe = Pipeline(store)\n"
        "    out = pipe.submit(\n"
        "        [\n"
        '            Record("d1", "mail", "hello"),\n'
        '            Record("d1", "mail", "hello"),\n'
        '            Record("d2", "mail", "world"),\n'
        "        ]\n"
        "    )\n"
        '    assert out == ["mail:HELLO", "mail:HELLO", "mail:WORLD"]\n'
        "    assert store.execution_count(\"d1\") == 1\n"
        "    assert store.duplicate_executions() == 0\n"
        "\n"
        "\n"
        "def test_duplicate_across_batches_executes_once():\n"
        "    store = RecordStore()\n"
        "    pipe = Pipeline(store)\n"
        '    pipe.submit([Record("r1", "web", "ping")])\n'
        "    out = pipe.submit([Record(\"r1\", \"web\", \"ping\")])\n"
        '    assert out == ["web:PING"]\n'
        '    assert store.execution_count("r1") == 1\n'
        "    assert store.duplicate_executions() == 0\n"
        "\n"
        "\n"
        "def test_no_redundant_processing_under_load():\n"
        "    store = RecordStore()\n"
        "    pipe = Pipeline(store)\n"
        "    hot = [Record(f\"hot{i}\", \"job\", f\"p{i}\") for i in range(5)]\n"
        "    for _ in range(8):\n"
        "        pipe.submit(hot)\n"
        "    assert store.duplicate_executions() == 0\n"
        '    assert sum(1 for kind, _ in store.events() if kind == "save") == 5\n'
        "\n"
        "\n"
        "def test_sibling_pipeline_sharing_store_does_not_reprocess():\n"
        "    store = RecordStore()\n"
        "    first = Pipeline(store)\n"
        "    second = Pipeline(store)\n"
        '    first.submit([Record("s1", "etl", "alpha")])\n'
        '    out = second.submit([Record("s1", "etl", "alpha")])\n'
        '    assert out == ["etl:ALPHA"]\n'
        '    assert store.execution_count("s1") == 1\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "goal-dedupe: ingestion duplicate-processing defect", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
