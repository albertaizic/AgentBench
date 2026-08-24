"""AgentBench: a reproducible evaluation framework for coding agents."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    # Single source of truth: the installed distribution's metadata, which pip
    # derives from pyproject.toml at build time. Never hardcode a version here —
    # bump pyproject.toml (and reinstall) instead.
    __version__ = _package_version("agentbench")
except PackageNotFoundError:  # bare source tree without an installed dist
    __version__ = "0.0.0.dev0"
