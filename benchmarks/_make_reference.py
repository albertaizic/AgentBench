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
    "iniforge": {
        "iniforge/loader.py": (
            '"""INI settings loading with typed coercion."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import re\n\n"
            'FALSE_WORDS = {"false", "no", "off", "0"}\n'
            'TRUE_WORDS = {"true", "yes", "on", "1"}\n'
            '_INT = re.compile(r"[+-]?[0-9]+\\Z")\n\n\n'
            "class Settings:\n"
            "    def __init__(self) -> None:\n"
            "        self._sections: dict[str, dict[str, object]] = {}\n\n"
            "    def load(self, text: str) -> None:\n"
            '        section = "_root"\n'
            "        for raw_line in text.splitlines():\n"
            "            line = raw_line.strip()\n"
            '            if not line or line.startswith((";", "#")):\n'
            "                continue\n"
            '            if line.startswith("[") and line.endswith("]"):\n'
            "                section = line[1:-1].strip()\n"
            "                self._sections.setdefault(section, {})\n"
            "                continue\n"
            '            key, _, value = line.partition("=")\n'
            "            key = key.strip()\n"
            "            value = value.strip()\n"
            "            bucket = self._sections.setdefault(section, {})\n"
            "            # Last duplicate wins.\n"
            "            bucket[key] = self._coerce(key, value)\n\n"
            "    @staticmethod\n"
            "    def _coerce(key: str, value: str) -> object:\n"
            "        lowered = value.lower()\n"
            "        if lowered in TRUE_WORDS:\n"
            "            return True\n"
            "        if lowered in FALSE_WORDS:\n"
            "            return False\n"
            "        if _INT.fullmatch(value):\n"
            "            return int(value)\n"
            "        if value:\n"
            "            return value  # any other non-empty value is a string\n"
            '        raise ValueError(f"empty value for {key!r}")\n\n'
            "    def get(self, section: str, key: str, default: object = None) -> object:\n"
            "        return self._sections.get(section, {}).get(key, default)\n"
        )
    },
    "csvroll": {
        "csvroll/records.py": (
            '"""Record sets persisted as CSV (v2 with header, v1 without)."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import csv\n"
            "import io\n\n"
            'HEADER = ["id", "label", "amount"]\n\n\n'
            "class RecordSet:\n"
            "    def __init__(self) -> None:\n"
            "        self.rows: list[tuple[str, str, str]] = []\n\n"
            "    def add(self, record_id: str, label: str, amount: str) -> None:\n"
            "        self.rows.append((record_id, label, amount))\n\n"
            "    def to_csv(self) -> str:\n"
            "        buffer = io.StringIO(newline='')\n"
            "        writer = csv.writer(buffer, lineterminator='\\n')\n"
            "        writer.writerow(HEADER)\n"
            "        writer.writerows(self.rows)\n"
            "        return buffer.getvalue()\n\n"
            "    @classmethod\n"
            "    def from_csv(cls, text: str) -> \"RecordSet\":\n"
            "        reader = csv.reader(io.StringIO(text, newline=''))\n"
            "        parsed = [tuple(row) for row in reader if row]\n"
            "        if parsed and list(parsed[0]) == HEADER:\n"
            "            parsed = parsed[1:]  # v2: exact header match\n"
            "        record_set = cls()\n"
            "        record_set.rows = [\n"
            "            (row[0], row[1], row[2]) for row in parsed\n"
            "        ]\n"
            "        return record_set\n"
        )
    },
    "tokenbucket": {
        "tokenbucket/limiter.py": (
            '"""Token-bucket rate limiting for outbound API calls."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import time\n\n\n"
            "class TokenBucket:\n"
            "    def __init__(self, capacity: float, refill_per_second: float,\n"
            "                 clock=None) -> None:\n"
            "        self.capacity = float(capacity)\n"
            "        self.refill_rate = float(refill_per_second)\n"
            "        self._clock = clock or time.monotonic\n"
            "        self._tokens = float(capacity)\n"
            "        self._last = self._clock()\n\n"
            "    def _refill(self) -> None:\n"
            "        now = self._clock()\n"
            "        elapsed = max(0.0, now - self._last)\n"
            "        gained = self.refill_rate * elapsed   # fractional credit kept\n"
            "        self._tokens = min(self.capacity, self._tokens + gained)\n"
            "        self._last = now\n\n"
            "    def try_take(self, amount: float) -> bool:\n"
            "        self._refill()\n"
            "        amount = max(0.0, float(amount))\n"
            "        if amount <= self._tokens:            # clamp: never go negative\n"
            "            self._tokens -= amount\n"
            "            return True\n"
            "        return False\n\n"
            "    @property\n"
            "    def available(self) -> float:\n"
            "        self._refill()\n"
            "        return self._tokens\n"
        )
    },
    "logroll": {
        "logroll/setup.py": (
            '"""Application logging setup."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import logging\n"
            "import sys\n\n\n"
            'LOGGER_NAME = "logroll.app"\n'
            '_FORMAT = "%(levelname)s %(name)s: %(message)s"\n\n\n'
            "def configure_logger(level: int = logging.INFO,\n"
            "                     stream=None) -> logging.Logger:\n"
            "    logger = logging.getLogger(LOGGER_NAME)\n"
            "    logger.setLevel(level)\n"
            "    handler = logging.StreamHandler(stream or sys.stdout)\n"
            "    handler.setFormatter(logging.Formatter(_FORMAT))\n"
            "    # Idempotent: same-config calls never duplicate handlers; a real\n"
            "    # change replaces (and closes) the previous set.\n"
            "    existing = logger.handlers\n"
            "    if len(existing) == 1 and isinstance(existing[0], logging.StreamHandler):\n"
            "        current = existing[0]\n"
            "        same_stream = getattr(current, 'stream', None) is handler.stream\n"
            "        if (same_stream and logger.level == level\n"
            "                and current.formatter is not None\n"
            "                and current.formatter._fmt == _FORMAT):\n"
            "            return logger\n"
            "    for old in existing:\n"
            "        logger.removeHandler(old)\n"
            "        old.close()\n"
            "    logger.addHandler(handler)\n"
            "    return logger\n"
        )
    },
    "vercomp": {
        "vercomp/versions.py": (
            '"""Version ordering for the release tool."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import re\n\n"
            "_VERSION = re.compile(\n"
            '    r"^(?P<core>[0-9]+(?:\\.[0-9]+)*)(?:-(?P<pre>.+))?$"\n'
            ")\n\n\n"
            "def compare(a: str, b: str) -> int:\n"
            "    ka, kb = _sort_key(a), _sort_key(b)\n"
            "    return -1 if ka < kb else (1 if ka > kb else 0)\n\n"
            "def sort_versions(versions: list[str]) -> list[str]:\n"
            "    return sorted(versions, key=_sort_key)\n\n"
            "def _parse(version: str):\n"
            "    match = _VERSION.match(version.strip())\n"
            "    if match is None:\n"
            '        raise ValueError(f"unparseable version: {version!r}")\n'
            "    core = tuple(int(part) for part in match.group(\"core\").split(\".\"))\n"
            "    pre = match.group(\"pre\")\n"
            "    identifiers = tuple(pre.split(\".\")) if pre else ()\n"
            "    return core, identifiers\n\n"
            "def _padded(core):\n"
            "    return tuple(list(core) + [0] * (3 - len(core))) if len(core) < 3 else core\n\n"
            "def _identifier_key(identifier: str):\n"
            "    if identifier.isdigit():\n"
            "        return (0, int(identifier), \"\")   # numeric sorts first\n"
            "    return (1, 0, identifier)\n\n"
            "def _sort_key(version: str):\n"
            "    core, identifiers = _parse(version)\n"
            "    core = _padded(core)                    # 1.5 == 1.5.0\n"
            "    pre_key = (\n"
            "        (0, tuple(_identifier_key(i) for i in identifiers))\n"
            "        if identifiers else (1, ())         # any prerelease < release\n"
            "    )\n"
            "    return (core, pre_key)\n"
        )
    },
    "retryloop": {
        "retryloop/core.py": (
            '"""Retry execution for flaky outbound calls."""\n'
            "\nfrom __future__ import annotations\n\n\n"
            "class RetryableError(Exception):\n"
            '    """Transient failure: safe to attempt again."""\n\n\n'
            "class FatalError(Exception):\n"
            '    """Permanent failure: retrying cannot help."""\n\n\n'
            "def run_with_retry(operation, attempts: int = 3, on_retry=None):\n"
            "    last_error = None\n"
            "    for attempt in range(attempts):\n"
            "        try:\n"
            "            return operation()\n"
            "        except RetryableError as exc:\n"
            "            last_error = exc\n"
            "            if attempt < attempts - 1 and on_retry is not None:\n"
            "                on_retry(exc, attempt)\n"
            "    # Exhausted: surface the real error instead of returning None.\n"
            "    raise last_error\n"
        )
    },
    "typegate": {
        "gate/remote.py": (
            '"""Fetch configs from the settings service."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import json\n\n"
            "from gate.config import GateConfig\n\n\n"
            "_KNOWN = {\"endpoint\", \"retries\", \"strict\"}\n\n\n"
            "def fetch_and_load(payload: str, strict: bool = False) -> GateConfig:\n"
            "    data = json.loads(payload)\n"
            "    unknown = {k: v for k, v in data.items() if k not in _KNOWN}\n"
            "    if strict and unknown:\n"
            '        raise KeyError(f\"unknown config key: {sorted(unknown)[0]}\")\n'
            "    return GateConfig(\n"
            "        endpoint=data.get(\"endpoint\", \"https://example.invalid\"),\n"
            "        retries=data.get(\"retries\", 2),\n"
            "        strict=strict,\n"
            "        extra=unknown,\n"
            "    )\n"
        ),
        "gate/policy.py": (
            '"""Policy evaluation over a tiny config cache."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from gate.config import GateConfig\n\n\n"
            "_cache: dict[str, GateConfig] = {}\n"
            '_ALLOWED = {"endpoint", "retries", "strict"}\n\n\n'
            "def cache_config(name: str, config: GateConfig) -> None:\n"
            "    _cache[name] = config\n\n\n"
            "def evaluate(name: str, payload_keys: list[str]) -> list[str]:\n"
            "    config = _cache[name]\n"
            "    if config.strict:\n"
            "        unknown = [k for k in payload_keys if k not in _ALLOWED]\n"
            "        if unknown:\n"
            '            raise KeyError(f"unknown config key: {sorted(unknown)[0]}")\n'
            "    return [k for k in payload_keys if k in _ALLOWED]\n"
        ),
        "gate/cli.py": (
            '"""Command line entry point."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import argparse\n\n"
            "from gate.loader import load_config\n"
            "from gate.remote import fetch_and_load\n\n\n"
            "def main(argv=None):\n"
            "    parser = argparse.ArgumentParser(prog=\"gate\")\n"
            '    parser.add_argument("--file")\n'
            '    parser.add_argument("--payload")\n'
            '    parser.add_argument("--strict", action="store_true")\n'
            "    args = parser.parse_args(argv)\n"
            "    if args.file:\n"
            '        return load_config(args.file, strict=args.strict)\n'
            '    return fetch_and_load(args.payload or "{}", strict=args.strict)\n'
        ),
    },
    "htmlstrip": {
        "htmlstrip/core.py": (
            '"""Convert small HTML fragments to plain text."""\n'
            "\nfrom __future__ import annotations\n\n"
            "import html as _html\n\n"
            "_BLOCK_NAMES = {\"p\", \"div\", \"li\", \"br\"}\n"
            "_VOID_BLOCKS = {\"br\"}\n"
            "_RAW_TEXT = {\"script\", \"style\"}\n\n\n"
            "def to_text(markup: str) -> str:\n"
            "    out: list[str] = []\n"
            "    index = 0\n"
            "    length = len(markup)\n"
            "    while index < length:\n"
            "        char = markup[index]\n"
            "        if markup.startswith(\"<!--\", index):\n"
            "            end = markup.find(\"-->\", index + 4)\n"
            "            index = length if end == -1 else end + 3\n"
            "            out.append(\" \")\n"
            "            continue\n"
            "        if char == \"<\":\n"
            "            closing = markup.find(\">\", index + 1)\n"
            "            if closing == -1:\n"
            "                out.append(char)\n"
            "                index += 1\n"
            "                continue\n"
            "            tag_body = markup[index + 1:closing].strip()\n"
            "            name = tag_body.split()[0].lower() if tag_body else \"\"\n"
            "            if name.startswith(\"/\"):\n"
            "                name = name[1:]\n"
            "            if name in _RAW_TEXT:\n"
            "                close_token = f\"</{name}>\"\n"
            "                stop = markup.lower().find(close_token, closing + 1)\n"
            "                index = length if stop == -1 else stop + len(close_token)\n"
            "                out.append(\" \")\n"
            "                continue\n"
            "            if name in _BLOCK_NAMES:\n"
            "                out.append(\" \")\n"
            "            index = closing + 1\n"
            "            continue\n"
            "        if char == \"&\":\n"
            "            semicolon = markup.find(\";\", index + 1)\n"
            "            candidate = markup[index:semicolon + 1] if semicolon != -1 else \"\"\n"
            "            if candidate.startswith(\"&\") and len(candidate) > 2:\n"
            "                decoded = _html.unescape(candidate)\n"
            "                out.append(decoded)\n"
            "                index += len(candidate)\n"
            "                continue\n"
            "        out.append(char)\n"
            "        index += 1\n"
            "    # Entities were decoded exactly once during the scan.\n"
            "    text = \"\".join(out)\n"
            "    return \" \".join(text.split())\n"
        )
    },
    "bankday": {
        "bankday/ledger.py": (
            '"""In-memory double-entry ledger."""\n'
            "\nfrom __future__ import annotations\n\n"
            "from dataclasses import dataclass, field\n\n\n"
            "class LedgerError(Exception):\n"
            "    pass\n\n\n"
            "class ValidationError(LedgerError):\n"
            "    pass\n\n\n"
            "@dataclass\nclass Entry:\n"
            '    kind: str          # "deposit" | "transfer"\n'
            "    source: str | None\n"
            "    target: str | None\n"
            "    amount: int\n\n"
            "@dataclass\nclass Batch:\n"
            "    entries: list[Entry] = field(default_factory=list)\n"
            "    committed: bool = False\n\n\n"
            "class Ledger:\n"
            "    def __init__(self) -> None:\n"
            "        self.balances: dict[str, int] = {}\n"
            "        self.history: list[Entry] = []\n\n"
            "    def deposit(self, account: str, amount: int) -> None:\n"
            "        if amount <= 0:\n"
            '            raise ValidationError("amount must be positive")\n'
            "        self.balances[account] = self.balances.get(account, 0) + amount\n"
            '        self.history.append(Entry("deposit", None, account, amount))\n\n'
            "    def transfer(self, source: str, target: str, amount: int) -> None:\n"
            "        # Validate FIRST; mutate only when every check passed.\n"
            "        if amount <= 0:\n"
            '            raise ValidationError("amount must be positive")\n'
            "        if target == source:\n"
            '            raise ValidationError("source and target must differ")\n'
            "        if self.balances.get(source, 0) < amount:\n"
            '            raise ValidationError("insufficient funds")\n'
            "        self.balances[source] = self.balances.get(source, 0) - amount\n"
            "        self.balances[target] = self.balances.get(target, 0) + amount\n"
            '        self.history.append(Entry("transfer", source, target, amount))\n\n'
            "    def execute_batch(self, entries: list[Entry]) -> None:\n"
            "        # Atomicity: stage everything on copies; commit only if all\n"
            "        # steps validate. History records committed work exclusively.\n"
            "        balances = dict(self.balances)\n"
            "        staged: list[Entry] = []\n"
            "\n"
            "        def apply(entry: Entry) -> None:\n"
            "            if entry.amount <= 0:\n"
            '                raise ValidationError("amount must be positive")\n'
            '            if entry.kind == "deposit":\n'
            "                balances[entry.target] = balances.get(entry.target, 0) + entry.amount\n"
            "                staged.append(entry)\n"
            "                return\n"
            "            if entry.target == entry.source:\n"
            '                raise ValidationError("source and target must differ")\n'
            "            if balances.get(entry.source, 0) < entry.amount:\n"
            '                raise ValidationError("insufficient funds")\n'
            "            balances[entry.source] = balances.get(entry.source, 0) - entry.amount\n"
            "            balances[entry.target] = balances.get(entry.target, 0) + entry.amount\n"
            "            staged.append(entry)\n"
            "\n"
            "        for entry in entries:\n"
            "            apply(entry)\n"
            "        self.balances = balances\n"
            "        self.history.extend(staged)\n\n"
            "    def total(self) -> int:\n"
            "        return sum(self.balances.values())\n"
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
