# schemamigrate — defect notes

## Symptom

Loading any configuration document and saving it back rewrites legacy v1
(flat, unmarked) files into the v2 nested layout, and settings that the
schema does not model disappear from the output. Old tenant tooling that
still reads v1 breaks after an apparently harmless load/save cycle.

## Mechanism

`parser.loads()` correctly detects the layout and records it on the document
(`ConfigDocument.format_version`) and collects everything outside the known
schema (`database`, `logging`, `cache`) into `ConfigDocument.extras`.

The defect lives entirely on the write path: `serializer.dumps()` never
consults `document.format_version`. It rebuilds a payload from scratch by
flattening only the known sections and pushing them through
`compat.nest_payload()`, which always emits `"version": 2`. Two observable
consequences from one root cause:

1. **Silent upgrade** — a v1 document serializes as v2, violating the
   "saving never upgrades" compatibility rule.
2. **Data loss** — `extras` is never merged into the emitted payload, so
   unknown settings vanish for *both* layouts.

## Fix shape

`dumps()` branches on `document.format_version`: v2 payloads are built as
`{"version": 2}` + nested sections + `extras`; v1 payloads go through
`compat.flatten_document()` (sections flattened back to dotted keys, extras
appended). No parser/model changes are required — the information was
already captured at load time.

## Why it discriminates

Public tests pin v1-stays-v1 and extras preservation with one dataset; the
hidden evaluator exercises the same contract with different data plus edges
the public tests do not cover: non-integer version markers (string `"2"`),
non-mapping roots, scalar-valued known sections inside v2 payloads, null /
list / float values, empty documents, and triple round-trip stability. A
fix that only special-cases the public fixtures' literal keys, or one that
"fixes" the bug by always writing v1 (breaking v2 fidelity), fails hidden
assertions on the other layout.
