#!/usr/bin/env node
/**
 * Hidden mutation scorer for ts-testwrite.
 *
 * Usage: node score_mutants.mjs <workspace-root>
 *
 * Applies each textual mutant to src/duration.js inside a throwaway copy of
 * the agent's workspace and re-runs the agent's suite (node run_tests.mjs).
 * A mutant is "killed" when the suite fails against it. Prints one line per
 * mutant and finishes with the fraction marker consumed by the harness:
 *
 *   agentbench-score: <killed/total>
 */
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import os from "node:os";

const workspace = process.argv[2];
if (!workspace) {
  console.error("usage: node score_mutants.mjs <workspace-root>");
  process.exit(1);
}

const SOURCE_REL = path.join("src", "duration.js");
const original = readFileSync(path.join(workspace, SOURCE_REL), "utf8");

// Every `find` snippet must occur EXACTLY once in the pristine source.
const MUTANTS = [
  { name: "seconds-unit-shrunk", find: "s: 1000,", replaceWith: "s: 100," },
  { name: "minutes-unit-shrunk", find: "m: 60_000,", replaceWith: "m: 6_000," },
  { name: "hours-unit-bumped", find: "h: 3_600_000,", replaceWith: "h: 3_600_001," },
  { name: "days-unit-doubled", find: "d: 86_400_000 }", replaceWith: "d: 172_800_000 }" },
  {
    name: "order-check-weakened",
    find: "index <= previous",
    replaceWith: "index < previous",
  },
  {
    name: "sign-flipped",
    find: "return negative ? -total : total;",
    replaceWith: "return negative ? total : -total;",
  },
  {
    name: "empty-guard-removed",
    find: "trimmed.length === 0",
    replaceWith: "trimmed.length < 0",
  },
  {
    name: "format-boundary-exclusive",
    find: "if (remaining >= size) {",
    replaceWith: "if (remaining > size) {",
  },
  {
    name: "format-sign-inverted",
    find: '(negative ? "-" : "")',
    replaceWith: '(negative ? "" : "-")',
  },
  {
    name: "clamp-min-inverted",
    find: "result < min",
    replaceWith: "result > min",
  },
  {
    name: "clamp-max-inverted",
    find: "result > max",
    replaceWith: "result < max",
  },
];

function applyMutation(source, mutant) {
  const occurrences = source.split(mutant.find).length - 1;
  if (occurrences !== 1) {
    throw new Error(
      `mutant ${mutant.name}: expected exactly 1 occurrence of ${JSON.stringify(mutant.find)}, found ${occurrences}`,
    );
  }
  return source.replace(mutant.find, mutant.replaceWith);
}

function runSuite(dir) {
  const result = spawnSync(process.execPath, ["run_tests.mjs"], {
    cwd: dir,
    encoding: "utf8",
    timeout: 120_000,
  });
  if (result.error && result.error.code === "ABORT_ERR") {
    return true; // timed out -> treat as survived (suite cannot refute)
  }
  return result.status === 0;
}

const tmp = mkdtempSync(path.join(os.tmpdir(), "duracore-mutants-"));
try {
  cpSync(workspace, tmp, {
    recursive: true,
    filter: (candidate) => {
      const base = path.basename(candidate);
      return base !== ".git" && base !== "node_modules";
    },
  });

  // The agent's suite must pass against pristine sources first.
  if (!runSuite(tmp)) {
    console.log("suite does not pass on pristine sources; no mutants evaluated");
    console.log("agentbench-score: 0");
    process.exit(0);
  }

  let killed = 0;
  for (const mutant of MUTANTS) {
    writeFileSync(path.join(tmp, SOURCE_REL), applyMutation(original, mutant));
    const survived = runSuite(tmp);
    console.log(`${survived ? "SURVIVED" : "killed  "} ${mutant.name}`);
    if (!survived) {
      killed += 1;
    }
  }
  writeFileSync(path.join(tmp, SOURCE_REL), original);

  const score = killed / MUTANTS.length;
  console.log(`${killed}/${MUTANTS.length} mutants killed`);
  console.log(`agentbench-score: ${score.toFixed(2)}`);
  process.exit(0);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
