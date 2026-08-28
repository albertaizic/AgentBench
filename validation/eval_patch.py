"""Evaluate an arbitrary patch against a benchmark's full evaluator set.

Used by the release-hardening session for:
* alternative-valid-solution checks (patch SHOULD pass everything);
* negative mutation checks           (patch MUST fail something).

Usage: python validation/eval_patch.py <benchmark-name> <patch-file>
Prints PASS/FAIL per evaluator and an overall verdict.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.insert = None


def main() -> int:
    name, patch_file = sys.argv[1], Path(sys.argv[2])
    from agentbench.loader import load_benchmark
    from agentbench.workspace import create_workspace
    from agentbench.evaluation import Evaluation, run_evaluation, run_hidden_evaluation
    from agentbench.runner import effective_public_scorers

    manifest_path = find_manifest(name)
    spec = load_benchmark(manifest_path)
    base = manifest_path.parent

    repo = resolve_repo(spec, base)
    ws = create_workspace(str(repo), spec.commit)
    try:
        patch = patch_file.read_text(encoding="utf-8")
        if patch.strip():
            apply_patch(ws.path, patch)
        outcomes = []
        for view in effective_public_scorers(spec):
            o = run_evaluation(Evaluation(name=view.id, command=view.command),
                               workspace=ws.path, timeout=spec.timeout_seconds)
            outcomes.append((f"public/{view.id}", bool(o.passed)))
        he = spec.hidden_evaluations
        if he is not None:
            hidden_dir = base / he.source
            for ev in he.evaluations:
                o = run_hidden_evaluation(ev, workspace=ws.path,
                                          hidden_dir=hidden_dir,
                                          timeout=spec.timeout_seconds)
                outcomes.append((f"hidden/{ev.name}", bool(o.passed)))
        verdict_all = all(ok for _, ok in outcomes)
        print(f"== {name} / {patch_file.name}")
        for label, ok in outcomes:
            print(f"   {'PASS' if ok else 'FAIL'}  {label}")
        print("OVERALL:", "PASS" if verdict_all else "FAIL")
        return 0 if verdict_all else 1
    finally:
        ws.cleanup()


def find_manifest(name):
    from agentbench.discovery import discover
    for m in discover():
        if m.parent.name == name:
            return m
    raise SystemExit(f"unknown benchmark {name}")


def resolve_repo(spec, base):
    from agentbench.loader import resolve_repository_path
    return resolve_repository_path(spec.repository, base_dir=base)


def apply_patch(workspace, patch_text):
    from agentbench.rescore import _apply_patch
    _apply_patch(workspace, patch_text)


if __name__ == "__main__":
    sys.exit(main())
