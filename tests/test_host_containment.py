"""Host-backend environment policy: restricted env, disposable HOME."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentbench.adapters.base import AgentInvocation
from agentbench.backends.host import HostExecutionBackend, restricted_base_env
from agentbench.models import ExecutionSpec


def backend(**spec_overrides) -> HostExecutionBackend:
    return HostExecutionBackend(ExecutionSpec(backend="host", **spec_overrides))


# Prints "k=v" pairs plus a live HOME existence verdict computed INSIDE the child.
PRINT_ENV = (
    "import os\n"
    "print('\\n'.join(f'{k}={v}' for k, v in sorted(os.environ.items())))\n"
    "home = os.environ.get('HOME') or os.environ.get('USERPROFILE') or ''\n"
    "print('HOME_ALIVE=' + ('yes' if home and os.path.isdir(home) else 'no'))\n"
)


def run_python_argv(code: str) -> AgentInvocation:
    return AgentInvocation(argv=[sys.executable, "-c", code])


class TestRestrictedPolicy:
    def test_default_policy_inherits_parent_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTBENCH_MARKER_INHERIT", "1")
        result = backend().run_agent(
            run_python_argv(PRINT_ENV), workspace=tmp_path, timeout=30, env=None
        )
        assert "AGENTBENCH_MARKER_INHERIT=" in result.stdout

    def test_restricted_policy_drops_unlisted_variables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_MARKER_XYZ", "1")
        spec_backend = backend(env_policy="restricted")

        result = spec_backend.run_agent(
            run_python_argv(PRINT_ENV), workspace=tmp_path, timeout=30, env=None
        )

        assert "SECRET_MARKER_XYZ=" not in result.stdout
        assert "PATH=" in result.stdout  # OS base survives

    def test_restricted_forwards_allowlisted_names_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALLOWED_VAR_ABC", "value")
        monkeypatch.setenv("DENIED_VAR_QRS", "value")
        spec_backend = backend(env_policy="restricted", pass_env=["ALLOWED_VAR_ABC"])

        result = spec_backend.run_agent(
            run_python_argv(PRINT_ENV), workspace=tmp_path, timeout=30, env=None
        )

        assert "ALLOWED_VAR_ABC=value" in result.stdout
        assert "DENIED_VAR_QRS" not in result.stdout

    def test_restricted_home_exists_during_the_run(self, tmp_path):
        result = backend(env_policy="restricted").run_agent(
            run_python_argv(PRINT_ENV), workspace=tmp_path, timeout=30, env=None
        )

        assert "HOME_ALIVE=yes" in result.stdout

    def test_disposable_home_removed_after_run(self, tmp_path):
        from agentbench.backends.host import disposable_home

        seen: list[Path] = []
        with disposable_home() as home:
            assert home.exists()
            seen.append(home)
        assert not seen[0].exists()

    def test_provenance_records_policy_and_never_values(self, monkeypatch):
        monkeypatch.setenv("SOME_TOKEN_NAME", "super-secret")
        provenance = backend(
            env_policy="restricted", pass_env=["SOME_TOKEN_NAME"]
        ).provenance()

        assert provenance["env_policy"] == "restricted"
        assert provenance["passed_env_names"] == ["SOME_TOKEN_NAME"]
        assert all("secret" not in str(v).lower() for v in provenance.values())

    def test_base_env_has_no_credentials(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        assert "ANTHROPIC_API_KEY" not in restricted_base_env()


class TestExecutionSpecPolicy:
    def test_merged_with_carries_restricted_policy(self):
        base = ExecutionSpec(backend="host")
        override = ExecutionSpec(backend="host", env_policy="restricted")

        merged = base.merged_with(override)

        assert merged.env_policy == "restricted"
        # And an inherit override does NOT downgrade a restricted base.
        assert (
            ExecutionSpec(backend="host", env_policy="restricted")
            .merged_with(ExecutionSpec(backend="host")).env_policy
            == "restricted"
        )
