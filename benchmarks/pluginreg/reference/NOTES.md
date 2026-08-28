# pluginreg — defect notes

## Defect mechanism

Both regressions live in `PluginRegistry.register()`
(pluginreg/registry.py):

1. **Order dependence.** `register()` eagerly raises
   `UnknownDependencyError` when any entry of `spec.requires` is not yet
   registered. Manifests list plugins arbitrarily, so a valid manifest whose
   dependents happen to appear before their dependencies is rejected at load
   time — even though `resolve_activation_order()` exists precisely to sort
   this out later.
2. **Case-sensitive duplicate detection.** The duplicate check tests raw
   display names (`spec.name in self._specs`) instead of normalized names,
   and `_by_norm` is keyed by the RAW name too. So `"Cache"` and `"cache"`
   both register, and dependency edges written in a different case than the
   registering name fail to bind (`normalize(dep)` lookup against a raw-name
   key).

## Reference fix

- `register()` drops the eager dependency walk entirely; it only checks
  duplicate names using `normalize(name)` and indexes `_by_norm` under the
  normalized key.
- Missing-dependency and cycle detection remain (already correctly
  implemented) in `resolve_activation_order()`, which now runs against the
  fully registered set; error messages continue to name the offending
  plugin/dependency.
- Deterministic tie-breaking (registration order among ready nodes) and
  hook attachment in `activate()` are untouched.

## Why it discriminates

- Baseline fails public tests: reversed-order manifests raise during
  registration, and case-duplicate plugins slip through.
- Fixes that only relax registration but leave raw-key `_by_norm` fail
  hidden case/padding binding checks (`" Cache "` → registered `cache`).
- Fixes that special-case the public graph fail hidden graphs: five-deep
  scrambled chains, diamond ties broken by registration order, two-node
  cycles spanning case differences, and whitespace-padded duplicates.
