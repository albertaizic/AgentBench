"""Maintenance tool: (re)generate reference/fix.patch for corpus benchmarks.

Usage: python benchmarks/_make_reference.py [name ...]

Applies each benchmark's known-good fix inside a throwaway clone of its
fixture and records the resulting diff as reference/fix.patch. Reference
patches are used only by `agentbench benchmark validate`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def _run(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


FIXES: dict[str, dict[str, str]] = {
    "stockflow": {
        "stockflow/reservations.py": (
            '"""Customer-facing reservation flow."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n"
            "from stockflow.catalog import ProductCatalog\n"
            "from stockflow.feed import StockFeed\n\n\n"
            "@dataclass\n"
            "class Reservation:\n"
            "    order_id: str\n"
            "    sku: str\n"
            "    quantity: int\n\n\n"
            "class ReservationRegistry:\n"
            "    def __init__(self, feed: StockFeed, catalog: ProductCatalog) -> None:\n"
            "        self._feed = feed\n"
            "        self._catalog = catalog\n"
            "        self._by_order: dict[str, Reservation] = {}\n\n"
            "    def reserve(self, order_id: str, sku: str, quantity: int) -> Reservation:\n"
            "        key = order_id.strip().casefold()\n"
            "        if key in self._by_order:\n"
            "            return self._by_order[key]\n"
            "        if quantity <= 0:\n"
            "            raise ValueError(\"quantity must be positive\")\n"
            "        level = self._catalog.available(sku)\n"
            "        if quantity > level:\n"
            "            raise ValueError(f\"insufficient stock for {sku}\")\n"
            "        self._feed.consume(sku, quantity)\n"
            "        self._catalog.invalidate()\n"
            "        reservation = Reservation(order_id=order_id, sku=sku, quantity=quantity)\n"
            "        self._by_order[key] = reservation\n"
            "        return reservation\n"
        )
    },
    "ledgerpad": {
        "ledgerpad/tracker.py": (
            '"""Expense tracking with per-currency totals."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n\n"
            'VALID_CURRENCIES = {"USD", "EUR", "GBP"}\n\n\n'
            "@dataclass\n"
            "class Expense:\n"
            "    merchant: str\n"
            "    amount_cents: int\n"
            "    currency: str\n\n\n"
            "class ExpenseTracker:\n"
            "    def __init__(self) -> None:\n"
            "        self.expenses: list[Expense] = []\n\n"
            "    def add_expense(self, merchant: str, amount_cents: int,\n"
            "                    currency: str = \"USD\") -> Expense:\n"
            "        if amount_cents <= 0:\n"
            "            raise ValueError(\"amount must be positive\")\n"
            "        if currency.upper() not in VALID_CURRENCIES:\n"
            "            raise ValueError(f\"unknown currency: {currency}\")\n"
            "        expense = Expense(merchant=merchant, amount_cents=amount_cents,\n"
            "                          currency=currency.upper())\n"
            "        self.expenses.append(expense)\n"
            "        return expense\n\n"
            "    def total_cents(self, currency: str = \"USD\") -> int:\n"
            "        return sum(e.amount_cents for e in self.expenses\n"
            "                   if e.currency == currency)\n"
        )
    },
    "configschema": {
        "configschema/loader.py": (
            '"""JSON config loading with strict key checking."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import json\n"
            "from pathlib import Path\n\n\n"
            'ALLOWED_KEYS = {"host", "port", "retries"}\n\n\n'
            "def load_config(path: str | Path, *, strict: bool = True) -> dict:\n"
            '    """Load config JSON. In strict mode, unknown keys raise ValueError."""\n'
            '    data = json.loads(Path(path).read_text(encoding="utf-8"))\n'
            "    if not isinstance(data, dict):\n"
            '        raise ValueError("config must be a JSON object")\n'
            "    if strict:\n"
            "        unknown = set(data) - ALLOWED_KEYS\n"
            "        if unknown:\n"
            '            raise ValueError(f"unknown config keys: {sorted(unknown)}")\n'
            "    return data\n"
        ),
        "configschema/server.py": (
            '"""HTTP server settings derived from the config file."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from configschema.loader import load_config\n\n\n"
            'DEFAULTS = {"host": "127.0.0.1", "port": 8000, "retries": 3}\n\n\n'
            "class ServerSettings:\n"
            "    def __init__(self, values: dict) -> None:\n"
            "        merged = {**DEFAULTS, **values}\n"
            "        self.host = merged[\"host\"]\n"
            "        self.port = int(merged[\"port\"])\n"
            "        self.retries = int(merged[\"retries\"])\n\n\n"
            "def settings_from(path) -> ServerSettings:\n"
            "    return ServerSettings(load_config(path))\n"
        ),
        "configschema/cli.py": (
            '"""Command line entry point for the demo service."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from configschema.loader import load_config\n\n\n"
            "def describe(path) -> str:\n"
            "    data = load_config(path)\n"
            '    host = data.get("host", "127.0.0.1")\n'
            '    port = data.get("port", 8000)\n'
            '    return f"serving on {host}:{port}"\n'
        ),
    },
    "jobqueue": {
        "jobqueue/queue.py": (
            '"""A small deterministic priority job queue."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\nclass Job:\n"
            "    name: str\n"
            "    priority: int          # lower value runs first\n"
            "    seq: int               # insertion order tie-breaker\n\n\n"
            "class JobQueue:\n"
            "    def __init__(self) -> None:\n"
            "        self._jobs: list[Job] = []\n"
            "        self._next_seq = 0\n\n"
            "    def submit(self, name: str, priority: int) -> Job:\n"
            "        job = Job(name=name, priority=priority, seq=self._next_seq)\n"
            "        self._next_seq += 1\n"
            "        self._jobs.append(job)\n"
            "        return job\n\n"
            "    def drain(self) -> list[Job]:\n"
            "        ordered = sorted(\n"
            "            self._jobs,\n"
            "            key=lambda job: (int(job.priority), job.seq),\n"
            "        )\n"
            "        self._jobs.clear()\n"
            "        return ordered\n"
        )
    },
    "prefsfile": {
        "prefsfile/settings.py": (
            '"""Settings persistence with schema versioning."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import json\n"
            "from pathlib import Path\n\n\n"
            "SCHEMA_VERSION = 2\n"
            "KNOWN_VERSIONS = {1, SCHEMA_VERSION}\n\n"
            "DEFAULTS: dict = {\"theme\": \"light\", \"notifications\": True, \"retries\": 3}\n\n\n"
            "def _coerce_v1(value):\n"
            "    if isinstance(value, str):\n"
            "        lowered = value.strip().lower()\n"
            "        if lowered == \"true\":\n"
            "            return True\n"
            "        if lowered == \"false\":\n"
            "            return False\n"
            "        if lowered.lstrip(\"-\").isdigit():\n"
            "            return int(value)\n"
            "    return value\n\n\n"
            "def load_settings(path) -> dict:\n"
            '    data = json.loads(Path(path).read_text(encoding="utf-8"))\n'
            "    version = data.get(\"schema\", 1)\n"
            "    if version not in KNOWN_VERSIONS:\n"
            "        raise ValueError(f\"unsupported settings schema: {version}\")\n"
            "    settings = {**DEFAULTS}\n"
            "    for key, value in data.items():\n"
            "        if key == \"schema\":\n"
            "            continue\n"
            "        settings[key] = _coerce_v1(value) if version == 1 else value\n"
            "    return settings\n"
        )
    },
    "fuzzysearch": {
        "fuzzysearch/search.py": (
            '"""Substring counting with an instrumentation hook."""\n'
            "\nfrom __future__ import annotations\n\n\n"
            "class ComparisonCounter:\n"
            '    """Counts character comparisons; defaults to a throwaway counter."""\n\n'
            "    def __init__(self) -> None:\n"
            "        self.comparisons = 0\n\n"
            "    def record(self) -> None:\n"
            "        self.comparisons += 1\n\n\n"
            "def _failure_function(needle: str, counter) -> list[int]:\n"
            "    table = [0] * len(needle)\n"
            "    k = 0\n"
            "    for position in range(1, len(needle)):\n"
            "        while k > 0:\n"
            "            counter.record()\n"
            "            if needle[position] == needle[k]:\n"
            "                break\n"
            "            k = table[k - 1]\n"
            "        counter.record()\n"
            "        if needle[position] == needle[k]:\n"
            "            k += 1\n"
            "        table[position] = k\n"
            "    return table\n\n\n"
            "def substring_count(haystack: str, needle: str,\n"
            "                    counter=None) -> int:\n"
            '    """Count (possibly overlapping) occurrences via KMP: O(n + m)."""\n'
            "    counter = counter or ComparisonCounter()\n"
            "    if not needle or len(needle) > len(haystack):\n"
            "        return 0\n"
            "    table = _failure_function(needle, counter)\n"
            "    count = 0\n"
            "    matched = 0\n"
            "    for character in haystack:\n"
            "        while matched > 0:\n"
            "            counter.record()\n"
            "            if character == needle[matched]:\n"
            "                break\n"
            "            matched = table[matched - 1]\n"
            "        counter.record()\n"
            "        if character == needle[matched]:\n"
            "            matched += 1\n"
            "        if matched == len(needle):\n"
            "            count += 1\n"
            "            matched = table[matched - 1]\n"
            "    return count\n"
        )
    },
}


def make_patch(name: str) -> Path | None:
    fix_files = FIXES.get(name)
    if fix_files is None:
        print(f"no reference fix registered for {name}")
        return None
    fixture_dir = ROOT / name / "fixture"
    if not fixture_dir.exists():
        print(f"fixture missing for {name}; run its generator first")
        return None
    work = Path(tempfile.mkdtemp(prefix=f"agentbench-ref-{name}-"))
    try:
        shutil.copytree(fixture_dir, work, dirs_exist_ok=True)
        for relative, content in fix_files.items():
            (work / relative).write_text(content, encoding="utf-8")
        _run(["git", "add", "-A"], work)
        patch_text = _run(["git", "diff", "--cached"], work)
        out_dir = ROOT / name / "reference"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "fix.patch"
        out.write_text(patch_text, encoding="utf-8")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(names: list[str]) -> int:
    targets = names or sorted(FIXES)
    for name in targets:
        patch_path = make_patch(name)
        print(f"{name}: {patch_path}" if patch_path else f"{name}: FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
