"""Execution backend registry and factory."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agentbench.backends.base import CONTAINER_WORKSPACE, ExecutionBackend, credential_env
from agentbench.backends.docker import (
    DEFAULT_IMAGE,
    RUN_LABEL,
    DockerExecutionBackend,
    docker_available,
    docker_version,
    validate_mount_path,
)
from agentbench.backends.host import HostExecutionBackend
from agentbench.models import ExecutionSpec


class UnknownBackendError(KeyError):
    pass


def make_backend(
    config: ExecutionSpec,
    *,
    workspace_parent: Path | None = None,
) -> ExecutionBackend:
    """Instantiate the configured backend.

    ``workspace_parent`` narrows where Docker mounts may point: only paths
    inside the runner's own workspace parent (plus the system temp root) are
    ever mountable.
    """
    if config.backend == "host":
        return HostExecutionBackend(config)
    if config.backend == "docker":
        roots = [Path(tempfile.gettempdir())]
        if workspace_parent is not None:
            roots.append(Path(workspace_parent))
        return DockerExecutionBackend(config, allowed_roots=roots)
    raise UnknownBackendError(config.backend)


__all__ = [
    "CONTAINER_WORKSPACE",
    "DEFAULT_IMAGE",
    "DockerExecutionBackend",
    "ExecutionBackend",
    "HostExecutionBackend",
    "RUN_LABEL",
    "UnknownBackendError",
    "credential_env",
    "docker_available",
    "docker_version",
    "make_backend",
    "validate_mount_path",
]
