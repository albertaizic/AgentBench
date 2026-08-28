"""Regenerate reference/fix.patch for ts-serialcompat (tolerant reader, strict writer)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixture"

ENVELOPE_JS_FIXED = '''import { normalizeManifest } from "./records.js";

export const FORMAT = "shipmanifest";
export const VERSION = 2;

/**
 * Serializes a manifest into a canonical v2 envelope. The input is always
 * normalized first, so either naming convention yields byte-identical output
 * and structurally invalid manifests are rejected.
 * @param {any} manifest
 * @returns {string} canonical single-line JSON envelope
 */
export function encode(manifest) {
  return JSON.stringify({
    format: FORMAT,
    version: VERSION,
    payload: normalizeManifest(manifest),
  });
}

/**
 * Rebuilds a canonical manifest from a legacy v1 envelope: snake_case field
 * names map to their camelCase successors and metric tons convert to
 * kilograms exactly (1 t = 1000 kg).
 * @param {any} doc
 */
function fromV1(doc) {
  const legacy = doc.manifest ?? {};
  const cargo = Array.isArray(legacy.items)
    ? legacy.items.map((item) => ({
        sku: item.item_code,
        qty: item.quantity,
        weightKg: Number(item.mass_tons) * 1000,
      }))
    : [];
  return normalizeManifest({
    id: legacy.manifest_id,
    vessel: legacy.vessel_name,
    port: legacy.dock,
    sealedAt: legacy.sealed_at,
    cargo,
  });
}

/**
 * Parses one envelope back into a canonical manifest. Accepts the current v2
 * format and archived v1 envelopes; everything else fails with a clear error.
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
  if (doc.format === FORMAT && doc.version === VERSION) {
    return normalizeManifest(doc.payload);
  }
  if (doc.format === "shipmanifest.v1") {
    return fromV1(doc);
  }
  if (doc.format !== FORMAT) {
    throw new TypeError(`not a shipmanifest envelope: format ${String(doc.format)}`);
  }
  throw new Error(`unsupported envelope version: ${String(doc.version)}`);
}
'''

FIX_FILES = {
    "src/envelope.js": ENVELOPE_JS_FIXED,
}


def make_patch() -> Path:
    work = Path(tempfile.mkdtemp(prefix="agentbench-ref-ts-serialcompat-"))
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
