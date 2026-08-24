"""Host execution backend: local subprocesses with an explicit env policy.

Containment honesty: a host run is NOT a sandbox. The agent executes with
the invoking user's OS permissions and (under the default ``inherit``
policy) the parent environment. What this backend adds on top of plain
``subprocess``:

* ``cwd`` is always the throwaway workspace, never AgentBench's own tree;
* process-tree termination is guaranteed on timeout (:mod:`agentbench.process`);
* under ``env_policy="restricted"`` the child starts from a minimal OS base
  plus the ``pass_env`` allowlist, and HOME/USERPROFILE point at a per-run
  temp directory that is deleted afterwards — generic agents cannot read the
  user's dotfiles, and any credentials they see are exactly the names the
  benchmark author allowlisted.

Network policy is recorded but not enforceable for host processes; use the
Docker backend when network containment matters.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from agentbench.adapters.base import AgentInvocation
from agentbench.backends.base import ExecutionBackend, credential_env
from agentbench.models import ExecutionSpec
from agentbench.process import ProcessResult, run_command, run_shell_command

# Variables every child reasonably needs to boot the OS toolchain. Nothing
# user-identifying, nothing credential-bearing.
_BASE_ENV_NAMES_WINDOWS = (
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)
_BASE_ENV_NAMES_POSIX = (
    "PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL",
)


def restricted_base_env() -> dict[str, str]:
    """The minimal environment children get under ``env_policy=restricted``."""
    names = _BASE_ENV_NAMES_WINDOWS if os.name == "nt" else _BASE_ENV_NAMES_POSIX
    return {name: value for name in names if (value := os.environ.get(name)) is not None}


@contextmanager
def disposable_home():
    """A per-run temp HOME/USERPROFILE, removed afterwards."""
    home = Path(tempfile.mkdtemp(prefix="agentbench-home-"))
    try:
        yield home
    finally:
        from agentbench.workspace import remove_tree

        remove_tree(home)


class HostExecutionBackend(ExecutionBackend):
    name = "host"

    # -- environment ----------------------------------------------------------

    def child_environment(self) -> tuple[dict[str, str] | None, bool]:
        """(env, restricted) for child processes.

        ``None`` means "inherit everything" — the historical behavior, kept as
        the default because real coding agents authenticate from ambient
        state. ``restricted`` returns the explicit minimal dict.
        """
        if self.config.env_policy != "restricted":
            return None, False
        env = restricted_base_env()
        forwarded, _evidence = credential_env(self.config.pass_env)
        env.update(forwarded)
        return env, True

    def run_agent(
        self,
        invocation: AgentInvocation,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        base_env, restricted = self.child_environment()
        if not restricted:
            return run_command(
                invocation.argv, cwd=workspace, timeout=timeout,
                input_text=invocation.input_text,
            )
        with disposable_home() as home:
            child_env = {**base_env, "HOME": str(home)}
            if os.name == "nt":
                child_env["USERPROFILE"] = str(home)
                child_env["HOMEDRIVE"], _, child_env["HOMEPATH"] = str(home)[:2], "", str(home)[2:]
            return run_command(
                invocation.argv, cwd=workspace, timeout=timeout,
                input_text=invocation.input_text, env=child_env,
            )

    def run_public_evaluation(
        self,
        command: str,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        base_env, restricted = self.child_environment()
        if not restricted:
            return run_shell_command(command, cwd=workspace, timeout=timeout, env=env)
        with disposable_home() as home:
            child_env = {**base_env, "HOME": str(home)}
            if os.name == "nt":
                child_env["USERPROFILE"] = str(home)
            return run_shell_command(command, cwd=workspace, timeout=timeout, env=child_env)

    def provenance(self) -> dict:
        provenance = {
            "backend": "host",
            "network": self.config.network,
            "env_policy": self.config.env_policy,
        }
        if self.config.env_policy == "restricted":
            # Presence evidence only — never values.
            provenance["passed_env_names"] = [
                name for name in self.config.pass_env if os.environ.get(name) is not None
            ]
        return provenance
