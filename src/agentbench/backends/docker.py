"""Docker execution backend.

Threat/reproducibility model (deliberate, not "subprocess through docker"):

* the benchmark workspace is the ONLY host path mounted, read-write;
* AgentBench source, hidden evaluator source, results storage, the host home
  directory, and the Docker socket are never mounted;
* container environment starts EMPTY — only variables explicitly allowlisted
  through ``ExecutionSpec.pass_env`` are forwarded (values read from the
  host at run time, presence-only evidence recorded);
* network policy is explicit: "enabled" (default bridge) or "disabled"
  (``--network none``). Agents needing model APIs require "enabled";
* resource limits map to Docker flags (--memory/--cpus/--pids-limit) and are
  recorded; Docker's limits are resource controls, not hard security;
* agent runs use ``--rm`` containers labeled ``org.agentbench.run=true`` so
  stale resources remain identifiable and cleanable;
* image identity: the requested tag plus the resolved image ID/digest from
  ``docker image inspect`` after a successful run — mutable tags alone are
  not reproducibility evidence.

Git diff capture, workspace cloning, hidden evaluations, and persistence
always run host-side. Public evaluations execute inside the same container
image as the agent (via stdin-fed ``sh -s``, so no host shell quoting ever
reaches the container).

Docker Desktop is required on Windows/macOS; bind-mount paths are normalized
to POSIX form for the docker CLI.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

from agentbench.adapters.base import AgentInvocation
from agentbench.backends.base import CONTAINER_WORKSPACE, ExecutionBackend
from agentbench.models import ExecutionSpec
from agentbench.process import ProcessResult, resolve_executable, run_command

DEFAULT_IMAGE = "python:3.12-slim"
RUN_LABEL = ("label", "org.agentbench.run=true")
_DOCKER_TIMEOUT = 60.0

# The docker CLI reserves these exit codes for infrastructure problems
# (125 daemon/CLI error, 126 not executable, 127 not found); a container's
# own command propagates its real exit code unchanged.
_INFRASTRUCTURE_EXIT_CODES = {125, 126, 127}
_INFRASTRUCTURE_MARKERS = (
    "unable to find image",
    "pull access denied",
    "manifest unknown",
    "no such image",
    "error response from daemon",
    "cannot connect to the docker daemon",
)


def is_infrastructure_failure(result: ProcessResult) -> bool:
    """Whether *result* indicates Docker/setup failure rather than agent failure.

    A missing image, unreachable daemon, or unstartable container is an
    AgentBench setup problem and must never be scored as ``agent_failed``
    — the agent process never meaningfully ran.
    """
    if result.exit_code in _INFRASTRUCTURE_EXIT_CODES:
        return True
    blob = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in blob for marker in _INFRASTRUCTURE_MARKERS)


def _docker_binary() -> str:
    return resolve_executable("docker")


@lru_cache(maxsize=1)
def docker_available() -> bool:
    """True when a docker CLI exists and its daemon responds."""
    try:
        result = run_command(
            [_docker_binary(), "version", "--format", "{{.Server.Version}}"],
            cwd=Path.cwd(),
            timeout=_DOCKER_TIMEOUT,
        )
    except (OSError, RuntimeError):
        return False
    return result.exit_code == 0 and bool(result.stdout.strip())


@lru_cache(maxsize=1)
def docker_version() -> str | None:
    try:
        result = run_command(
            [_docker_binary(), "--version"], cwd=Path.cwd(), timeout=_DOCKER_TIMEOUT
        )
    except (OSError, RuntimeError):
        return None
    return (result.stdout or "").strip() or None


def validate_mount_path(workspace: Path, allowed_roots: list[Path]) -> Path:
    """Ensure *workspace* may be mounted: absolute, real, inside an allowed root.

    Treats benchmark-supplied paths as hostile: '..' games, Windows drive
    tricks, UNC paths, and symlink escapes all fail because containment is
    checked against fully resolved paths.
    """
    resolved = Path(workspace).resolve()
    if not resolved.is_absolute():
        raise ValueError(f"workspace must be absolute after resolution: {workspace}")
    if re.match(r"^\\\\", str(resolved)) or str(resolved).startswith("//"):
        raise ValueError(f"UNC paths cannot be mounted: {resolved}")
    for root in allowed_roots:
        root_resolved = Path(root).resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise ValueError(
        f"workspace {resolved} is outside every approved mount root "
        f"{[str(Path(r).resolve()) for r in allowed_roots]}"
    )


class DockerExecutionBackend(ExecutionBackend):
    name = "docker"

    def __init__(self, config: ExecutionSpec, *, allowed_roots: list[Path] | None = None) -> None:
        super().__init__(config)
        self._allowed_roots = allowed_roots or [Path(tempfile.gettempdir()).resolve()]
        self.image = config.image or DEFAULT_IMAGE
        self._image_id: str | None = None
        self._image_digests: list[str] | None = None
        # One backend instance per run: a unique, predictable container name is
        # what makes timeout cleanup possible — killing the host-side docker
        # CLI does NOT stop the container it started.
        self.container_name = f"agentbench-{uuid.uuid4().hex[:12]}"
        self._container_started = False

    # -- command construction -------------------------------------------------

    def container_args(self, workspace: Path, *, interactive: bool = False) -> list[str]:
        """The shared ``docker run`` prefix. Exposed for tests."""
        resolved = validate_mount_path(workspace, self._allowed_roots)
        args = [
            _docker_binary(),
            "run",
            "--rm",
            "--name",
            self.container_name,
            f"--{RUN_LABEL[0]}",
            RUN_LABEL[1],
            "-v",
            f"{resolved.as_posix()}:{CONTAINER_WORKSPACE}",
            "-w",
            CONTAINER_WORKSPACE,
        ]
        if interactive:
            args.append("-i")
        args += ["--network", "none" if self.config.network == "disabled" else "bridge"]
        if self.config.memory:
            args += ["--memory", self.config.memory]
        if self.config.cpus:
            args += ["--cpus", str(self.config.cpus)]
        if self.config.pids_limit:
            args += ["--pids-limit", str(self.config.pids_limit)]
        for name in self.config.pass_env:
            import os

            value = os.environ.get(name)
            if value is not None:
                args += ["-e", f"{name}={value}"]
        return args

    # -- execution ------------------------------------------------------------

    def run_agent(
        self,
        invocation: AgentInvocation,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        argv = [*self.container_args(workspace, interactive=invocation.input_text is not None),
                self.image, *invocation.argv]
        self._container_started = True
        result = run_command(argv, cwd=Path.cwd(), timeout=timeout, input_text=invocation.input_text)
        self._record_image_identity()
        return result

    def run_public_evaluation(
        self,
        command: str,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        # Feed the shell script via stdin ('sh -s'): evaluation commands are
        # never passed through a HOST shell and never embedded in argv, so no
        # quoting boundary exists between AgentBench and the container.
        argv = [*self.container_args(workspace, interactive=True), self.image, "sh", "-s"]
        self._container_started = True
        result = run_command(
            argv, cwd=Path.cwd(), timeout=timeout, input_text=command,
        )
        self._record_image_identity()
        return result

    def placeholders(self, *, workspace: Path, hidden_dir: Path | None = None) -> dict[str, str]:
        # {hidden_dir} deliberately resolves to empty inside containers:
        # hidden evaluator source must never be reachable from the container,
        # so a public evaluation referencing it is an authoring error.
        return {
            "python": "python",
            "workspace": CONTAINER_WORKSPACE,
            "hidden_dir": "",
        }

    # -- provenance -----------------------------------------------------------

    def _inspect_image(self) -> tuple[str | None, list[str]]:
        result = run_command(
            [_docker_binary(), "image", "inspect", "--format",
             "{{.Id}}\t{{json .RepoDigests}}", self.image],
            cwd=Path.cwd(),
            timeout=_DOCKER_TIMEOUT,
        )
        if result.exit_code != 0 or not result.stdout.strip():
            return None, []
        first_line = result.stdout.strip().splitlines()[0]
        image_id, _, digests_json = first_line.partition("\t")
        try:
            digests = json.loads(digests_json) if digests_json.strip() else []
        except ValueError:
            digests = []
        return (image_id or None), [str(d) for d in digests]

    def _record_image_identity(self) -> None:
        if self._image_id is None:
            self._image_id, self._image_digests = self._inspect_image()

    def provenance(self) -> dict:
        return {
            "backend": "docker",
            "docker_version": docker_version(),
            "image_requested": self.image,
            "image_id": self._image_id,
            "image_digests": self._image_digests or [],
            "network": self.config.network,
            "memory_limit": self.config.memory,
            "cpus_limit": self.config.cpus,
            "pids_limit": self.config.pids_limit,
            # Presence evidence only — never values.
            "passed_env_names": [
                name for name in self.config.pass_env if os.environ.get(name) is not None
            ],
            "container_workspace": CONTAINER_WORKSPACE,
            "container_name": self.container_name,
        }

    def cleanup(self) -> None:
        """Force-remove this run's container if it outlived the docker CLI.

        Killing the host-side ``docker run`` process (timeout, interrupt) does
        not stop the container it started; without this, a timed-out agent
        keeps running as an orphan holding the workspace bind mount. ``--rm``
        containers are already gone on the happy path, so a missing-name
        error here is normal and ignored.
        """
        if not self._container_started:
            return
        self._container_started = False  # one attempt per started container
        try:
            run_command(
                [_docker_binary(), "rm", "-f", self.container_name],
                cwd=Path.cwd(),
                timeout=_DOCKER_TIMEOUT,
            )
        except (OSError, RuntimeError):
            pass  # best effort: cleanup must never mask the run's own outcome
