"""Offline-rescore hardening (v0.6 release hardening, mission VII).

Proves that rescoring never guesses: every missing-evidence path reports
``unrescorable`` with a concrete reason, setup-failed runs are refused
outright, and successful revisions carry the full provenance block.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentbench.rescore import rescore_run


def _make_repo_with_commit(tmp_path: Path) -> tuple[Path, str]:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env,
                   check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                  encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, env=env,
                   check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env,
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


def test_missing_result_json_reports_unrescorable(tmp_path):
    outcome = rescore_run("no-such-run", results_root=tmp_path)
    assert outcome.error == "run not found"
    assert outcome.new_resolved is False


def test_setup_failed_run_is_refused_not_guessed(tmp_path):
    run_dir = tmp_path / "bench" / "r-setup"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({
        "overall": {"status": "setup_failed"},
        "config": {"_benchmark_manifest": "nowhere.yaml"},
    }), encoding="utf-8")
    outcome = rescore_run("r-setup", results_root=tmp_path)
    assert outcome.error is not None
    assert "no agent evidence" in outcome.error
    assert outcome.original_status == "setup_failed"


def test_missing_manifest_hint_reports_unrescorable(tmp_path):
    run_dir = tmp_path / "bench" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({
        "overall": {"status": "passed"},
        "config": {},
    }), encoding="utf-8")
    outcome = rescore_run("r1", results_root=tmp_path)
    assert "manifest no longer recorded" in outcome.error


def test_revision_carries_full_provenance(tmp_path, monkeypatch):
    """Successful rescore revision records identity + old/new verdicts."""
    from agentbench.loader import load_benchmark

    repo, sha = _make_repo_with_commit(tmp_path)
    manifest_dir = tmp_path / "bench"
    hidden_dir = manifest_dir / "hidden"
    hidden_dir.mkdir(parents=True)
    manifest = manifest_dir / "benchmark.yaml"
    manifest.write_text(
        "name: provcheck\n"
        "prompt: solve it\n"
        f"repository: {repo.as_posix()}\n"
        f"commit: {sha}\n"
        "timeout_seconds: 60\n"
        "agent:\n  type: command\n  argv: ['true']\n"
        "evaluations:\n  - name: add-works\n    command: 'python -c \"pass\"'\n",
        encoding="utf-8")

    run_id = "20260101T000000Z-prov"
    run_dir = tmp_path / "provcheck" / run_id
    run_dir.mkdir(parents=True)
    payload = {
        "run_id": run_id,
        "overall": {"status": "passed"},
        "config": {"_benchmark_manifest": str(manifest)},
        "benchmark": {"name": "provcheck", "resolved_commit": sha},
        "scoring": {"resolved": True, "partial_score": 0.42},
    }
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "diff.patch").write_text(
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n     return a + b\n+solved = True\n",
        encoding="utf-8")

    spec = load_benchmark(manifest)
    # Deterministic evaluator: always passes.
    monkeypatch.setattr(
        "agentbench.evaluation.run_evaluation",
        lambda evaluation, **kwargs: type("O", (), {
            "name": evaluation.name, "passed": True, "exit_code": 0,
            "stdout": "", "duration_seconds": 0.0})())

    outcome = rescore_run(run_id, results_root=tmp_path)
    assert outcome.error is None, outcome.error
    revision = json.loads(
        Path(outcome.revision_path).read_text(encoding="utf-8"))
    assert revision["original_run_id"] == run_id
    assert revision["original_status"] == "passed"
    assert revision["original_scoring"]["partial_score"] == 0.42
    assert revision["old_partial_score"] == 0.42
    assert revision["new_resolved"] is True
    assert revision["new_partial_score"] == 1.0
    assert revision["benchmark"]["name"] == "provcheck"
    assert revision["benchmark"]["resolved_commit"] == sha
    assert revision["benchmark"]["config_hash"] == spec.config_hash()
    assert revision["scorer_set_hash"]
    assert "rescored_at" in revision and "agentbench_version" in revision


def test_empty_patch_is_a_clean_skip(tmp_path):
    """Failed/no-op agents record an empty diff; rescore must skip clearly."""
    import json
    from pathlib import Path

    from agentbench.rescore import rescore_run

    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text(
        "name: bench\nrepository: fixture\ncommit: " + "a" * 40 +
        "\nprompt: p\nagent:\n  type: claude-code\n"
        "evaluations:\n  - name: t\n    command: echo ok\n",
        encoding="utf-8",
    )
    root = tmp_path / "results"
    run_dir = root / "bench" / "20260101T000000Z-aaaaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "diff.patch").write_text("", encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps({
        "schema_version": 4,
        "run_id": "20260101T000000Z-aaaaaa",
        "benchmark": {"name": "bench", "resolved_commit": "b" * 40},
        "overall": {"status": "evaluation_failed"},
        "config": {"_benchmark_manifest": str(manifest)},
    }), encoding="utf-8")

    outcome = rescore_run("20260101T000000Z-aaaaaa", results_root=root)

    assert outcome is not None and outcome.error is not None
    assert "empty diff" in outcome.error


def test_apply_failure_returns_outcome_not_none(monkeypatch, tmp_path):
    """A patch that fails to apply must surface as outcome.error, not None."""
    import json
    import os
    import subprocess
    from pathlib import Path

    import agentbench.rescore as rmod
    from agentbench.rescore import rescore_run

    # Fixture repo with one real commit for workspace creation.
    repo = tmp_path / "fixture"
    repo.mkdir()
    env_extra = dict(os.environ,
                     GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                     GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                     GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
                     GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True,
                   capture_output=True, env=env_extra)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()

    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text(
        "name: bench\nrepository: fixture\ncommit: " + head +
        "\nprompt: p\nagent:\n  type: claude-code\n"
        "evaluations:\n  - name: t\n    command: echo ok\n",
        encoding="utf-8",
    )
    root = tmp_path / "results"
    run_dir = root / "bench" / "20260101T000000Z-bbbbbb"
    run_dir.mkdir(parents=True)
    (run_dir / "diff.patch").write_text("diff --git a/x a/x\n", encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps({
        "schema_version": 4,
        "run_id": "20260101T000000Z-bbbbbb",
        "benchmark": {"name": "bench", "repository": "fixture",
                      "resolved_commit": head},
        "overall": {"status": "passed"},
        "config": {"_benchmark_manifest": str(manifest)},
    }), encoding="utf-8")

    def boom(workspace, patch_text):
        raise ValueError("stored patch does not apply cleanly: kaboom")

    monkeypatch.setattr(rmod, "_apply_patch", boom)

    outcome = rescore_run("20260101T000000Z-bbbbbb", results_root=root)

    assert outcome is not None
    assert "kaboom" in (outcome.error or "")
