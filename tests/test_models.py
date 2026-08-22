"""Tests for benchmark specification models (agentbench.models)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentbench.models import AgentSpec, Evaluation, BenchmarkSpec


def spec_data(**overrides) -> dict:
    """A minimal valid benchmark spec as raw parsed YAML data."""
    data = {
        "name": "demo",
        "repository": "https://example.com/repo.git",
        "commit": "a" * 40,
        "prompt": "Fix the failing test in src/app.py.",
        "agent": {"type": "claude-code"},
        "evaluations": [{"name": "smoke", "command": 'python -c "print(1)"'}],
    }
    data.update(overrides)
    return data


class TestBenchmarkSpecDefaults:
    def test_minimal_spec_gets_sane_defaults(self):
        spec = BenchmarkSpec.model_validate(spec_data())

        assert spec.timeout_seconds == 900
        assert spec.results_dir == "results"
        assert spec.agent == AgentSpec(type="claude-code")
        assert len(spec.evaluations) == 1
        assert spec.evaluations[0] == Evaluation(name="smoke", command='python -c "print(1)"')


class TestBenchmarkSpecRequiredFields:
    @pytest.mark.parametrize(
        "field",
        ["name", "repository", "commit", "prompt", "agent", "evaluations"],
    )
    def test_missing_required_field_rejected(self, field):
        data = spec_data()
        del data[field]

        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(data)


class TestBenchmarkSpecFieldValidation:
    @pytest.mark.parametrize("bad_name", ["", "../evil", "a/b", "c:\\out", ".hidden", "demo."])
    def test_unsafe_or_empty_name_rejected(self, bad_name):
        # The name is used as a directory name under results/, so path
        # separators and traversal must never reach the filesystem; Windows
        # also silently strips trailing dots from directory names.
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(name=bad_name))

    def test_accepts_reasonable_name(self):
        spec = BenchmarkSpec.model_validate(spec_data(name="Fix_Bug-01.v2"))
        assert spec.name == "Fix_Bug-01.v2"

    @pytest.mark.parametrize("bad_commit", ["not-a-sha", "abc123", "z" * 40, "a" * 3])
    def test_malformed_commit_rejected(self, bad_commit):
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(commit=bad_commit))

    @pytest.mark.parametrize("commit", ["a" * 7, "ABCDEF0" + "1" * 33])
    def test_abbreviated_or_uppercase_hex_commit_accepted(self, commit):
        spec = BenchmarkSpec.model_validate(spec_data(commit=commit))
        assert spec.commit == commit

    def test_unknown_agent_type_rejected(self):
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(agent={"type": "gpt-cli"}))

    def test_agent_command_override_and_extra_args_accepted(self):
        spec = BenchmarkSpec.model_validate(
            spec_data(agent={"type": "claude-code", "command": "/opt/claude", "extra_args": ["--model", "sonnet"]})
        )
        assert spec.agent.command == "/opt/claude"
        assert spec.agent.extra_args == ["--model", "sonnet"]

    def test_empty_evaluations_rejected(self):
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(evaluations=[]))

    def test_evaluation_requires_name_and_command(self):
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(evaluations=[{"name": "only-name"}]))

    def test_duplicate_evaluation_names_rejected(self):
        # Sidecar logs are keyed by evaluation identity: duplicates would
        # silently overwrite each other's captured output.
        duplicate = [{"name": "same", "command": "one"}, {"name": "same", "command": "two"}]

        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(evaluations=duplicate))

    def test_non_positive_timeout_rejected(self):
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(timeout_seconds=0))

    @pytest.mark.parametrize(
        "bad_results_dir", ["/tmp/out", "C:/out", "C:x", "D:data", "../outside", "a/../b"]
    )
    def test_results_dir_must_be_safe_relative_path(self, bad_results_dir):
        # Drive-relative paths like 'C:x' are not is_absolute() on Windows
        # but still escape the results root.
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(results_dir=bad_results_dir))

    def test_nested_results_dir_accepted(self):
        spec = BenchmarkSpec.model_validate(spec_data(results_dir="out/results"))
        assert spec.results_dir == "out/results"

    def test_unknown_top_level_key_rejected(self):
        # Typos in the YAML must fail loudly instead of being silently ignored.
        with pytest.raises(ValidationError):
            BenchmarkSpec.model_validate(spec_data(evalutions=[{"name": "x", "command": "y"}]))
