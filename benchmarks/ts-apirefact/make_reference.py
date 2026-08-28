"""Regenerate reference/fix.patch for ts-apirefact (known-good facade migration)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixture"

RESPONSE_JS = '''/**
 * Centralized response-envelope handling.
 * @param {any} envelope
 * @returns {any} the payload carried by the `{ data: ... }` envelope
 */
export function unwrapEnvelope(envelope) {
  if (
    envelope === null ||
    typeof envelope !== "object" ||
    !Object.hasOwn(envelope, "data")
  ) {
    throw new Error("malformed response envelope");
  }
  return envelope.data;
}
'''

USERS_JS = '''import { unwrapEnvelope } from "../response.js";

/** @param {import("../http.js").Transport} transport */
export function createUsersResource(transport) {
  return {
    list: () => transport.get("/v2/users").then(unwrapEnvelope),
    get: (id) =>
      transport.get(`/v2/users/${encodeURIComponent(id)}`).then(unwrapEnvelope),
    create: (data) => transport.post("/v2/users", data).then(unwrapEnvelope),
    remove: (id) =>
      transport.del(`/v2/users/${encodeURIComponent(id)}`).then(unwrapEnvelope),
  };
}
'''

PROJECTS_JS = '''import { unwrapEnvelope } from "../response.js";

/** @param {import("../http.js").Transport} transport */
export function createProjectsResource(transport) {
  return {
    list: () => transport.get("/v2/projects").then(unwrapEnvelope),
    get: (id) =>
      transport.get(`/v2/projects/${encodeURIComponent(id)}`).then(unwrapEnvelope),
    create: (data) => transport.post("/v2/projects", data).then(unwrapEnvelope),
    close: (id) =>
      transport
        .post(`/v2/projects/${encodeURIComponent(id)}/close`)
        .then(unwrapEnvelope),
  };
}
'''

FACADE_JS = '''import { createUsersResource } from "./resources/users.js";
import { createProjectsResource } from "./resources/projects.js";

/**
 * Builds the namespaced resource facade.
 * @param {{transport: import("./http.js").Transport}} options
 */
export function createClient({ transport }) {
  if (!transport || typeof transport.get !== "function") {
    throw new TypeError("createClient requires a transport");
  }
  return {
    users: createUsersResource(transport),
    projects: createProjectsResource(transport),
  };
}
'''

CLIENT_JS_NEW = '''/**
 * Deprecated compatibility shim.
 *
 * Flat methods delegate to the resource modules and emit one deprecation
 * warning per instance per method name through Node's warning channel.
 */
import { createUsersResource } from "./resources/users.js";
import { createProjectsResource } from "./resources/projects.js";

const DEPRECATION_CODE = "ACME-DEP-API";

export class ApiClient {
  #warned = new Set();

  /** @param {import("./http.js").Transport} transport */
  constructor(transport) {
    this.transport = transport;
    this.users = createUsersResource(transport);
    this.projects = createProjectsResource(transport);
  }

  /** @param {string} method @param {string} replacement */
  #warnOnce(method, replacement) {
    if (this.#warned.has(method)) {
      return;
    }
    this.#warned.add(method);
    process.emitWarning(
      `ApiClient.${method}() is deprecated; use client.${replacement}() instead.`,
      { code: DEPRECATION_CODE },
    );
  }

  async listUsers() {
    this.#warnOnce("listUsers", "users.list");
    return this.users.list();
  }

  /** @param {string} id */
  async getUser(id) {
    this.#warnOnce("getUser", "users.get");
    return this.users.get(id);
  }

  /** @param {{name: string, email: string}} data */
  async createUser(data) {
    this.#warnOnce("createUser", "users.create");
    return this.users.create(data);
  }

  /** @param {string} id */
  async deleteUser(id) {
    this.#warnOnce("deleteUser", "users.remove");
    return this.users.remove(id);
  }

  async listProjects() {
    this.#warnOnce("listProjects", "projects.list");
    return this.projects.list();
  }

  /** @param {string} id */
  async getProject(id) {
    this.#warnOnce("getProject", "projects.get");
    return this.projects.get(id);
  }

  /** @param {{title: string}} data */
  async createProject(data) {
    this.#warnOnce("createProject", "projects.create");
    return this.projects.create(data);
  }

  /** @param {string} id */
  async closeProject(id) {
    this.#warnOnce("closeProject", "projects.close");
    return this.projects.close(id);
  }
}
'''

INDEX_JS_NEW = '''import { ApiClient } from "./client.js";

export { createClient } from "./facade.js";
export { ApiClient };
export default ApiClient;
'''

DUMP_USERS_NEW = '''import { createClient } from "../src/index.js";

/**
 * Renders a directory listing plus a temporary-user smoke record.
 * @param {import("../src/http.js").Transport} transport
 * @returns {Promise<string>}
 */
export async function main(transport) {
  const client = createClient({ transport });
  const users = await client.users.list();

  /** @type {string[]} */
  const lines = [];
  for (const user of users) {
    lines.push(`- ${user.id}: ${user.name} <${user.email}>`);
  }

  const created = await client.users.create({
    name: "Temp User",
    email: "temp@acme.example",
  });
  lines.push(`+ ${created.id}: ${created.name}`);

  await client.users.remove(created.id);
  return lines.join("\\n");
}
'''

AUDIT_PROJECTS_NEW = '''import { createClient } from "../src/index.js";

/**
 * Audits project state: lists titles with their open/closed status and
 * closes any project whose title starts with "[stale]".
 * @param {import("../src/http.js").Transport} transport
 * @returns {Promise<string>}
 */
export async function main(transport) {
  const client = createClient({ transport });
  const projects = await client.projects.list();

  /** @type {string[]} */
  const lines = [];
  for (const project of projects) {
    lines.push(`- ${project.id}: ${project.title} [${project.open ? "open" : "closed"}]`);
  }

  for (const project of projects) {
    if (project.title.startsWith("[stale]") && project.open) {
      await client.projects.close(project.id);
      lines.push(`closed ${project.id}`);
    }
  }

  return lines.join("\\n");
}
'''

FIX_FILES = {
    "src/response.js": RESPONSE_JS,
    "src/resources/users.js": USERS_JS,
    "src/resources/projects.js": PROJECTS_JS,
    "src/facade.js": FACADE_JS,
    "src/client.js": CLIENT_JS_NEW,
    "src/index.js": INDEX_JS_NEW,
    "tools/dump_users.js": DUMP_USERS_NEW,
    "tools/audit_projects.js": AUDIT_PROJECTS_NEW,
}


def make_patch() -> Path:
    work = Path(tempfile.mkdtemp(prefix="agentbench-ref-ts-apirefact-"))
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
