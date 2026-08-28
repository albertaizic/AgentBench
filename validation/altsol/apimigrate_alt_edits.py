"""Alternative apimigrate solution: each caller composes the split API
through a tiny module-local helper; deprecation dedupe keyed by caller file."""

def edits(files):
    out = dict(files)
    client = files["apimigrate/client.py"]
    client = client.replace(
        """    def __init__(self, backend: dict | None = None) -> None:
        self._backend = backend if backend is not None else DIRECTORY
        # BUG: deprecation bookkeeping is missing entirely - see
        # fetch_user_profile below.
        pass""",
        """    def __init__(self, backend: dict | None = None) -> None:
        self._backend = backend if backend is not None else DIRECTORY
        self._warned_callers: set[str] = set()""")
    client = client.replace(
        """        # BUG: this shim warns on EVERY call instead of at most once per
        # calling module per client instance.
        warnings.warn(
            "fetch_user_profile() is deprecated; use fetch_user() and "
            "fetch_preferences() instead",
            DeprecationWarning,
            stacklevel=2,
        )""",
        """        frame = sys._getframe(1)
        caller = frame.f_code.co_filename
        if caller not in self._warned_callers:
            self._warned_callers.add(caller)
            warnings.warn(
                "fetch_user_profile() is deprecated; use fetch_user() and "
                "fetch_preferences() instead",
                DeprecationWarning,
                stacklevel=2,
            )""")
    out["apimigrate/client.py"] = client

    def split_pair(client_expr: str, uid: str):
        return (f"    user = {client_expr}.fetch_user({uid})\n"
                f"    preferences = {client_expr}.fetch_preferences({uid})\n")

    out["apimigrate/caller_a.py"] = """\"\"\"Caller A: reporting helpers.\"\"\"

from __future__ import annotations

from .client import ProfileClient


def digest_footer(client: ProfileClient, user_id: str) -> str:
""" + split_pair("client", "user_id") + """    return f"-- {user['name']} <{user['email']}> ({preferences['locale']})"
"""
    out["apimigrate/caller_b.py"] = """\"\"\"Caller B: invoicing views.\"\"\"

from __future__ import annotations

from .client import ProfileClient


def invoice_label(client: ProfileClient, user_id: str) -> dict:
""" + split_pair("client", "user_id") + """    return {"bill_to": user["email"], "plan_theme": preferences["theme"]}
"""
    out["apimigrate/caller_c.py"] = """\"\"\"Caller C: export rows.\"\"\"

from __future__ import annotations

from .client import ProfileClient


def export_row(client: ProfileClient, user_id: str) -> list:
""" + split_pair("client", "user_id") + """    return [user["id"], user["name"], preferences["newsletter"]]
"""
    facade = files["apimigrate/facade.py"]
    facade = facade.replace(
        """    def account_overview(self, user_id: str) -> dict:
        # BUG: still funnels through the deprecated combined method instead
        # of composing the split API.
        return self._client.fetch_user_profile(user_id)""",
        """    def fetch_user(self, user_id: str) -> dict:
        return self._client.fetch_user(user_id)

    def fetch_preferences(self, user_id: str) -> dict:
        return self._client.fetch_preferences(user_id)

    def account_overview(self, user_id: str) -> dict:
        overview = self.fetch_user(user_id)
        overview["preferences"] = self.fetch_preferences(user_id)
        return overview""")
    out["apimigrate/facade.py"] = facade
    return out
