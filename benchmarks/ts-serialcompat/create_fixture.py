"""Deterministic generator for the ts-serialcompat fixture (envelope versioning)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

PACKAGE_JSON = (
    '{\n'
    '  "name": "shipmanifest",\n'
    '  "version": "2.1.0",\n'
    '  "type": "module",\n'
    '  "description": "Cargo manifest serialization with versioned envelopes"\n'
    '}\n'
)

RECORDS_JS = '''/**
 * Internal cargo-manifest model and input normalization.
 *
 * Canonical field names are camelCase (`sealedAt`, `weightKg`, ...). During
 * the v1 -> v2 migration some integrations still construct manifests with
 * the legacy snake_case names, so normalization accepts both spellings and
 * always yields the canonical shape:
 *
 *   {
 *     id: string,
 *     vessel: string,
 *     port: string,          // defaults to ""
 *     sealedAt: string,
 *     cargo: [{ sku, qty, weightKg }]   // qty integer >= 0, weightKg >= 0
 *   }
 */

function requireString(value, field) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value;
}

function requireNonNegativeNumber(value, field) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${field} must be a finite number >= 0`);
  }
  return value;
}

/**
 * Coerces loose manifest input (either naming convention) into the
 * canonical model. Throws TypeError on structurally invalid input.
 * @param {any} input
 */
export function normalizeManifest(input) {
  if (input === null || typeof input !== "object") {
    throw new TypeError("manifest must be an object");
  }

  const rawCargo = Array.isArray(input.cargo)
    ? input.cargo
    : Array.isArray(input.items)
      ? input.items
      : [];

  return {
    id: requireString(input.id ?? input.manifest_id, "id"),
    vessel: requireString(input.vessel ?? input.vessel_name, "vessel"),
    port: typeof (input.port ?? input.dock) === "string" ? (input.port ?? input.dock) : "",
    sealedAt: requireString(input.sealedAt ?? input.sealed_at, "sealedAt"),
    cargo: rawCargo.map((item) => ({
      sku: requireString(item.sku ?? item.item_code, "cargo.sku"),
      qty: Math.trunc(requireNonNegativeNumber(item.qty ?? item.quantity, "cargo.qty")),
      weightKg: requireNonNegativeNumber(item.weightKg ?? item.weight_kg, "cargo.weightKg"),
    })),
  };
}
'''

ENVELOPE_JS = '''import { normalizeManifest } from "./records.js";

export const FORMAT = "shipmanifest";
export const VERSION = 2;

/**
 * Serializes a manifest into a v2 envelope.
 *
 * BUG: the input is written out as-is instead of being normalized first, so
 * manifests built with legacy snake_case fields (or partial objects) produce
 * envelopes whose payloads silently drop data on the next decode.
 *
 * @param {any} manifest
 * @returns {string} canonical single-line JSON envelope
 */
export function encode(manifest) {
  return JSON.stringify({ format: FORMAT, version: VERSION, payload: manifest });
}

/**
 * Parses one envelope back into a canonical manifest.
 *
 * BUG: only version 2 is accepted. Archives written by the pre-upgrade
 * emitter use the "shipmanifest.v1" format (snake_case payload, weights in
 * metric tons under `items[].mass_tons`) and every such line now fails with
 * "unsupported envelope version".
 *
 * @param {string} text
 * @returns {any} canonical manifest
 */
export function decode(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new TypeError("envelope is not valid JSON");
  }
  if (doc === null || typeof doc !== "object") {
    throw new TypeError("not a shipmanifest envelope");
  }
  if (doc.format !== FORMAT && doc.format !== "shipmanifest.v1") {
    throw new TypeError(`not a shipmanifest envelope: format ${String(doc.format)}`);
  }
  if (doc.version !== VERSION) {
    throw new Error(`unsupported envelope version: ${String(doc.version)}`);
  }
  return doc.payload;
}
'''

ARCHIVE_JS = '''import { decode, encode } from "./envelope.js";

/**
 * Newline-delimited envelope archive helpers. Each line is one envelope;
 * blank lines are ignored.
 */

/**
 * @param {Array<any>} manifests
 * @returns {string} archive text with one envelope per line
 */
export function renderArchive(manifests) {
  return manifests.map((m) => encode(m)).join("\\n") + (manifests.length > 0 ? "\\n" : "");
}

/**
 * Loads an archive. In tolerant mode (default) lines that cannot be decoded
 * are collected under `errors` while valid lines still load; in strict mode
 * the first undecodable line throws.
 *
 * BUG (via envelope.decode): any archive containing legacy v1 lines currently
 * fails outright even in tolerant mode.
 *
 * @param {string} text
 * @param {{tolerant?: boolean}} [options]
 * @returns {{manifests: any[], errors: Array<{line: number, error: any}>}}
 */
export function loadArchive(text, { tolerant = true } = {}) {
  /** @type {any[]} */
  const manifests = [];
  /** @type {Array<{line: number, error: any}>} */
  const errors = [];

  const lines = text.split("\\n");
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index].trim();
    if (line.length === 0) {
      continue;
    }
    try {
      manifests.push(decode(line));
    } catch (error) {
      if (!tolerant) {
        throw error;
      }
      errors.push({ line: index + 1, error });
    }
  }

  return { manifests, errors };
}
'''

TOOL_EXPORT_MANIFEST_JS = '''import { encode } from "../src/envelope.js";
import { renderArchive } from "../src/archive.js";

/**
 * Builds the archive text plus a human-readable summary for a shipment batch.
 * @param {Array<any>} manifests
 * @returns {{archive: string, summary: string}}
 */
export function exportBatch(manifests) {
  const archive = renderArchive(manifests);
  const items = manifests.flatMap((m) =>
    Array.isArray(m.cargo) ? m.cargo : Array.isArray(m.items) ? m.items : [],
  );
  const totalWeight = items.reduce(
    (acc, item) =>
      acc +
      (item.weightKg ??
        item.weight_kg ??
        (typeof item.mass_tons === "number" ? item.mass_tons * 1000 : 0)),
    0,
  );
  const summary = `${manifests.length} manifest(s), ${Math.round(totalWeight)} kg declared`;
  return { archive, summary };
}
'''

TESTS_SERIAL_MJS = '''import test from "node:test";
import assert from "node:assert/strict";

import { encode, decode } from "../src/envelope.js";
import { normalizeManifest } from "../src/records.js";
import { renderArchive, loadArchive } from "../src/archive.js";

const V2_MANIFEST = {
  id: "M-2041",
  vessel: "MV Halcyon",
  port: "ROT",
  sealedAt: "2026-02-14",
  cargo: [
    { sku: "CRG-77", qty: 12, weightKg: 340.5 },
    { sku: "CRG-91", qty: 3, weightKg: 1200 },
  ],
};

test("v2 envelopes round-trip through encode/decode", () => {
  const restored = decode(encode(V2_MANIFEST));
  assert.deepEqual(restored, normalizeManifest(V2_MANIFEST));
});

test("the writer is strict: legacy-shaped input still emits canonical bytes", () => {
  const legacyShaped = {
    manifest_id: "M-2041",
    vessel_name: "MV Halcyon",
    dock: "ROT",
    sealed_at: "2026-02-14",
    items: [
      { item_code: "CRG-77", quantity: 12, weight_kg: 340.5 },
      { item_code: "CRG-91", quantity: 3, weight_kg: 1200 },
    ],
  };

  const canonical = encode(normalizeManifest(V2_MANIFEST));
  assert.equal(encode(legacyShaped), canonical, "writer must normalize before serializing");
  assert.equal(encode(V2_MANIFEST), canonical);
  const doc = JSON.parse(canonical);
  assert.deepEqual(Object.keys(doc), ["format", "version", "payload"]);
  assert.equal(doc.version, 2);
  assert.deepEqual(
    Object.keys(doc.payload),
    ["id", "vessel", "port", "sealedAt", "cargo"],
  );
});

test("legacy v1 envelopes load again with ton-to-kg conversion", () => {
  const v1 = JSON.stringify({
    format: "shipmanifest.v1",
    manifest: {
      manifest_id: "M-1188",
      vessel_name: "SS Meridian",
      dock: "HAM-3",
      sealed_at: "2024-11-02",
      items: [
        { item_code: "IT-1", quantity: 6, mass_tons: 0.25 },
        { item_code: "IT-2", quantity: 1, mass_tons: 2 },
      ],
    },
  });

  const restored = decode(v1);
  assert.equal(restored.id, "M-1188");
  assert.equal(restored.vessel, "SS Meridian");
  assert.equal(restored.port, "HAM-3");
  assert.deepEqual(restored.cargo, [
    { sku: "IT-1", qty: 6, weightKg: 250 },
    { sku: "IT-2", qty: 1, weightKg: 2000 },
  ]);
});

test("mixed archives tolerate undecodable lines without losing the rest", () => {
  const good = encode(V2_MANIFEST);
  const junk = '{"format":"postparcel","version":9,"payload":{}}';
  const archiveText = [good, "", junk].join("\\n");

  const { manifests, errors } = loadArchive(archiveText);
  assert.equal(manifests.length, 1);
  assert.deepEqual(manifests[0], normalizeManifest(V2_MANIFEST));
  assert.equal(errors.length, 1);
  assert.match(String(errors[0].error), /not a shipmanifest envelope/);
});

test("strict loading throws on the first bad line", () => {
  assert.throws(() => loadArchive("garbage\\n", { tolerant: false }), TypeError);
});

test("blank lines never produce manifests or errors", () => {
  const { manifests, errors } = loadArchive("\\n\\n");
  assert.deepEqual(manifests, []);
  assert.deepEqual(errors, []);
});
'''

RUN_TESTS_MJS = '''#!/usr/bin/env node
/** Tiny zero-dependency test runner built on node:test (node >= 20). */
import { run } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const requested = process.argv.slice(2);
const targets = (requested.length > 0 ? requested : ["tests/serial.test.mjs"])
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
    "src/records.js": RECORDS_JS,
    "src/envelope.js": ENVELOPE_JS,
    "src/archive.js": ARCHIVE_JS,
    "tools/export_manifest.js": TOOL_EXPORT_MANIFEST_JS,
    "tests/serial.test.mjs": TESTS_SERIAL_MJS,
    "run_tests.mjs": RUN_TESTS_MJS,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "shipmanifest: versioned cargo envelopes", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
