"""Reference-baseline runs must persist reproducibility metadata."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from agentbench.baselines import run_reference_baseline
from agentbench.loader import load_benchmark

GENERATOR = textwrap.dedent(
    """\
    import os
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).parent / "fixture"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True,
                       capture_output=True, text=True)

    git("init", "-q")
    (root / "app.py").write_text("VALUE = 1\\n", encoding="utf-8")
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.com",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.com",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
    )
    git("add", "-A")
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True,
                   capture_output=True, text=True, env=env)
    """
)

FIX_PATCH = """\\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def _build_benchmark(tmp_path: Path) -> tuple[Path, Path]:
    benchmark_dir = tmp_path / "envdemo"
    benchmark_dir.mkdir()
    (benchmark_dir / "create_fixture.py").write_text(GENERATOR, encoding="utf-8")
    subprocess.run(
        [sys.executable, str(benchmark_dir / "create_fixture.py")],
        capture_output=True, text=True, check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(benchmark_dir / "fixture"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    ref = benchmark_dir / "reference"
    ref.mkdir()
    (ref / "fix.patch").write_text(FIX_PATCH, encoding="utf-8")

    py = Path(sys.executable).as_posix()
    cmd = f'"{py}" -c "import pathlib; assert \'VALUE = 2\' in pathlib.Path(\'app.py\').read_text()"'
    manifest = benchmark_dir / "benchmark.yaml"
    manifest.write_text(
        textwrap.dedent(
            f"""\
            name: envdemo
            repository: fixture
            commit: {json.dumps(head)}
            prompt: Bump VALUE to 2.
            agent:
              type: command
              argv: {json.dumps([py, "--version"])}
            evaluations:
              - name: value-check
                command: {json.dumps(cmd)}
            expect_broken_baseline: true
            reference_solution:
              patch: reference/fix.patch
            """
        ),
        encoding="utf-8",
    )
    return manifest, benchmark_dir


def test_reference_baseline_persists_environment_metadata(tmp_path):
    manifest, benchmark_dir = _build_benchmark(tmp_path)
    spec = load_benchmark(manifest)

    result, _run_dir = run_reference_baseline(
        spec,
        repository=str(benchmark_dir / "fixture"),
        benchmark_dir=benchmark_dir,
        manifest_path=manifest,
        results_root=tmp_path / "results",
    )

    assert result.environment["agentbench_version"]
    assert result.environment["python_version"]
    assert result.environment["platform"]
