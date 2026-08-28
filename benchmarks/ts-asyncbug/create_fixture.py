"""Deterministic generator for the ts-asyncbug fixture (unawaited promise drop)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

PACKAGE_JSON = (
    '{\n'
    '  "name": "taskpipe",\n'
    '  "version": "0.9.2",\n'
    '  "type": "module",\n'
    '  "description": "Deterministic job queue and batch pipeline"\n'
    '}\n'
)

QUEUE_JS = '''/**
 * TaskQueue: FIFO job dispatch with per-kind handlers and outcome tracking.
 *
 * A job is `{ id, kind, payload }`. Handlers receive the payload and may
 * return a value or throw. Outcomes land in `queue.results`, aggregate
 * counters live in `queue.stats`.
 */
let NEXT_ID = 1;

export class TaskQueue {
  constructor() {
    /** @type {Array<{id: number, kind: string, payload: any}>} */
    this.pending = [];
    /** @type {Map<string, (payload: any) => any>} */
    this.handlers = new Map();
    /** @type {Map<number, {status: string, value?: any, error?: any}>} */
    this.results = new Map();
    this.closed = false;
    this.stats = { enqueued: 0, completed: 0, failed: 0, dropped: 0 };
  }

  /** @param {string} kind @param {(payload: any) => any} handler */
  register(kind, handler) {
    this.handlers.set(kind, handler);
  }

  /**
   * Enqueues one job; returns the stored job, or null when the queue is
   * already closed (the attempt is counted as dropped).
   * @param {{kind: string, payload: any}} spec
   */
  enqueue(spec) {
    if (this.closed) {
      this.stats.dropped += 1;
      return null;
    }
    const job = { id: NEXT_ID++, kind: spec.kind, payload: spec.payload };
    this.pending.push(job);
    this.stats.enqueued += 1;
    return job;
  }

  /**
   * Dispatches every pending job.
   *
   * BUG: the handler promise is neither awaited nor tracked, so flush()
   * resolves before outcomes are recorded. Under load callers observe
   * completed=failed=0 right after awaiting flush(), and close() discards
   * the still-unrecorded work entirely.
   */
  async flush() {
    const batch = this.pending.splice(0);
    for (const job of batch) {
      const handler = this.handlers.get(job.kind);
      if (!handler) {
        this.results.set(job.id, { status: "failed", error: new Error(`no handler for kind ${job.kind}`) });
        this.stats.failed += 1;
        continue;
      }
      Promise.resolve()
        .then(() => handler(job.payload))
        .then(
          (value) => {
            this.results.set(job.id, { status: "completed", value });
            this.stats.completed += 1;
          },
          (error) => {
            this.results.set(job.id, { status: "failed", error });
            this.stats.failed += 1;
          },
        );
    }
  }

  /**
   * Closes the queue: later enqueues are rejected as dropped.
   *
   * BUG: returns before any in-flight work settles, so jobs flushed just
   * before close() lose their outcome bookkeeping.
   */
  async close() {
    this.closed = true;
    this.pending.length = 0;
  }
}
'''

HANDLERS_JS = '''/**
 * Sample handler registry used by tools and tests. Handlers are pure and
 * either return synchronously or with an already-resolved result so tests
 * stay deterministic.
 */

/** @returns {Record<string, (payload: any) => any>} */
export function defaultHandlers() {
  return {
    double: (n) => n * 2,
    label: (text) => ({ label: String(text).toUpperCase() }),
    sum: (numbers) => numbers.reduce((a, b) => a + b, 0),
    boom: () => {
      throw new Error("handler exploded");
    },
  };
}
'''

SCHEDULER_JS = '''import { TaskQueue } from "./queue.js";

/**
 * Runs a batch through a fresh TaskQueue and returns the outcome report.
 *
 * Jobs whose handler throws are retried up to `maxAttempts` times total.
 *
 * BUG: the retry decision and the final report both read queue.stats
 * before the (untracked) handler promises record anything, so failures
 * are never retried and the report claims every job vanished:
 * processed=0 even when every handler succeeds.
 *
 * @param {Array<{kind: string, payload: any}>} jobs
 * @param {Record<string, (payload: any) => any>} handlers
 * @param {{maxAttempts?: number}} [options]
 * @returns {Promise<{total: number, succeeded: number, failed: Array<{id: number, error: any}>}>}
 */
export async function runPipeline(jobs, handlers, { maxAttempts = 2 } = {}) {
  const queue = new TaskQueue();
  for (const [kind, handler] of Object.entries(handlers)) {
    queue.register(kind, handler);
  }

  for (const spec of jobs) {
    queue.enqueue(spec);
  }

  await queue.flush();

  if (maxAttempts > 1 && queue.stats.failed > 0) {
    // Retried jobs would be re-enqueued here; the stale counters above mean
    // this branch never fires in practice.
    await queue.flush();
  }

  await queue.close();

  const failed = [];
  for (const [, outcome] of queue.results) {
    if (outcome.status === "failed") {
      failed.push(outcome);
    }
  }
  return {
    total: jobs.length,
    succeeded: queue.stats.completed,
    failed,
  };
}
'''

TESTS_PIPELINE_MJS = '''import test from "node:test";
import assert from "node:assert/strict";

import { TaskQueue } from "../src/queue.js";
import { defaultHandlers } from "../src/handlers.js";
import { runPipeline } from "../src/scheduler.js";

test("flush accounts for every job before it resolves", async () => {
  const queue = new TaskQueue();
  queue.register("double", (n) => n * 2);

  for (const n of [1, 2, 3, 4]) {
    queue.enqueue({ kind: "double", payload: n });
  }

  await queue.flush();

  assert.equal(queue.stats.completed, 4, "all four jobs must be completed");
  assert.equal(queue.stats.failed, 0);
  assert.equal(queue.results.get(1).value, 2);
  assert.equal(queue.results.get(4).value, 8);
});

test("handler errors are recorded as failures, not silently lost", async () => {
  const queue = new TaskQueue();
  for (const [kind, handler] of Object.entries(defaultHandlers())) {
    queue.register(kind, handler);
  }

  queue.enqueue({ kind: "sum", payload: [10, 20] });
  queue.enqueue({ kind: "boom", payload: null });

  await queue.flush();

  assert.equal(queue.stats.completed, 1);
  assert.equal(queue.stats.failed, 1);
  const boom = [...queue.results.values()].find((r) => r.status === "failed");
  assert.match(String(boom.error), /handler exploded/);
});

test("unknown kinds fail explicitly instead of vanishing", async () => {
  const queue = new TaskQueue();
  queue.enqueue({ kind: "nope", payload: 1 });

  await queue.flush();

  assert.equal(queue.stats.enqueued, 1);
  assert.equal(queue.stats.failed, 1);
});

test("runPipeline reports accurate counts for a mixed workload", async () => {
  const report = await runPipeline(
    [
      { kind: "double", payload: 21 },
      { kind: "label", payload: "ready" },
      { kind: "sum", payload: [1, 2, 3] },
      { kind: "boom", payload: undefined },
    ],
    defaultHandlers(),
  );

  assert.equal(report.total, 4);
  assert.equal(report.succeeded, 3, "three handlers succeed");
  assert.equal(report.failed.length, 1);
});

test("close waits for in-flight work; nothing is dropped across close", async () => {
  const queue = new TaskQueue();
  queue.register("label", (t) => t.toUpperCase());

  queue.enqueue({ kind: "label", payload: "alpha" });
  queue.enqueue({ kind: "label", payload: "beta" });

  await queue.flush();
  await queue.close();

  assert.equal(queue.stats.completed, 2);
  assert.equal(queue.stats.dropped, 0);
});

test("enqueue after close is rejected as an explicit drop", async () => {
  const queue = new TaskQueue();
  queue.register("double", (n) => n * 2);
  await queue.close();

  const rejected = queue.enqueue({ kind: "double", payload: 5 });
  assert.equal(rejected, null);
  assert.equal(queue.stats.dropped, 1);
});
'''

RUN_TESTS_MJS = '''#!/usr/bin/env node
/** Tiny zero-dependency test runner built on node:test (node >= 20). */
import { run } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const requested = process.argv.slice(2);
const targets = (requested.length > 0 ? requested : ["tests/pipeline.test.mjs"])
  .map((entry) => path.resolve(root, entry));

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
    "src/queue.js": QUEUE_JS,
    "src/handlers.js": HANDLERS_JS,
    "src/scheduler.js": SCHEDULER_JS,
    "tests/pipeline.test.mjs": TESTS_PIPELINE_MJS,
    "run_tests.mjs": RUN_TESTS_MJS,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "taskpipe: deterministic job queue", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
