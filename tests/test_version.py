"""Version source of truth: pyproject.toml -> package metadata -> everything else.

Regression guard: v0.4.0 shipped while ``agentbench.__version__`` still said
"0.3.0", so doctor and new-run environment metadata kept reporting the previous
release. The version must never be hardcoded in a module again; these tests
compare every consumer against the single authority (installed distribution
metadata, which pip derives from pyproject.toml).
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agentbench
from agentbench.cli import app

runner = CliRunner()

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def installed_version() -> str | None:
    """The version pip recorded for agentbench, or None in a bare source tree."""
    try:
        return importlib.metadata.version("agentbench")
    except importlib.metadata.PackageNotFoundError:
        return None


def test_dunder_version_comes_from_package_metadata():
    # Holds for editable installs too: metadata is the source, not a constant.
    installed = installed_version()
    if installed is None:
        pytest.skip("agentbench distribution not installed in this environment")
    assert agentbench.__version__ == installed


def test_pyproject_version_agrees_with_the_reported_version():
    # Guards the exact regression: pyproject bumped while another copy of the
    # version lagged behind. Also fails on a stale editable install.
    if not PYPROJECT.exists():
        pytest.skip("not running from a source checkout")
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert agentbench.__version__ == declared
    installed = installed_version()
    if installed is not None:
        assert installed == declared


def test_run_environment_records_the_reported_version():
    from agentbench.envmeta import capture_environment

    env = capture_environment(agent_cli_version=None)
    assert env["agentbench_version"] == agentbench.__version__


def test_doctor_reports_the_reported_version(tmp_path):
    result = runner.invoke(app, ["doctor", "--results-dir", str(tmp_path / "results")])

    assert result.exit_code == 0, result.output
    assert "AgentBench version" in result.output
    assert agentbench.__version__ in result.output
