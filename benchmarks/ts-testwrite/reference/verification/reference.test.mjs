import test from "node:test";
import assert from "node:assert/strict";

// Reference verification suite for ts-testwrite: must kill ALL 11 seeded
// mutants. Used only for maintenance verification (see reference/NOTES.md);
// it is NOT part of the fixture.

const { parseDuration, formatDuration, clampDuration } = await import("../src/duration.js");

test("parseDuration unit values", () => {
  assert.equal(parseDuration("1ms"), 1);
  assert.equal(parseDuration("2s"), 2000);
  assert.equal(parseDuration("3m"), 180_000);
  assert.equal(parseDuration("2h"), 7_200_000);
  assert.equal(parseDuration("1d"), 86_400_000);
});

test("parseDuration compound expressions", () => {
  assert.equal(parseDuration("1h30m"), 5_400_000);
  assert.equal(parseDuration("1d2h3m4s500ms"), 93_784_500);
  assert.equal(parseDuration("90m"), 5_400_000);
  // descending order enforced
  assert.throws(() => parseDuration("1m30m"), RangeError);
  assert.throws(() => parseDuration("30s1h"), RangeError);
});

test("parseDuration signs and whitespace", () => {
  assert.equal(parseDuration("-15m"), -900_000);
  assert.equal(parseDuration("  1h 30m "), 5_400_000);
  assert.ok(Object.is(parseDuration("-0s"), -0));
});

test("parseDuration rejects invalid input distinctly", () => {
  assert.throws(() => parseDuration(""), /empty/);
  assert.throws(() => parseDuration("   "), RangeError);
  assert.throws(() => parseDuration("30"), RangeError);
  assert.throws(() => parseDuration("abc"), RangeError);
  assert.throws(() => parseDuration("5x"), RangeError);
  assert.throws(() => parseDuration(42), TypeError);
  assert.throws(() => parseDuration(null), TypeError);
});

test("formatDuration canonical shapes", () => {
  assert.equal(formatDuration(0), "0ms");
  assert.equal(formatDuration(-0), "0ms");
  assert.equal(formatDuration(5_400_000), "1h30m");
  assert.equal(formatDuration(61_000), "1m1s");
  assert.equal(formatDuration(86_400_000), "1d"); // exact multiple boundary
  assert.equal(formatDuration(60_000), "1m");
  assert.equal(formatDuration(1500), "1s500ms");
  assert.equal(formatDuration(250), "250ms");
  assert.equal(formatDuration(-1500), "-1s500ms");
  assert.equal(formatDuration(900_61000), "1d1h1m1s");
});

test("formatDuration rejects non-finite input", () => {
  assert.throws(() => formatDuration(Number.NaN), TypeError);
  assert.throws(() => formatDuration(Number.POSITIVE_INFINITY), TypeError);
  assert.throws(() => formatDuration("1s"), TypeError);
});

test("clampDuration bounds are inclusive and optional", () => {
  assert.equal(clampDuration(500, { min: 1000 }), 1000);
  assert.equal(clampDuration(1000, { min: 1000 }), 1000); // at-min equality stays
  assert.equal(clampDuration(999, { min: 1000 }), 1000);
  assert.equal(clampDuration(5000, { max: 4000 }), 4000);
  assert.equal(clampDuration(4000, { max: 4000 }), 4000); // at-max equality stays
  assert.equal(clampDuration(4001, { max: 4000 }), 4000);
  assert.equal(clampDuration(2500, { min: 1000, max: 4000 }), 2500);
  assert.equal(clampDuration(-500), -500); // absent bounds pass through
  assert.throws(() => clampDuration(Number.NaN, {}), TypeError);
});
