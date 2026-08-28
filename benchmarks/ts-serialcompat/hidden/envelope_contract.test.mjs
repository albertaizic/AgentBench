/**
 * Hidden envelope-contract evaluator for ts-serialcompat.
 *
 * Runs OUTSIDE the agent workspace (cwd = this directory); workspace root is
 * argv[2]. Uses different data than the public tests: an interleaved
 * v1/v2 archive, a 1-ton boundary conversion, exact canonical bytes, and a
 * legacy-shaped batch through the export tool.
 */
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspace = process.argv[2];
if (!workspace) {
  console.error("usage: node envelope_contract.test.mjs <workspace-root>");
  process.exit(1);
}

const mod = (rel) => import(pathToFileURL(path.join(workspace, rel)).href);

const { encode, decode } = await mod("src/envelope.js");
const { normalizeManifest } = await mod("src/records.js");
const { loadArchive, renderArchive } = await mod("src/archive.js");

function v1Envelope(id) {
  return JSON.stringify({
    format: "shipmanifest.v1",
    manifest: {
      manifest_id: id,
      vessel_name: "MV Northern Light",
      dock: "BRV-1",
      sealed_at: "2025-06-30",
      items: [
        { item_code: "HX-01", quantity: 4, mass_tons: 1 },
        { item_code: "HX-02", quantity: 40, mass_tons: 0.05 },
      ],
    },
  });
}

function v2Envelope() {
  const manifest = {
    id: "M-9001",
    vessel: "Kaimana Star",
    port: "SIN",
    sealedAt: "2026-04-19",
    cargo: [{ sku: "BLK-3", qty: 9, weightKg: 615.75 }],
  };
  return encode(manifest);
}

test("v1 and v2 decode to the identical canonical shape with exact conversion", () => {
  const fromV1 = decode(v1Envelope("M-7734"));
  assert.equal(fromV1.id, "M-7734");
  assert.equal(fromV1.vessel, "MV Northern Light");
  assert.equal(fromV1.port, "BRV-1");
  assert.deepEqual(fromV1.cargo, [
    { sku: "HX-01", qty: 4, weightKg: 1000 },
    { sku: "HX-02", qty: 40, weightKg: 50 },
  ]);

  const fromV2 = decode(v2Envelope());
  assert.equal(fromV2.vessel, "Kaimana Star");
  assert.deepEqual(Object.keys(fromV2), ["id", "vessel", "port", "sealedAt", "cargo"]);
});

test("an interleaved v1/v2 archive loads completely in order with no errors", () => {
  const lines = [
    v1Envelope("M-7734"),
    "",
    v2Envelope(),
    v1Envelope("M-7735"),
  ];
  const { manifests, errors } = loadArchive(lines.join("\n"));

  assert.equal(errors.length, 0, `unexpected errors: ${errors.map(String)}`);
  assert.deepEqual(manifests.map((m) => m.id), ["M-7734", "M-9001", "M-7735"]);
});

test("encode output is byte-stable across naming conventions", () => {
  const camel = {
    id: "M-42",
    vessel: "Boreas",
    port: "PIR",
    sealedAt: "2026-01-09",
    cargo: [
      { sku: "A-1", qty: 1, weightKg: 10 },
      { sku: "B-2", qty: 2, weightKg: 20 },
    ],
  };
  const snake = {
    manifest_id: "M-42",
    vessel_name: "Boreas",
    dock: "PIR",
    sealed_at: "2026-01-09",
    items: [
      { item_code: "A-1", quantity: 1, weight_kg: 10 },
      { item_code: "B-2", quantity: 2, weight_kg: 20 },
    ],
  };

  const bytes = encode(camel);
  assert.equal(encode(snake), bytes);
  // Canonical key order inside the payload:
  assert.match(
    bytes,
    /^\{"format":"shipmanifest","version":2,"payload":\{"id":"M-42","vessel":"Boreas","port":"PIR","sealedAt":"2026-01-09","cargo":\[\{"sku":"A-1","qty":1,"weightKg":10\},\{"sku":"B-2","qty":2,"weightKg":20\}\]\}\}$/,
  );
});

test("structurally invalid manifests are rejected by the strict writer", () => {
  assert.throws(() => encode({ vessel: "no id" }), TypeError);
  assert.throws(() => encode(null), TypeError);
  assert.throws(
    () => encode({ id: "M-1", vessel: "V", port: "", sealedAt: "2026-01-01", cargo: [{ sku: "X", qty: -3 }] }),
    /cargo\.qty/,
  );
});

test("foreign formats and future versions keep their distinct errors", () => {
  assert.throws(
    () => decode('{"format":"postparcel","version":3,"payload":{}}'),
    /not a shipmanifest envelope/,
  );
  assert.throws(
    () => decode('{"format":"shipmanifest","version":3,"payload":{}}'),
    /unsupported envelope version: 3/,
  );
  assert.throws(() => decode("{not json"), TypeError);
});

test("the export tool produces decodable archives for legacy-shaped batches", async () => {
  const { exportBatch } = await mod("tools/export_manifest.js");

  const batch = {
    manifest_id: "M-555",
    vessel_name: "Old Integrator",
    dock: "TYO-8",
    sealed_at: "2025-12-24",
    items: [{ item_code: "LEG-1", quantity: 7, weight_kg: 140 }],
  };

  const { archive, summary } = exportBatch([batch]);
  assert.match(summary, /^1 manifest\(s\), 140 kg declared$/);

  const firstLine = archive.split("\n")[0];
  const restored = decode(firstLine);
  assert.equal(restored.id, "M-555");
  assert.equal(restored.port, "TYO-8");
  assert.deepEqual(restored.cargo, [{ sku: "LEG-1", qty: 7, weightKg: 140 }]);
});

test("renderArchive/loadArchive round-trip preserves duplicate skus and order", () => {
  const manifest = normalizeManifest({
    id: "M-66",
    vessel: "Duplex",
    port: "DUB",
    sealedAt: "2026-03-03",
    cargo: [
      { sku: "SAME", qty: 1, weightKg: 5 },
      { sku: "SAME", qty: 2, weightKg: 7 },
    ],
  });

  const restored = loadArchive(renderArchive([manifest])).manifests;
  assert.deepEqual(restored, [manifest]);
});
