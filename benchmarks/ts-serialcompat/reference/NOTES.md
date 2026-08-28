# ts-serialcompat — defect & task notes (maintainer-facing)

## Defect mechanism

The v2 emitter upgrade landed in `src/envelope.js` with two regressions:

1. **Reader regression** — `decode()` hard-requires `version === 2`. Every
   archived "shipmanifest.v1" envelope (snake_case payload, weights in metric
   tons under `items[].mass_tons`) now throws "unsupported envelope version",
   so any archive containing a legacy line fails to load even in tolerant
   mode.
2. **Writer regression** — `encode()` serializes its input verbatim instead
   of normalizing. Integrations that still construct manifests with
   snake_case names (`manifest_id`, `items[].weight_kg`, ...) produce
   envelopes whose payloads decode into objects missing canonical fields —
   silent data loss on the next read.

`src/records.js` already exposes `normalizeManifest`, which accepts both
naming conventions and yields the canonical model; the writer simply stopped
using it.

## Reference fix

- `encode()` normalizes through `normalizeManifest` before serializing:
  byte-identical output for either convention, TypeError on invalid input.
- `decode()` routes v2 payloads through normalization, rebuilds v1 envelopes
  (snake_case mapping + exact 1 t = 1000 kg conversion) before normalizing,
  and preserves the distinct errors: "not a shipmanifest envelope" for
  foreign formats, "unsupported envelope version" for our format with an
  unknown version, TypeError for malformed JSON.
- No changes needed in `src/archive.js`; tolerant/strict semantics work once
  `decode()` accepts both versions.

## Why it discriminates

- A reader fix that maps v1 but skips v2 normalization fails the strict-writer
  byte-stability checks (hidden test pins the full canonical JSON string).
- A writer-only fix leaves archives broken; hidden interleaved v1/v2 archive
  loads must succeed with zero errors and preserved order.
- Unit conversion is checked at the 1 t = 1000 kg boundary and with fractional
  tons (0.05 t -> 50 kg), so sloppy float handling or a x1024 guess fails.
- Public data differs from hidden data throughout.

Language note: TypeScript-suite task implemented as zero-dependency Node ESM
`.js` with JSDoc annotations; runner is node:test-based (`node run_tests.mjs`).
