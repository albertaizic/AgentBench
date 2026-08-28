/**
 * Hidden facade-contract evaluator for ts-apirefact.
 *
 * Runs OUTSIDE the agent workspace (cwd = this directory). The workspace
 * root arrives as argv[2]; all imports resolve against it. Uses a different
 * dataset than the public tests so special-casing public inputs cannot pass.
 */
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspace = process.argv[2];
if (!workspace) {
  console.error("usage: node facade_contract.test.mjs <workspace-root>");
  process.exit(1);
}

const mod = (rel) => import(pathToFileURL(path.join(workspace, rel)).href);

const { createTransport } = await mod("src/http.js");
const { createClient } = await mod("src/index.js");

function routes() {
  return {
    "GET /v2/users": [
      { id: "e-77", name: "Rosalind Franklin", email: "rosalind@corp.example" },
      { id: "e-12", name: "Alan Turing", email: "alan@corp.example" },
      { id: "e-99", name: "Grace Hopper", email: "grace@corp.example" },
    ],
    "GET /v2/users/e-12": { id: "e-12", name: "Alan Turing", email: "alan@corp.example" },
    "GET /v2/users/e-404": new Error("upstream 500"),
    "POST /v2/users": { id: "e-100", name: "New Hire", email: "hire@corp.example" },
    "DELETE /v2/users/e-100": { deleted: true },
    "GET /v2/projects": [
      { id: "pr-5", title: "[stale] Nightly Reindex", open: true },
      { id: "pr-6", title: "Quantum Ledger", open: true },
      { id: "pr-7", title: "Legacy Importer", open: false },
    ],
    "GET /v2/projects/pr-6": { id: "pr-6", title: "Quantum Ledger", open: true },
    "POST /v2/projects": { id: "pr-8", title: "Fresh", open: true },
    "POST /v2/projects/pr-5/close": { id: "pr-5", title: "[stale] Nightly Reindex", open: false },
  };
}

test("facade resources work on an unseen dataset over the exact wire paths", async () => {
  const transport = createTransport(routes());
  const client = createClient({ transport });

  assert.equal((await client.users.list()).length, 3);
  const user = await client.users.get("e-12");
  assert.equal(user.email, "alan@corp.example");
  assert.deepEqual(await client.users.create({ name: "x", email: "y@z" }), {
    id: "e-100",
    name: "New Hire",
    email: "hire@corp.example",
  });
  assert.deepEqual(await client.users.remove("e-100"), { deleted: true });

  assert.equal((await client.projects.list()).length, 3);
});

test("resources keep hitting the historical wire paths", async () => {
  const transport = createTransport(routes());
  const client = createClient({ transport });

  await client.users.list();
  await client.users.get("e-12");
  await client.projects.list();
  await client.projects.close("pr-5");
  assert.deepEqual(transport.requests(), [
    "GET /v2/users",
    "GET /v2/users/e-12",
    "GET /v2/projects",
    "POST /v2/projects/pr-5/close",
  ]);
});

test("legacy flat surface returns identical results to the resources", async () => {
  const { ApiClient } = await mod("src/client.js");
  const client = new ApiClient(createTransport(routes()));
  const namespaced = createClient({ transport: createTransport(routes()) });

  assert.deepEqual(await client.listUsers(), await namespaced.users.list());
  assert.deepEqual(await client.getUser("e-12"), await namespaced.users.get("e-12"));
  assert.deepEqual(
    await client.createProject({ title: "t" }),
    await namespaced.projects.create({ title: "t" }),
  );
  assert.deepEqual(await client.listProjects(), await namespaced.projects.list());
});

test("flat calls warn with ACME-DEP-API at most once per instance per method", async (t) => {
  const { ApiClient } = await mod("src/client.js");
  const warnings = [];
  const original = process.emitWarning;
  process.emitWarning = (warning, options) => {
    warnings.push({ message: String(warning), code: options?.code ?? "" });
  };
  t.after(() => {
    process.emitWarning = original;
  });

  const clientA = new ApiClient(createTransport(routes()));
  await clientA.listUsers();
  await clientA.listUsers();
  await clientA.deleteUser("e-100").catch(() => {});
  const countListUsers = warnings.filter((w) => w.message.includes("listUsers")).length;
  assert.equal(countListUsers, 1, `expected one listUsers warning, saw ${countListUsers}`);
  assert.ok(warnings.every((w) => w.code === "ACME-DEP-API"));

  // A fresh instance warns again — the once-per-method scope is per instance.
  warnings.length = 0;
  const clientB = new ApiClient(createTransport(routes()));
  await clientB.listUsers();
  assert.equal(warnings.filter((w) => w.message.includes("listUsers")).length, 1);
});

test("resource calls never trigger the deprecation channel", async (t) => {
  const warnings = [];
  const original = process.emitWarning;
  process.emitWarning = (warning, options) => {
    warnings.push(String(warning));
  };
  t.after(() => {
    process.emitWarning = original;
  });

  const client = createClient({ transport: createTransport(routes()) });
  await client.users.list();
  await client.users.get("e-12");
  await client.projects.close("pr-5");
  assert.equal(warnings.length, 0, `unexpected deprecation warnings: ${warnings.join(" | ")}`);
});

test("transport failures propagate identically through both surfaces", async () => {
  const { ApiClient } = await mod("src/client.js");
  const failing = createTransport({ "GET /v2/users/e-404": new Error("upstream 500") });
  const throughFacade = createClient({ transport: createTransport({ "GET /v2/users/e-404": new Error("upstream 500") }) });

  await assert.rejects(() => new ApiClient(failing).getUser("e-404"), /upstream 500/);
  await assert.rejects(() => throughFacade.users.get("e-404"), /upstream 500/);
});

test("migrated tools emit no deprecation warnings and keep their output format", async (t) => {
  const warnings = [];
  const original = process.emitWarning;
  process.emitWarning = (warning, options) => {
    warnings.push(String(warning));
  };
  t.after(() => {
    process.emitWarning = original;
  });

  const dumpUsers = await mod("tools/dump_users.js");
  const auditProjects = await mod("tools/audit_projects.js");

  const dump = await dumpUsers.main(createTransport(routes()));
  assert.match(dump, /^- e-77: Rosalind Franklin <rosalind@corp\.example>/m);
  assert.match(dump, /^\+ e-100: New Hire/m);

  const audit = await auditProjects.main(createTransport(routes()));
  assert.match(audit, /closed pr-5/m);
  assert.match(audit, /- pr-7: Legacy Importer \[closed\]/m);

  assert.equal(warnings.length, 0, `tools must not warn after migration: ${warnings.join(" | ")}`);
});
