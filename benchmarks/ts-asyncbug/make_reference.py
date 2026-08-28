"""Regenerate reference/fix.patch for ts-asyncbug (tracked async dispatch)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixture"

QUEUE_JS_FIXED = '''/**
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
   * Dispatches every pending job and resolves only once all dispatched work
   * has settled, so stats/results are complete by the time callers observe.
   */
  async flush() {
    const batch = this.pending.splice(0);
    /** @type {Promise<void>[]} */
    const tracked = [];
    for (const job of batch) {
      const handler = this.handlers.get(job.kind);
      if (!handler) {
        this.results.set(job.id, { status: "failed", error: new Error(`no handler for kind ${job.kind}`) });
        this.stats.failed += 1;
        continue;
      }
      tracked.push(
        (async () => {
          try {
            const value = await handler(job.payload);
            this.results.set(job.id, { status: "completed", value });
            this.stats.completed += 1;
          } catch (error) {
            this.results.set(job.id, { status: "failed", error });
            this.stats.failed += 1;
          }
        })(),
      );
    }
    await Promise.all(tracked);
  }

  /**
   * Closes the queue after draining anything still pending; later enqueues
   * are rejected as explicit drops.
   */
  async close() {
    this.closed = true;
    if (this.pending.length > 0) {
      await this.flush();
    }
  }
}
'''

SCHEDULER_JS_FIXED = '''import { TaskQueue } from "./queue.js";

/**
 * Runs a batch through a fresh TaskQueue and returns the outcome report.
 *
 * Jobs whose handler throws are retried up to `maxAttempts` times total; a
 * retried spec keeps its logical (first) id in the final report.
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

  // logicalId -> { attempts, latestResultId, spec }
  const ledger = new Map();

  for (const spec of jobs) {
    const job = queue.enqueue(spec);
    if (job) {
      ledger.set(job.id, { attempts: 1, latestResultId: job.id, spec });
    }
  }

  await queue.flush();

  let retryable = [...ledger.entries()]
    .filter(([, entry]) => {
      const outcome = queue.results.get(entry.latestResultId);
      return entry.attempts < maxAttempts && outcome?.status === "failed";
    });

  while (retryable.length > 0) {
    for (const [logicalId, entry] of retryable) {
      const job = queue.enqueue(entry.spec);
      entry.attempts += 1;
      entry.latestResultId = job ? job.id : entry.latestResultId;
      ledger.set(logicalId, entry);
    }
    await queue.flush();
    retryable = [...ledger.entries()].filter(([, entry]) => {
      const outcome = queue.results.get(entry.latestResultId);
      return entry.attempts < maxAttempts && outcome?.status === "failed";
    });
  }

  await queue.close();

  const failed = [];
  let succeeded = 0;
  for (const [logicalId, entry] of ledger) {
    const outcome = queue.results.get(entry.latestResultId);
    if (outcome?.status === "completed") {
      succeeded += 1;
    } else {
      failed.push({ id: logicalId, error: outcome?.error });
    }
  }
  return {
    total: jobs.length,
    succeeded,
    failed,
  };
}
'''

FIX_FILES = {
    "src/queue.js": QUEUE_JS_FIXED,
    "src/scheduler.js": SCHEDULER_JS_FIXED,
}


def make_patch() -> Path:
    work = Path(tempfile.mkdtemp(prefix="agentbench-ref-ts-asyncbug-"))
    try:
        shutil.copytree(FIXTURE, work, dirs_exist_ok=True)
        for relative, content in FIX_FILES.items():
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached"], cwd=work, check=True,
            capture_output=True, text=True,
        ).stdout
        out_dir = ROOT / "reference"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "fix.patch"
        out.write_text(diff, encoding="utf-8")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    patch = make_patch()
    print(f"wrote {patch}")
    sys.exit(0)
