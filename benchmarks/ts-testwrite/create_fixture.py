"""Deterministic generator for the ts-testwrite fixture (mutation-scored test writing)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

PACKAGE_JSON = (
    '{\n'
    '  "name": "duracore",\n'
    '  "version": "1.0.4",\n'
    '  "type": "module",\n'
    '  "description": "Duration parsing, formatting, and clamping utilities"\n'
    '}\n'
)

# NOTE: this module is intentionally undocumented. Mutant replacements in
# benchmarks/ts-testwrite/hidden/score_mutants.mjs depend on these EXACT
# text fragments; change both together.
DURATION_JS = '''const UNIT_MS = { ms: 1, s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 };
const UNIT_ORDER = ["d", "h", "m", "s", "ms"];

/**
 * Internal: tokenize a normalized duration expression.
 */
function tokenize(text) {
  /** @type {Array<[number, string]>} */
  const tokens = [];
  let rest = text;
  const pattern = /^(\\d+)(ms|s|m|h|d)/;
  while (rest.length > 0) {
    const match = pattern.exec(rest);
    if (!match) {
      throw new RangeError(`invalid duration segment near: ${rest}`);
    }
    tokens.push([Number.parseInt(match[1], 10), match[2]]);
    rest = rest.slice(match[0].length);
  }
  return tokens;
}

/**
 * Internal: sum tokens into milliseconds, enforcing descending unit order.
 */
function toMilliseconds(tokens) {
  let total = 0;
  let previous = -1;
  for (const [amount, unit] of tokens) {
    const index = UNIT_ORDER.indexOf(unit);
    if (previous !== -1 && index <= previous) {
      throw new RangeError("duration units must appear in descending order");
    }
    total += amount * UNIT_MS[unit];
    previous = index;
  }
  return total;
}

export function parseDuration(text) {
  if (typeof text !== "string") {
    throw new TypeError("duration must be a string");
  }
  const trimmed = text.trim();
  if (trimmed.length === 0) {
    throw new RangeError("empty duration text");
  }
  const compact = trimmed.replace(/\\s+/g, "");
  const negative = compact.startsWith("-");
  const body = negative ? compact.slice(1) : compact;
  const total = toMilliseconds(tokenize(body));
  return negative ? -total : total;
}

export function formatDuration(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) {
    throw new TypeError("duration must be a finite number of milliseconds");
  }
  const negative = ms < 0;
  let remaining = Math.abs(Math.round(ms));
  if (remaining === 0) {
    return "0ms";
  }
  /** @type {string[]} */
  const parts = [];
  for (const [unit, size] of [["d", UNIT_MS.d], ["h", UNIT_MS.h], ["m", UNIT_MS.m], ["s", UNIT_MS.s]]) {
    if (remaining >= size) {
      parts.push(`${Math.floor(remaining / size)}${unit}`);
      remaining %= size;
    }
  }
  if (remaining > 0) {
    parts.push(`${remaining}ms`);
  }
  return (negative ? "-" : "") + parts.join("");
}

export function clampDuration(ms, bounds = {}) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) {
    throw new TypeError("duration must be a finite number of milliseconds");
  }
  const min = bounds.min ?? null;
  const max = bounds.max ?? null;
  let result = ms;
  if (min !== null && result < min) {
    result = min;
  }
  if (max !== null && result > max) {
    result = max;
  }
  return result;
}
'''

BUDGET_JS = '''import { clampDuration, formatDuration, parseDuration } from "./duration.js";

/**
 * Builds a time-budget report from human-written duration strings.
 * @param {{name: string, limit: string, spent: string, floorMs?: number, ceilingMs?: number}} request
 */
export function buildBudget({ name, limit, spent, floorMs, ceilingMs }) {
  const total = parseDuration(limit);
  const used = parseDuration(spent);
  const left = clampDuration(total - used, { min: floorMs ?? 0, max: ceilingMs ?? undefined });
  return { name, total, used, left, label: formatDuration(left) };
}

/**
 * @param {Array<{name: string, limit: string, spent: string, floorMs?: number, ceilingMs?: number}>} requests
 */
export function estimateAll(requests) {
  return requests.map(buildBudget);
}
'''

TOOL_ESTIMATE_JS = '''import { estimateAll } from "../src/budget.js";

/**
 * CLI-style helper: render an estimate sheet.
 * @param {Array<{name: string, limit: string, spent: string, floorMs?: number, ceilingMs?: number}>} requests
 * @returns {string}
 */
export function renderSheet(requests) {
  return estimateAll(requests)
    .map((row) => `${row.name}: ${row.label} left of ${formatOf(row)}`)
    .join("\\n");

  function formatOf(row) {
    const raw = row.total;
    return `${raw}ms`;
  }
}
'''

RUN_TESTS_MJS = '''#!/usr/bin/env node
/** Tiny zero-dependency test runner built on node:test (node >= 20). */
import { run } from "node:test";
import { readdirSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const requested = process.argv.slice(2);

/** @type {string[]} */
let targets;
if (requested.length > 0) {
  targets = requested.map((entry) => path.resolve(root, entry));
} else {
  const testsDir = path.join(root, "tests");
  targets = existsSync(testsDir)
    ? readdirSync(testsDir)
        .filter((name) => name.endsWith(".test.mjs"))
        .sort()
        .map((name) => path.join(testsDir, name))
    : [];
}

if (targets.length === 0) {
  console.error("no test files found under tests/ (expected tests/*.test.mjs)");
  process.exit(1);
}

let passed = 0;
let failed = 0;
for await (const event of run({ files: targets })) {
  if (event.type === "test:pass") {
    passed += 1;
  } else if (event.type === "test:fail") {
    failed += 1;
    console.error(`FAIL ${event.data.name ?? "(unnamed)"}`);
    const message = event.data.details?.error?.message;
    if (message) console.error(`     ${String(message).split("\\n")[0]}`);
  }
}
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed === 0 && passed > 0 ? 0 : 1;
'''

FILES = {
    ".gitignore": "node_modules/\n",
    "package.json": PACKAGE_JSON,
    "src/duration.js": DURATION_JS,
    "src/budget.js": BUDGET_JS,
    "tools/estimate.js": TOOL_ESTIMATE_JS,
    "tests/.gitkeep": "",
    "run_tests.mjs": RUN_TESTS_MJS,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "duracore: undocumented duration utilities", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
