"""Tests for benchmark YAML loading (agentbench.loader)."""

from __future__ import annotations

import sys
import textwrap

import pytest
from pydantic import ValidationError

from agentbench.loader import LoaderError, load_benchmark
from agentbench.models import BenchmarkSpec

VALID_YAML = textwrap.dedent(
    """\
    name: demo
    repository: https://example.com/repo.git
    commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    prompt: Fix the failing test.
    agent:
      type: claude-code
    evaluations:
      - name: smoke
        command: python -c "print('ok')"
    """
)


def write_benchmark(tmp_path, content: str, filename: str = "benchmark.yaml"):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadBenchmark:
    def test_loads_valid_yaml_into_benchmark_spec(self, tmp_path):
        path = write_benchmark(tmp_path, VALID_YAML)

        spec = load_benchmark(path)

        assert isinstance(spec, BenchmarkSpec)
        assert spec.name == "demo"
        assert spec.evaluations[0].command == "python -c \"print('ok')\""

    def test_missing_file_raises_loader_error(self, tmp_path):
        with pytest.raises(LoaderError, match="not found"):
            load_benchmark(tmp_path / "nope.yaml")

    def test_invalid_yaml_syntax_raises_loader_error(self, tmp_path):
        path = write_benchmark(tmp_path, "name: [unclosed")

        with pytest.raises(LoaderError, match="YAML"):
            load_benchmark(path)

    def test_non_mapping_yaml_raises_loader_error(self, tmp_path):
        path = write_benchmark(tmp_path, "- just\n- a\n- list\n")

        with pytest.raises(LoaderError, match="mapping"):
            load_benchmark(path)

    def test_schema_violation_raises_validation_error(self, tmp_path):
        # Schema problems are distinct from file problems: let pydantic's
        # error propagate so callers can tell bad content from bad files.
        broken = VALID_YAML.replace("name: demo\n", "")
        path = write_benchmark(tmp_path, broken)

        with pytest.raises(ValidationError):
            load_benchmark(path)


class TestSignificantCharactersSurviveRoundTrip:
    def test_yaml_significant_values_round_trip_through_structured_dump(self, tmp_path):
        # Generators must serialize benchmark data structurally (yaml.safe_dump),
        # never by string concatenation: hand-built plain scalars break the
        # moment a value contains ': ', '#', quotes, or newlines.
        import yaml

        tricky_prompt = "Create canary.txt containing exactly: alive\nSecond line # not a comment"
        tricky_command = f'"{sys.executable}" -c "print(\':\' if 1 else 0)"'
        document = {
            "name": "tricky",
            "repository": "https://example.com/repo.git",
            "commit": "a" * 40,
            "prompt": tricky_prompt,
            "agent": {"type": "claude-code"},
            "evaluations": [{"name": "colon: check", "command": tricky_command}],
        }
        path = tmp_path / "generated.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

        spec = load_benchmark(path)

        assert spec.prompt == tricky_prompt
        assert spec.evaluations[0].name == "colon: check"
        assert spec.evaluations[0].command == tricky_command
