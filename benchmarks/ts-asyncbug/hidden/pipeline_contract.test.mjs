/**
 * Hidden pipeline-contract evaluator for ts-asyncbug.
 *
 * Runs OUTSIDE the agent workspace (cwd = this directory); workspace root
 * arrives as argv[2]. Uses different data and scenarios than the public
 * tests, including a larger batch and a flaky handler that must be retried.
 */
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspace = process.argv[2];
if (!workspace) {
  console.error("usage: node pipeline_contract.test.mjs <workspace-root>");
  process.exit(1);
}

const mod = (rel) => import(pathToFileURL(path.join(workspace, rel)).href);

const { TaskQueue } = await mod("src/queue.js");
const { runPipeline } = await mod("src/scheduler.js");
const { defaultHandlers } = await mod("src/handlers.js");

test("a 30-job batch is fully accounted for once flush resolves", async () => {
  const queue = new TaskQueue();
  const seen = [];
  queue.register("tag", (n) => `tag-${n}`);

  for (let n = 100; n < 130; n++) {
    queue.enqueue({ kind: "tag", payload: n });
  }
  await queue.flush();

  assert.equal(queue.stats.enqueued, 30);
  assert.equal(queue.stats.completed, 30, "all 30 completions must be recorded");
  assert.equal(queue.stats.failed, 0);
  for (const [id, outcome] of queue.results) {
    assert.equal(outcome.status, "completed");
    seen.push([id, outcome.value]);
  }
  assert.equal(seen.length, 30);
});

test("permanent failures stay failures; retry budget is respected", async () => {
  let calls = 0;
  const flakyThenDead = () => {
    calls += 1;
    throw new Error(`attempt ${calls} blew up`);
  };

  const report = await runPipeline(
    [
      { kind: "solid", payload: 7 },
      { kind: "cursed", payload: null },
    ],
    { solid: (n) => n + 1, cursed: flakyThenDead },
    { maxAttempts: 3 },
  );

  assert.equal(report.total, 2);
  assert.equal(report.succeeded, 1);
  assert.equal(report.failed.length, 1);
  assert.match(String(report.failed[0].error), /blew up/);
  assert.ok(calls >= 2, `cursed handler should have been retried, called ${calls}x`);
  assert.ok(calls <= 3, `retry budget exceeded: ${calls} calls`);
});

test("a flaky handler that succeeds on retry ends up succeeded exactly once", async () => {
  let firstCall = true;
  const report = await runPipeline(
    [{ kind: "flaky", payload: "payload-x" }],
    {
      flaky: (p) => {
        if (firstCall) {
          firstCall = false;
          throw new Error("transient network hiccup");
        }
        return { recovered: p };
      },
    },
  );

  assert.equal(report.total, 1);
  assert.equal(report.succeeded, 1);
  assert.equal(report.failed.length, 0);
});

test("accounting invariant holds for mixed outcomes", async () => {
  const report = await runPipeline(
    [
      { kind: "double", payload: 5 },
      { kind: "boom", payload: undefined },
      { kind: "double", payload: 12 },
      { kind: "boom", payload: undefined },
      { kind: "label", payload: "hidden" },
    ],
    defaultHandlers(),
  );

  assert.equal(report.total, 5);
  assert.equal(report.succeeded + report.failed.length, 5);
  assert.equal(report.succeeded, 3);
  assert.equal(report.failed.length, 2);
});

test("close() drains accepted work and post-close enqueues are explicit drops", async () => {
  const queue = new TaskQueue();
  queue.register("sum", (xs) => xs.reduce((a, b) => a + b, 0));

  const accepted = queue.enqueue({ kind: "sum", payload: [40, 2] });
  await queue.close();

  assert.equal(queue.stats.completed, 1, "work accepted before close must complete");
  assert.equal(queue.results.get(accepted.id).value, 42);
  assert.equal(queue.stats.dropped, 0);

  assert.equal(queue.enqueue({ kind: "sum", payload: [1] }), null);
  assert.equal(queue.stats.dropped, 1);
  assert.equal(queue.stats.enqueued, 1, "dropped attempts are not counted as enqueued");
});

test("flush on an empty queue is a no-op", async () => {
  const queue = new TaskQueue();
  await queue.flush();
  await queue.flush();
  assert.equal(queue.stats.enqueued, 0);
  assert.equal(queue.stats.completed, 0);
  assert.equal(queue.stats.failed, 0);
});
