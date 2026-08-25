"""Benchmark leakage review: answer material must never reach agent workspaces.

The public repository legitimately contains hidden evaluators and reference
patches (maintainer tooling), but a run's cloned workspace is built from the
fixture repository alone. These tests pin that boundary at runtime.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from agentbench.models import (
    AgentSpec,
    BenchmarkSpec,
    Evaluation,
    HiddenEvaluationSpec,
    ReferenceSolution,
)
from agentbench.runner import run_benchmark


class StubWriteAgent:
    """Deterministic stand-in: writes the target file, no model involved."""

    name = "stub"

    def build_invocation(self, *, workspace, prompt, agent_spec):
        from agentbench.adapters.base import AgentInvocation

        script = workspace.parent / f"stub-{id(self)}.py"
        script.write_text(
            "from pathlib import Path\n"
            "Path('agent_change.txt').write_text('x')\n",
            encoding="utf-8",
        )
        return AgentInvocation(argv=[sys.executable, str(script)], input_text=None)

    def capabilities(self):
        return set()


def leaking_spec(repo_path: Path, sha: str, benchmark_dir: Path) -> BenchmarkSpec:
    """Benchmark whose own directory hides answers beside the manifest."""
    hidden = benchmark_dir / "hidden"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "test_secret_behavior.py").write_text(
        "def test_secret():\n    assert True\n", encoding="utf-8"
    )
    reference = benchmark_dir / "reference"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / "fix.patch").write_text(
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# demo\n+# demo (fixed)\n",
        encoding="utf-8",
    )
    return BenchmarkSpec(
        name="leakcheck",
        repository=str(repo_path),
        commit=sha,
        prompt="create agent_change.txt",
        agent=AgentSpec(type="claude-code"),
        evaluations=[Evaluation(name="public-check", command=f'"{sys.executable}" -c "pass"')],
        hidden_evaluations=HiddenEvaluationSpec(
            source="hidden",
            evaluations=[Evaluation(name="secret-behavior", command='"{python}" -m pytest -q')],
        ),
        reference_solution=ReferenceSolution(patch="reference/fix.patch"),
    )


class TestRuntimeIsolation:
    def test_answers_never_enter_the_agent_workspace_or_diff(
        self, make_git_repo, tmp_path
    ):
        repo_path, sha = make_git_repo(files={"README.md": "# demo\n"})
        benchmark_dir = tmp_path / "benchdir"
        benchmark_dir.mkdir()
        spec = leaking_spec(repo_path, sha, benchmark_dir)

        outcome = run_benchmark(
            spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
            benchmark_dir=benchmark_dir,
            keep_workspace=True,
        )

        assert outcome.result.overall["status"] == "passed"
        assert outcome.run_dir is not None

        kept = outcome.workspace_path  # keep_workspace=True preserves it
        assert kept is not None
        for banned in ("hidden", "reference"):
            assert not (kept / banned).exists(), f"{banned}/ leaked into the workspace"
        assert not list(kept.rglob("test_secret_behavior.py"))
        assert not list(kept.rglob("fix.patch"))

        changed = outcome.result.diff["changed_paths"]
        assert changed == ["agent_change.txt"]
        for path in changed:
            assert not path.startswith(("hidden", "reference"))
