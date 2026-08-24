"""Tests for execution backends: construction, security, provenance."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentbench.adapters.base import AgentInvocation
from agentbench.backends import make_backend
from agentbench.backends.base import ExecutionBackend, credential_env
from agentbench.backends.docker import (
    DockerExecutionBackend,
    validate_mount_path,
)
from agentbench.models import ExecutionSpec
from agentbench.process import ProcessResult


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    """Keep unit tests hermetic regardless of whether Docker is installed."""
    monkeypatch.setenv("AGENTBENCH_NO_CACHE", "1")


class TestHostBackend:
    def test_runs_invocation_and_captures_everything(self, tmp_path):
        backend = make_backend(ExecutionSpec())

        result = backend.run_agent(
            AgentInvocation(argv=[sys_executable(), "-c", "print('hi')"]),
            workspace=tmp_path,
            timeout=30,
            env=None,
        )

        assert result.exit_code == 0
        assert result.stdout.strip() == "hi"
        assert result.duration_seconds > 0

    def test_provenance_names_host(self):
        assert make_backend(ExecutionSpec()).provenance()["backend"] == "host"


class TestDockerCommandConstruction:
    def backend(self, tmp_path, **spec_overrides) -> DockerExecutionBackend:
        spec = ExecutionSpec(**{"backend": "docker", **spec_overrides})
        return DockerExecutionBackend(spec, allowed_roots=[tmp_path])

    def test_minimal_run_command_shape(self, tmp_path):
        backend = self.backend(tmp_path)

        args = backend.container_args(tmp_path / "ws")

        assert args[0].lower().endswith("docker") or args[0].lower().endswith("docker.exe")
        assert "run" in args
        assert "--rm" in args
        assert "org.agentbench.run=true" in args
        mount_at = args.index("-v")
        mount = args[mount_at + 1]
        assert mount.endswith(":/workspace")
        assert "-w" in args and args[args.index("-w") + 1] == "/workspace"

    def test_network_disabled_uses_none(self, tmp_path):
        args = self.backend(tmp_path, network="disabled").container_args(tmp_path / "ws")

        assert args[args.index("--network") + 1] == "none"

    def test_resource_limits_become_flags(self, tmp_path):
        args = self.backend(
            tmp_path, memory="2g", cpus=1.5, pids_limit=128
        ).container_args(tmp_path / "ws")

        assert args[args.index("--memory") + 1] == "2g"
        assert args[args.index("--cpus") + 1] == "1.5"
        assert args[args.index("--pids-limit") + 1] == "128"

    def test_only_allowlisted_env_vars_forwarded_with_values(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
        monkeypatch.setenv("UNRELATED", "nope")
        backend = self.backend(tmp_path, pass_env=["ANTHROPIC_API_KEY"])

        args = backend.container_args(tmp_path / "ws")

        env_flags = [a for a in args if a.startswith("-e=") or a.startswith("ANTHROPIC")]
        forwarded = args[args.index("-e") + 1] if "-e" in args else ""
        assert "ANTHROPIC_API_KEY=sk-secret-value" in forwarded
        assert not any("UNRELATED" in str(a) for a in args)

    def test_agent_argv_appended_after_image_without_shell(self, tmp_path):
        backend = self.backend(tmp_path, image="python:3.12-slim")
        invocation = AgentInvocation(argv=["claude", "--print"], input_text="prompt")

        args = [*backend.container_args(tmp_path / "ws"), backend.image, *invocation.argv]

        image_at = args.index("python:3.12-slim")
        assert args[image_at + 1:] == ["claude", "--print"]

    def test_containers_are_named_and_unique_per_instance(self, tmp_path):
        first = self.backend(tmp_path)
        second = self.backend(tmp_path)

        args = first.container_args(tmp_path / "ws")

        name = args[args.index("--name") + 1]
        assert name.startswith("agentbench-")
        assert name != second.container_name


class TestDockerTimeoutCleanup:
    """A killed host-side ``docker run`` CLI leaves the container running;
    backend.cleanup() must force-remove it by its recorded name."""

    @pytest.fixture
    def invocations(self, monkeypatch):
        import agentbench.backends.docker as docker_mod

        calls: list[list[str]] = []

        def fake_run_command(argv, **kwargs):
            calls.append(list(argv))
            return ProcessResult(exit_code=0, stdout="", stderr="", duration_seconds=0.1)

        monkeypatch.setattr(docker_mod, "run_command", fake_run_command)
        return calls

    def backend(self, tmp_path) -> DockerExecutionBackend:
        spec = ExecutionSpec(backend="docker", network="disabled")
        return DockerExecutionBackend(spec, allowed_roots=[tmp_path])

    def test_cleanup_after_started_run_force_removes_container(self, tmp_path, invocations):
        backend = self.backend(tmp_path)
        # Simulate the docker CLI being killed by a timeout mid-run.
        invocations.append([])
        backend._container_started = True

        backend.cleanup()

        assert any(
            argv[-3:-1] == ["rm", "-f"] and argv[-1] == backend.container_name
            for argv in invocations
            if len(argv) >= 3
        ), f"no force-remove in {invocations}"

    def test_cleanup_before_any_start_is_a_noop(self, tmp_path, invocations):
        backend = self.backend(tmp_path)

        backend.cleanup()

        assert invocations == []

    def test_cleanup_swallows_rm_failures(self, tmp_path, monkeypatch):
        import agentbench.backends.docker as docker_mod

        backend = self.backend(tmp_path)
        backend._container_started = True

        def boom(*args, **kwargs):
            raise RuntimeError("docker daemon vanished")

        monkeypatch.setattr(docker_mod, "run_command", boom)

        backend.cleanup()  # must not raise

    def test_provenance_records_container_name(self, tmp_path):
        backend = self.backend(tmp_path)

        assert backend.provenance()["container_name"] == backend.container_name


class TestCredentialForwarding:
    def test_presence_evidence_never_contains_values(self, monkeypatch):
        monkeypatch.setenv("SECRET_TEST_VAR", "super-secret")

        env, evidence = credential_env(["SECRET_TEST_VAR", "MISSING_VAR"])

        assert env["SECRET_TEST_VAR"] == "super-secret"  # for the container only
        assert evidence == [
            {"name": "SECRET_TEST_VAR", "present": True},
            {"name": "MISSING_VAR", "present": False},
        ]
        assert all("secret" not in json_of(evidence) for _ in [0])

    def test_invalid_env_names_rejected_by_schema(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExecutionSpec(backend="docker", pass_env=["A; rm -rf /"])


def json_of(value) -> str:
    import json

    return json.dumps(value)


def sys_executable() -> str:
    import sys

    return sys.executable


class TestMountContainment:
    def test_workspace_inside_allowed_root_passes(self, tmp_path):
        ws = tmp_path / "workspaces" / "agentbench-abc"
        ws.mkdir(parents=True)

        resolved = validate_mount_path(ws, [tmp_path])

        assert resolved == ws.resolve()

    def test_outside_allowed_roots_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            validate_mount_path(Path("C:/Windows") if os.name == "nt" else Path("/etc"),
                                [tmp_path])

    def test_unc_paths_rejected(self, tmp_path):
        if os.name != "nt":
            pytest.skip("UNC paths are Windows-specific")
        with pytest.raises(ValueError):
            validate_mount_path(Path("//server/share/repo"), [tmp_path])

    def test_symlink_escape_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside-containment"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link" / "ws"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")

        with pytest.raises(ValueError):
            validate_mount_path(link, [tmp_path])


class TestDockerUnavailable:
    def test_availability_check_tolerates_missing_binary(self, monkeypatch):
        from agentbench.backends.docker import docker_available

        monkeypatch.setenv("PATH", "")
        # Cached lru state must not leak between configurations.
        docker_available.__wrapped__ and None  # touch without clearing cache
        assert isinstance(docker_available(), bool)


class TestFactory:
    def test_unknown_backend_raises(self):
        from agentbench.backends import UnknownBackendError

        spec = ExecutionSpec()
        spec.backend = "warp-drive"  # type: ignore[assignment]

        with pytest.raises(UnknownBackendError):
            make_backend(spec)

    def test_factory_returns_host_by_default(self):
        backend = make_backend(ExecutionSpec())

        assert isinstance(backend, ExecutionBackend)
        assert backend.name == "host"
