"""Deterministic generator for the pluginreg fixture (registry compatibility)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {}
FILES[".gitignore"] = "__pycache__/\n*.pyc\n.pytest_cache/\n"
FILES["pyproject.toml"] = '[project]\nname = "pluginreg"\nversion = "2.1.0"\n'

FILES["pluginreg/__init__.py"] = ""

FILES["pluginreg/hooks.py"] = '''\
"""Lifecycle hook bus shared by the registry and activated plugins."""

from __future__ import annotations


class HookBus:
    """Records which subscriber observed each dispatched event, in order."""

    def __init__(self) -> None:
        self._subs: dict[str, list[str]] = {}
        self._history: list[tuple[str, str | None]] = []

    def attach(self, event: str, subscriber: str) -> None:
        self._subs.setdefault(event, []).append(subscriber)

    def subscribers(self, event: str) -> tuple[str, ...]:
        return tuple(self._subs.get(event, ()))

    def dispatch(self, event: str, payload: str | None = None) -> tuple[str, ...]:
        notified = tuple(self._subs.get(event, ()))
        self._history.append((event, payload))
        return notified

    def history(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(self._history)
'''

# BUG 1: register() demands that every dependency is ALREADY registered,
# so registration order matters and dependents cannot be loaded before
# their dependencies even though activation order is computed later.
# BUG 2: duplicate detection compares raw display names only, so plugins
# whose names differ just by case ("Cache" vs "cache") both register.
FILES["pluginreg/registry.py"] = '''\
"""Plugin registry with dependency-aware activation ordering."""

from __future__ import annotations

from dataclasses import dataclass

from pluginreg.hooks import HookBus


class PluginError(Exception):
    """Base class for registry failures."""


class DuplicatePluginError(PluginError):
    """Two plugins claim the same (normalized) name."""


class UnknownDependencyError(PluginError):
    """A dependency edge points at a plugin that was never registered."""


class DependencyCycleError(PluginError):
    """The dependency graph contains a cycle."""


@dataclass(frozen=True)
class PluginSpec:
    name: str
    version: str
    requires: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()


def normalize(name: str) -> str:
    return name.strip().casefold()


class PluginRegistry:
    """Holds plugin specs; activation order is derived from dependencies."""

    def __init__(self) -> None:
        self._specs: dict[str, PluginSpec] = {}
        self._by_norm: dict[str, str] = {}
        self._order: list[str] = []

    def register(self, spec: PluginSpec) -> None:
        if spec.name in self._specs:
            raise DuplicatePluginError(f"plugin already registered: {spec.name}")
        for dep in spec.requires:
            if normalize(dep) not in self._by_norm:
                raise UnknownDependencyError(
                    f"{spec.name} requires unregistered plugin: {dep}"
                )
        self._specs[spec.name] = spec
        self._by_norm[spec.name] = spec.name
        self._order.append(spec.name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._order)

    def get(self, name: str) -> PluginSpec:
        display = self._by_norm.get(normalize(name))
        if display is None:
            raise KeyError(f"unknown plugin: {name}")
        return self._specs[display]

    def resolve_activation_order(self) -> list[str]:
        deps: dict[str, list[str]] = {}
        for name in self._order:
            resolved: list[str] = []
            for dep in self._specs[name].requires:
                display = self._by_norm.get(normalize(dep))
                if display is None:
                    raise UnknownDependencyError(
                        f"{self._specs[name].name} requires unregistered plugin: {dep}"
                    )
                resolved.append(display)
            deps[name] = resolved
        pending = list(self._order)
        ordered: list[str] = []
        satisfied: set[str] = set()
        while pending:
            progressed = False
            for name in list(pending):
                if all(dep in satisfied for dep in deps[name]):
                    ordered.append(name)
                    satisfied.add(name)
                    pending.remove(name)
                    progressed = True
            if not progressed:
                raise DependencyCycleError(
                    f"cyclic plugin dependencies among: {sorted(pending)}"
                )
        return ordered

    def activate(self, bus: HookBus) -> list[str]:
        activated: list[str] = []
        for name in self.resolve_activation_order():
            spec = self.get(name)
            for hook in spec.hooks:
                bus.attach(hook, spec.name)
            bus.dispatch("plugin_activated", spec.name)
            activated.append(spec.name)
        return activated
'''

FILES["pluginreg/loader.py"] = '''\
"""Declarative manifest loading into a plugin registry."""

from __future__ import annotations

from pluginreg.hooks import HookBus
from pluginreg.registry import PluginRegistry, PluginSpec


def spec_from_descriptor(descriptor: dict) -> PluginSpec:
    return PluginSpec(
        name=str(descriptor["name"]),
        version=str(descriptor.get("version", "0.0.0")),
        requires=tuple(descriptor.get("requires", ())),
        hooks=tuple(descriptor.get("hooks", ())),
    )


def load_manifest(
    registry: PluginRegistry, descriptors: list[dict]
) -> list[str]:
    """Register every descriptor in manifest order; manifests may be scrambled."""
    registered: list[str] = []
    for descriptor in descriptors:
        spec = spec_from_descriptor(descriptor)
        registry.register(spec)
        registered.append(spec.name)
    return registered


def load_and_activate(
    descriptors: list[dict],
    registry: PluginRegistry | None = None,
    bus: HookBus | None = None,
) -> tuple[PluginRegistry, HookBus, list[str]]:
    registry = registry if registry is not None else PluginRegistry()
    bus = bus if bus is not None else HookBus()
    load_manifest(registry, descriptors)
    order = registry.activate(bus)
    return registry, bus, order
'''

FILES["tests/__init__.py"] = ""

FILES["tests/test_plugins.py"] = '''\
"""Public tests for plugin registration compatibility."""

import pytest

from pluginreg.hooks import HookBus
from pluginreg.loader import load_and_activate, spec_from_descriptor
from pluginreg.registry import (
    DependencyCycleError,
    DuplicatePluginError,
    PluginRegistry,
    UnknownDependencyError,
)

CACHE = {"name": "cache", "version": "1.4", "hooks": ["request"]}
AUTH = {"name": "auth", "version": "2.0", "requires": ["cache"],
        "hooks": ["request"]}
API = {"name": "api", "version": "0.9", "requires": ["auth", "cache"]}


def test_registration_order_does_not_matter():
    registry = PluginRegistry()
    for descriptor in [API, AUTH, CACHE]:  # dependents listed first
        registry.register(spec_from_descriptor(descriptor))
    assert registry.names() == ("api", "auth", "cache")
    assert registry.resolve_activation_order() == ["cache", "auth", "api"]


def test_activation_order_follows_dependencies_from_scrambled_manifest():
    _, _, order = load_and_activate([API, CACHE, AUTH])
    assert order == ["cache", "auth", "api"]

def test_duplicate_names_differing_only_in_case_are_rejected():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(CACHE))
    with pytest.raises(DuplicatePluginError):
        registry.register(spec_from_descriptor({"name": "Cache", "version": "9.9"}))
    assert registry.get("CACHE").version == "1.4"
    assert registry.names() == ("cache",)


def test_dependency_lookup_ignores_case_and_padding():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(CACHE))
    registry.register(
        spec_from_descriptor({"name": "auth", "version": "2.0", "requires": [" Cache "]})
    )
    assert registry.resolve_activation_order() == ["cache", "auth"]


def test_truly_missing_dependency_is_reported_at_resolution():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(
        {"name": "api", "version": "0.9", "requires": ["ghost"]}
    ))
    with pytest.raises(UnknownDependencyError):
        registry.resolve_activation_order()


def test_hooks_attach_in_dependency_order():
    _, bus, _ = load_and_activate([API, CACHE, AUTH])
    assert bus.subscribers("request") == ("cache", "auth")


def test_duplicate_error_names_the_offender():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(AUTH))
    with pytest.raises(DuplicatePluginError, match="auth"):
        registry.register(spec_from_descriptor({"name": "auth", "version": "3.0"}))
'''


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "pluginreg: dependency-aware plugin registry", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
