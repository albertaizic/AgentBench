"""Hidden behavioral checks for pluginreg (different plugin graphs)."""

from __future__ import annotations

import pytest

from pluginreg.hooks import HookBus
from pluginreg.loader import load_and_activate, spec_from_descriptor
from pluginreg.registry import (
    DependencyCycleError,
    DuplicatePluginError,
    PluginRegistry,
    UnknownDependencyError,
)

# Deep chain: exporter -> d-sink -> c-queue -> b-buffer -> a-source, with
# dependency references written in scrambled case/padding.
CHAIN = [
    {"name": "exporter", "version": "1.0", "requires": [" d-sink "]},
    {"name": "d-sink", "version": "1.1", "requires": ["C-QUEUE"]},
    {"name": "c-queue", "version": "1.2", "requires": ["B-BUFFER"]},
    {"name": "b-buffer", "version": "1.3", "requires": ["\ta-source"]},
    {"name": "a-source", "version": "1.4", "hooks": ["tick"]},
]

DIAMOND = [
    {"name": "top", "version": "3.0", "requires": ["left", "right"]},
    {"name": "left", "version": "1.0", "requires": ["base"]},
    {"name": "right", "version": "2.0", "requires": ["base"]},
    {"name": "base", "version": "0.1"},
]


def test_deep_chain_registers_scrambled_and_activates_in_dependency_order():
    _, _, order = load_and_activate(list(reversed(CHAIN)))
    assert order == ["a-source", "b-buffer", "c-queue", "d-sink", "exporter"]


def test_diamond_ties_break_by_registration_order():
    _, bus, order = load_and_activate(DIAMOND)
    assert order == ["base", "left", "right", "top"]
    assert bus.history() == (
        ("plugin_activated", "base"),
        ("plugin_activated", "left"),
        ("plugin_activated", "right"),
        ("plugin_activated", "top"),
    )


def test_cycle_detected_only_at_resolution_time():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(
        {"name": "alpha", "version": "1", "requires": ["beta"]}
    ))
    registry.register(spec_from_descriptor(
        {"name": "beta", "version": "1", "requires": ["ALPHA "]}
    ))
    with pytest.raises(DependencyCycleError):
        registry.resolve_activation_order()


def test_whitespace_case_duplicate_rejected():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor({"name": "metrics ", "version": "1.0"}))
    with pytest.raises(DuplicatePluginError):
        registry.register(spec_from_descriptor({"name": "\tMETRICS", "version": "2.0"}))
    assert len(registry.names()) == 1
    assert registry.get("metrics").version == "1.0"


def test_normalized_get_and_missing_lookup():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(CHAIN[1]))
    assert registry.get("D-SINK").version == "1.1"
    assert registry.get("  d-sink ").version == "1.1"
    with pytest.raises(KeyError):
        registry.get("missing")


def test_unknown_dependency_names_plugin_and_edge():
    registry = PluginRegistry()
    registry.register(spec_from_descriptor(
        {"name": "orphan", "version": "1", "requires": ["phantom"]}
    ))
    with pytest.raises(UnknownDependencyError, match="phantom") as excinfo:
        registry.resolve_activation_order()
    assert "orphan" in str(excinfo.value)


def test_registration_order_still_preserved_for_ties():
    # Independent roots: activation keeps registration order between them.
    _, _, order = load_and_activate(
        [
            {"name": "zeta", "version": "1"},
            {"name": "Alpha", "version": "1"},
            {"name": "Mid", "version": "1"},
        ]
    )
    assert order == ["zeta", "Alpha", "Mid"]


def test_hooks_attach_once_per_plugin():
    registry = PluginRegistry()
    bus = HookBus()
    registry.register(spec_from_descriptor(
        {"name": "timer", "version": "1", "hooks": ["tick", "tock"]}
    ))
    with pytest.raises(DuplicatePluginError):
        registry.register(spec_from_descriptor({"name": "Timer", "version": "9"}))
    registry.activate(bus)
    assert bus.subscribers("tick") == ("timer",)
    assert len(registry.resolve_activation_order()) == 1
