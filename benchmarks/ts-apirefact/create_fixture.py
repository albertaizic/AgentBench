"""Deterministic generator for the ts-apirefact fixture (API facade migration)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

GITIGNORE = "node_modules/\n"

PACKAGE_JSON = (
    '{\n'
    '  "name": "acmefetch",\n'
    '  "version": "1.4.0",\n'
    '  "type": "module",\n'
    '  "description": "Internal API client for the Acme directory service"\n'
    '}\n'
)

HTTP_JS = '''/**
 * In-memory transport used by tests and offline tooling.
 *
 * Routes map a "<METHOD> <path>" key to either a plain payload (returned to
 * callers wrapped in the standard `{ data: ... }` response envelope) or an
 * Error instance to simulate upstream failures. Every call is appended to a
 * request log so tests can assert on the wire traffic.
 *
 * @typedef {{
 *   get: (p: string) => Promise<any>,
 *   post: (p: string, body?: any) => Promise<any>,
 *   del: (p: string) => Promise<any>,
 *   requests: () => string[],
 * }} Transport
 */

/** @param {Record<string, any>} routes */
export function createTransport(routes = {}) {
  /** @type {string[]} */
  const log = [];

  /**
   * @param {string} method
   * @param {string} p
   * @param {any=} body
   */
  async function call(method, p, body) {
    log.push(`${method} ${p}`);
    const key = `${method} ${p}`;
    if (!Object.hasOwn(routes, key)) {
      throw new Error(`no route registered for ${key}`);
    }
    const value = routes[key];
    if (value instanceof Error) {
      throw value;
    }
    return structuredClone({ data: value ?? null });
  }

  return {
    /** @param {string} p */ get: (p) => call("GET", p),
    /** @param {string} p */ post: (p, body) => call("POST", p, body),
    /** @param {string} p */ del: (p) => call("DELETE", p),
    /** @returns {string[]} snapshot of issued request lines */
    requests: () => [...log],
  };
}
'''

CLIENT_JS = '''/**
 * Monolithic API client for the Acme directory service (legacy surface).
 *
 * Every operation inlines its own path building and response-envelope
 * handling, which is why this file keeps growing every sprint. New
 * operations are still being added here instead of behind resource
 * boundaries.
 */

/**
 * Unwraps the standard `{ data: ... }` response envelope.
 * @param {any} envelope
 * @returns {any} the payload carried by the envelope
 */
function unwrap(envelope) {
  if (
    envelope === null ||
    typeof envelope !== "object" ||
    !Object.hasOwn(envelope, "data")
  ) {
    throw new Error("malformed response envelope");
  }
  return envelope.data;
}

export class ApiClient {
  /** @param {import("./http.js").Transport} transport */
  constructor(transport) {
    this.transport = transport;
  }

  /** @returns {Promise<Array<{id: string, name: string, email: string}>>} */
  async listUsers() {
    return unwrap(await this.transport.get("/v2/users"));
  }

  /** @param {string} id */
  async getUser(id) {
    return unwrap(await this.transport.get(`/v2/users/${encodeURIComponent(id)}`));
  }

  /** @param {{name: string, email: string}} data */
  async createUser(data) {
    return unwrap(await this.transport.post("/v2/users", data));
  }

  /** @param {string} id */
  async deleteUser(id) {
    return unwrap(await this.transport.del(`/v2/users/${encodeURIComponent(id)}`));
  }

  /** @returns {Promise<Array<{id: string, title: string, open: boolean}>>} */
  async listProjects() {
    return unwrap(await this.transport.get("/v2/projects"));
  }

  /** @param {string} id */
  async getProject(id) {
    return unwrap(await this.transport.get(`/v2/projects/${encodeURIComponent(id)}`));
  }

  /** @param {{title: string}} data */
  async createProject(data) {
    return unwrap(await this.transport.post("/v2/projects", data));
  }

  /** @param {string} id */
  async closeProject(id) {
    return unwrap(
      await this.transport.post(`/v2/projects/${encodeURIComponent(id)}/close`),
    );
  }
}
'''

INDEX_JS_OLD = '''// Legacy entry point: the whole surface is the monolithic class.
export { ApiClient } from "./client.js";
export default ApiClient;
'''

TOOL_DUMP_USERS_JS = '''import { ApiClient } from "../src/client.js";

/**
 * Renders a directory listing plus a temporary-user smoke record.
 * @param {import("../src/http.js").Transport} transport
 * @returns {Promise<string>}
 */
export async function main(transport) {
  const client = new ApiClient(transport);
  const users = await client.listUsers();

  /** @type {string[]} */
  const lines = [];
  for (const user of users) {
    lines.push(`- ${user.id}: ${user.name} <${user.email}>`);
  }

  const created = await client.createUser({
    name: "Temp User",
    email: "temp@acme.example",
  });
  lines.push(`+ ${created.id}: ${created.name}`);

  await client.deleteUser(created.id);
  return lines.join("\\n");
}
'''

TOOL_AUDIT_PROJECTS_JS = '''import { ApiClient } from "../src/client.js";

/**
 * Audits project state: lists titles with their open/closed status and
 * closes any project whose title starts with "[stale]".
 * @param {import("../src/http.js").Transport} transport
 * @returns {Promise<string>}
 */
export async function main(transport) {
  const client = new ApiClient(transport);
  const projects = await client.listProjects();

  /** @type {string[]} */
  const lines = [];
  for (const project of projects) {
    lines.push(`- ${project.id}: ${project.title} [${project.open ? "open" : "closed"}]`);
  }

  for (const project of projects) {
    if (project.title.startsWith("[stale]") && project.open) {
      await client.closeProject(project.id);
      lines.push(`closed ${project.id}`);
    }
  }

  return lines.join("\\n");
}
'''

TESTS_FACADE_MJS = '''import test from "node:test";
import assert from "node:assert/strict";

import { createClient } from "../src/index.js";
import { createTransport } from "../src/http.js";

function userRoutes() {
  return {
    "GET /v2/users": [
      { id: "u-1", name: "Ada Lovelace", email: "ada@acme.example" },
      { id: "u-2", name: "Blaise Pascal", email: "blaise@acme.example" },
    ],
    "GET /v2/users/u-1": { id: "u-1", name: "Ada Lovelace", email: "ada@acme.example" },
    "POST /v2/users": { id: "u-new", name: "Created", email: "c@acme.example" },
    "DELETE /v2/users/u-new": { deleted: true },
  };
}

function projectRoutes() {
  return {
    "GET /v2/projects": [
      { id: "p-1", title: "Atlas", open: true },
      { id: "p-2", title: "Borealis", open: false },
    ],
    "GET /v2/projects/p-1": { id: "p-1", title: "Atlas", open: true },
    "POST /v2/projects": { id: "p-new", title: "Created", open: true },
    "POST /v2/projects/p-1/close": { id: "p-1", title: "Atlas", open: false },
  };
}

test("client.users exposes the four user operations", async () => {
  const transport = createTransport(userRoutes());
  const client = createClient({ transport });

  assert.deepEqual(await client.users.list(), [
    { id: "u-1", name: "Ada Lovelace", email: "ada@acme.example" },
    { id: "u-2", name: "Blaise Pascal", email: "blaise@acme.example" },
  ]);
  assert.deepEqual(await client.users.get("u-1"), {
    id: "u-1",
    name: "Ada Lovelace",
    email: "ada@acme.example",
  });
});

test("directory tools keep their output after migrating", async () => {
  const { main: dumpUsers } = await import("../tools/dump_users.js");
  const { main: auditProjects } = await import("../tools/audit_projects.js");

  const dump = await dumpUsers(createTransport(userRoutes()));
  assert.match(dump, /- u-1: Ada Lovelace <ada@acme\\.example>/);
  assert.match(dump, /\\+ u-new: Created/);

  const audit = await auditProjects(createTransport(projectRoutes()));
  assert.match(audit, /- p-1: Atlas \\[open\\]/);
});

test("flat calls still work and warn once per instance per method", async (t) => {
  const { ApiClient } = await import("../src/client.js");
  const transport = createTransport(userRoutes());

  /** @type {{message: string, code: string}[]} */
  const warnings = [];
  const original = process.emitWarning;
  process.emitWarning = (warning, options) => {
    warnings.push({ message: String(warning), code: options?.code ?? "" });
  };
  t.after(() => {
    process.emitWarning = original;
  });

  const client = new ApiClient(transport);
  assert.deepEqual(await client.listUsers(), [
    { id: "u-1", name: "Ada Lovelace", email: "ada@acme.example" },
    { id: "u-2", name: "Blaise Pascal", email: "blaise@acme.example" },
  ]);
  // A second identical call must NOT produce another warning.
  await client.listUsers();
  const created = await client.createUser({ name: "Created", email: "c@acme.example" });
  assert.equal(created.id, "u-new");

  assert.ok(warnings.every((w) => w.code === "ACME-DEP-API"));
  assert.equal(
    warnings.filter((w) => w.message.includes("listUsers")).length,
    1,
    "listUsers must warn exactly once",
  );
  assert.ok(warnings.some((w) => w.message.includes("createUser")));
});

test("client.projects exposes list/get/create/close", async () => {
  const transport = createTransport(projectRoutes());
  const client = createClient({ transport });

  assert.equal((await client.projects.list()).length, 2);
  assert.equal((await client.projects.get("p-1")).title, "Atlas");
  assert.equal((await client.projects.create({ title: "Created" })).id, "p-new");
  const closed = await client.projects.close("p-1");
  assert.equal(closed.open, false);
});

test("resources hit the same wire paths as before", async () => {
  const transport = createTransport({ ...userRoutes(), ...projectRoutes() });
  const client = createClient({ transport });

  await client.users.list();
  await client.users.get("u-1");
  await client.projects.list();
  await client.projects.close("p-1");

  assert.deepEqual(transport.requests(), [
    "GET /v2/users",
    "GET /v2/users/u-1",
    "GET /v2/projects",
    "POST /v2/projects/p-1/close",
  ]);
});

'''

RUN_TESTS_MJS = '''#!/usr/bin/env node
/** Tiny zero-dependency test runner built on node:test (node >= 20). */
import { run } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const requested = process.argv.slice(2);
const targets = (requested.length > 0 ? requested : ["tests/facade.test.mjs"])
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
    ".gitignore": GITIGNORE,
    "package.json": PACKAGE_JSON,
    "src/http.js": HTTP_JS,
    "src/client.js": CLIENT_JS,
    "src/index.js": INDEX_JS_OLD,
    "tools/dump_users.js": TOOL_DUMP_USERS_JS,
    "tools/audit_projects.js": TOOL_AUDIT_PROJECTS_JS,
    "tests/facade.test.mjs": TESTS_FACADE_MJS,
    "run_tests.mjs": RUN_TESTS_MJS,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "acmefetch: monolithic API client", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
