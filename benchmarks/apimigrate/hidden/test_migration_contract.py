"""Hidden behavioral checks for the apimigrate migration contract.

Uses different user records than the public tests (u-204..u-206).
"""

from __future__ import annotations

import warnings

from apimigrate.caller_a import digest_footer
from apimigrate.caller_b import invoice_label
from apimigrate.caller_c import export_row
from apimigrate.client import ProfileClient
from apimigrate.directory import DIRECTORY
from apimigrate.facade import AccountFacade


def run(client, fn, *args):
    """Run fn catching warnings; return (value, deprecation_list)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = fn(client, *args)
    deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    return value, deps


def test_migrated_callers_emit_no_warnings():
    for fn in (digest_footer, invoice_label, export_row):
        _value, deps = run(ProfileClient(), fn, "u-204")
        assert deps == [], f"{fn.__name__} still uses the deprecated path"


def test_caller_outputs_track_backend_data():
    client = ProfileClient()
    footer, deps = run(client, digest_footer, "u-205")
    entry = DIRECTORY["u-205"]
    assert deps == []
    assert footer == f"-- {entry['user']['name']} <{entry['user']['email']}> ({entry['preferences']['locale']})"
    label, deps = run(client, invoice_label, "u-206")
    assert deps == []
    assert label == {"bill_to": DIRECTORY["u-206"]["user"]["email"], "plan_theme": DIRECTORY["u-206"]["preferences"]["theme"]}
    row, deps = run(client, export_row, "u-204")
    assert deps == []
    assert row == ["u-204", "Dara", False]


def test_shim_warns_once_per_module_across_repeated_calls():
    client = ProfileClient()
    _, first = run(client, lambda c: c.fetch_user_profile("u-204"))
    _, second = run(client, lambda c: c.fetch_user_profile("u-205"))
    _, third = run(client, lambda c: c.fetch_user_profile("u-206"))
    assert len(first) == 1
    assert all(issubclass(w.category, DeprecationWarning) for w in first)
    assert second == []
    assert third == []


def test_fresh_client_instance_warns_again_for_same_module():
    warmed = ProfileClient()
    run(warmed, lambda c: c.fetch_user_profile("u-204"))
    fresh = ProfileClient()
    _, again = run(fresh, lambda c: c.fetch_user_profile("u-204"))
    assert len(again) == 1


def test_facade_passthroughs_expose_split_api():
    facade = AccountFacade()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        user = facade.fetch_user("u-205")
        preferences = facade.fetch_preferences("u-205")
        overview = facade.account_overview("u-206")
    assert user == DIRECTORY["u-205"]["user"]
    assert preferences == DIRECTORY["u-205"]["preferences"]
    expected = {**DIRECTORY["u-206"]["user"], "preferences": DIRECTORY["u-206"]["preferences"]}
    assert overview == expected


def test_shim_payload_equals_manual_composition():
    client = ProfileClient()
    shimmed, deps = run(client, lambda c: c.fetch_user_profile("u-206"))
    manual = {
        **client.fetch_user("u-206"),
        "preferences": client.fetch_preferences("u-206"),
    }
    assert deps  # exactly one warning from the single shim call
    assert len(deps) == 1
    assert shimmed == manual


def test_unknown_user_raises_key_error_everywhere():
    client = ProfileClient()
    for call in (
        lambda: client.fetch_user("ghost"),
        lambda: client.fetch_preferences("ghost"),
    ):
        try:
            call()
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")
