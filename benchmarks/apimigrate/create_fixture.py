"""Deterministic generator for the apimigrate fixture (API split migration)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

INIT_PY = '''"""Profile SDK client, facade, and internal callers."""

from .client import ProfileClient
from .facade import AccountFacade

__all__ = ["ProfileClient", "AccountFacade"]
'''

DIRECTORY_PY = '''"""Fake user directory backing the SDK (stand-in for the remote service)."""

from __future__ import annotations


def _entry(uid: str, name: str, email: str, theme: str, locale: str, newsletter: bool):
    return {
        "user": {"id": uid, "name": name, "email": email},
        "preferences": {"theme": theme, "locale": locale, "newsletter": newsletter},
    }


DIRECTORY = {
    "u-101": _entry("u-101", "Ada", "ada@example.invalid", "dark", "en-US", True),
    "u-102": _entry("u-102", "Ben", "ben@example.invalid", "light", "de-DE", False),
    "u-103": _entry("u-103", "Cleo", "cleo@example.invalid", "contrast", "fr-FR", True),
    "u-204": _entry("u-204", "Dara", "dara@example.invalid", "dark", "es-MX", False),
    "u-205": _entry("u-205", "Emil", "emil@example.invalid", "sepia", "pt-BR", True),
    "u-206": _entry("u-206", "Fay", "fay@example.invalid", "light", "ja-JP", False),
}


def lookup(user_id: str) -> dict:
    try:
        return DIRECTORY[user_id]
    except KeyError:
        raise KeyError(f"unknown user: {user_id}") from None
'''

CLIENT_PY = '''"""Public SDK client.

The combined ``fetch_user_profile`` view has been superseded by the finer
grained ``fetch_user`` / ``fetch_preferences`` pair; the old method remains
as a deprecated shim for external integrations.
"""

from __future__ import annotations

import sys
import warnings

from .directory import DIRECTORY, lookup


class ProfileClient:
    def __init__(self, backend: dict | None = None) -> None:
        self._backend = backend if backend is not None else DIRECTORY
        # BUG: deprecation bookkeeping is missing entirely - see
        # fetch_user_profile below.
        pass

    def fetch_user(self, user_id: str) -> dict:
        entry = self._backend.get(user_id) or lookup(user_id)
        return dict(entry["user"])

    def fetch_preferences(self, user_id: str) -> dict:
        entry = self._backend.get(user_id) or lookup(user_id)
        return dict(entry["preferences"])

    def fetch_user_profile(self, user_id: str) -> dict:
        """Deprecated: combined profile view. Kept for external integrations."""
        # BUG: this shim warns on EVERY call instead of at most once per
        # calling module per client instance.
        warnings.warn(
            "fetch_user_profile() is deprecated; use fetch_user() and "
            "fetch_preferences() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        user = self.fetch_user(user_id)
        preferences = self.fetch_preferences(user_id)
        return {**user, "preferences": preferences}
'''

FACADE_PY = '''"""External-facing facade over the profile SDK."""

from __future__ import annotations

from .client import ProfileClient


class AccountFacade:
    def __init__(self, client: ProfileClient | None = None) -> None:
        self._client = client if client is not None else ProfileClient()

    def account_overview(self, user_id: str) -> dict:
        # BUG: still funnels through the deprecated combined method instead
        # of composing the split API.
        return self._client.fetch_user_profile(user_id)
'''

CALLER_A_PY = '''"""Notification module - builds digest footers from profile data."""

from __future__ import annotations

from .client import ProfileClient


def digest_footer(client: ProfileClient, user_id: str) -> str:
    # BUG: still on the deprecated combined path.
    profile = client.fetch_user_profile(user_id)
    preferences = profile["preferences"]
    return f"-- {profile['name']} <{profile['email']}> ({preferences['locale']})"
'''

CALLER_B_PY = '''"""Billing module - labels invoices with profile attributes."""

from __future__ import annotations

from .client import ProfileClient


def invoice_label(client: ProfileClient, user_id: str) -> dict:
    # BUG: still on the deprecated combined path.
    profile = client.fetch_user_profile(user_id)
    return {
        "bill_to": profile["email"],
        "plan_theme": profile["preferences"]["theme"],
    }
'''

CALLER_C_PY = '''"""Reporting module - exports flat rows into the warehouse."""

from __future__ import annotations

from .client import ProfileClient


def export_row(client: ProfileClient, user_id: str) -> list:
    # BUG: still on the deprecated combined path.
    profile = client.fetch_user_profile(user_id)
    return [profile["id"], profile["name"], profile["preferences"]["newsletter"]]
'''

TEST_API_SURFACE_PY = '''"""Public tests for the split-API migration state."""

from __future__ import annotations

import warnings

from apimigrate.caller_a import digest_footer
from apimigrate.caller_b import invoice_label
from apimigrate.caller_c import export_row
from apimigrate.client import ProfileClient
from apimigrate.facade import AccountFacade


def test_facade_composes_overview_without_deprecated_path():
    facade = AccountFacade()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        overview = facade.account_overview("u-101")
    assert overview == {
        "id": "u-101",
        "name": "Ada",
        "email": "ada@example.invalid",
        "preferences": {"theme": "dark", "locale": "en-US", "newsletter": True},
    }


def test_internal_callers_are_off_the_deprecated_path():
    client = ProfileClient()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        footer = digest_footer(client, "u-102")
        label = invoice_label(client, "u-102")
        row = export_row(client, "u-103")
    assert footer == "-- Ben <ben@example.invalid> (de-DE)"
    assert label == {"bill_to": "ben@example.invalid", "plan_theme": "light"}
    assert row == ["u-103", "Cleo", True]


def test_shim_warns_exactly_once_per_call_site_module():
    client = ProfileClient()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.fetch_user_profile("u-101")
        client.fetch_user_profile("u-101")
        client.fetch_user_profile("u-102")
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1


def test_fresh_client_instance_warns_again():
    warmed = ProfileClient()
    with warnings.catch_warnings(record=True) as first_round:
        warnings.simplefilter("always")
        warmed.fetch_user_profile("u-103")
    second = ProfileClient()
    with warnings.catch_warnings(record=True) as second_round:
        warnings.simplefilter("always")
        second.fetch_user_profile("u-103")
    assert len([w for w in first_round if issubclass(w.category, DeprecationWarning)]) == 1
    assert len([w for w in second_round if issubclass(w.category, DeprecationWarning)]) == 1


def test_shim_still_returns_combined_payload():
    client = ProfileClient()
    profile = client.fetch_user_profile("u-102")
    assert profile["id"] == "u-102"
    assert profile["preferences"] == {"theme": "light", "locale": "de-DE", "newsletter": False}
'''

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "apimigrate"\nversion = "1.1.0"\n',
    "apimigrate/__init__.py": INIT_PY,
    "apimigrate/directory.py": DIRECTORY_PY,
    "apimigrate/client.py": CLIENT_PY,
    "apimigrate/facade.py": FACADE_PY,
    "apimigrate/caller_a.py": CALLER_A_PY,
    "apimigrate/caller_b.py": CALLER_B_PY,
    "apimigrate/caller_c.py": CALLER_C_PY,
    "tests/test_api_surface.py": TEST_API_SURFACE_PY,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "apimigrate: profile SDK mid-migration", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
