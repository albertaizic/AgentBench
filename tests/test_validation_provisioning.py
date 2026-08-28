"""Wheel-install validation: fixtures are provisioned from packaged generators.

Installed distributions ship ``create_fixture.py`` but not the generated
``fixture/`` git repository. ``validate_benchmark`` must provision the
deterministic fixture itself instead of failing on first use.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

from agentbench.validation import validate_benchmark

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


def _force_rmtree(path: Path) -> None:
    """Windows-safe rmtree: git object files are created read-only."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            (Path(root) / name).chmod(stat.S_IWRITE)
    shutil.rmtree(path)


def _materialize_generator(benchmark_dir: Path) -> str:
    """Run the generator once and return the pinned commit sha."""
    result = subprocess.run(
        [sys.executable, str(benchmark_dir / "create_fixture.py")],
        capture_output=True, text=True, check=True,
    )
    assert result.returncode == 0, result.stderr
    head = subprocess.run(
        ["git", "-C", str(benchmark_dir / "fixture"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return head.stdout.strip()


def _write_manifest(benchmark_dir: Path, sha: str) -> Path:
    cmd = f'"{Path(sys.executable).as_posix()}" -c "import sys; sys.exit(1)"'
    manifest = benchmark_dir / "benchmark.yaml"
    manifest.write_text(
        textwrap.dedent(
            f"""\
            name: provdemo
            repository: fixture
            commit: {json.dumps(sha)}
            prompt: Make the suite pass.
            agent:
              type: command
              argv: {json.dumps([Path(sys.executable).as_posix(), "--version"])}
            evaluations:
              - name: public-tests
                command: {json.dumps(cmd)}
            expect_broken_baseline: true
            """
        ),
        encoding="utf-8",
    )
    return manifest


def _check(report, name: str):
    for check_name, passed, detail in report.checks:
        if check_name == name:
            return passed, detail
    raise AssertionError(f"check {name!r} missing from {[c[0] for c in report.checks]}")


class TestFixtureProvisioning:
    def test_missing_fixture_is_generated_during_validate(self, tmp_path):
        benchmark_dir = tmp_path / "provdemo"
        benchmark_dir.mkdir()
        (benchmark_dir / "create_fixture.py").write_text(GENERATOR, encoding="utf-8")
        sha = _materialize_generator(benchmark_dir)
        manifest = _write_manifest(benchmark_dir, sha)

        # Simulate a wheel install: generator shipped, generated repo absent.
        _force_rmtree(benchmark_dir / "fixture")

        report = validate_benchmark(manifest)

        passed, detail = _check(report, "repository/fixture exists")
        assert passed, detail
        assert "generated via create_fixture.py" in detail
        assert report.ok, report.checks

    def test_failing_generator_reports_failure(self, tmp_path):
        benchmark_dir = tmp_path / "brokendemo"
        benchmark_dir.mkdir()
        (benchmark_dir / "create_fixture.py").write_text(
            "import sys\nsys.exit(3)\n", encoding="utf-8"
        )
        manifest = _write_manifest(benchmark_dir, "0" * 40)

        report = validate_benchmark(manifest)

        passed, detail = _check(report, "repository/fixture exists")
        assert not passed
        assert "generator failed" in detail


class TestFixtureStub:
    def test_non_git_fixture_stub_is_regenerated(self, tmp_path):
        """Direct-wheel installs may ship file-only fixture dirs (no .git)."""
        benchmark_dir = tmp_path / "provdemo"
        benchmark_dir.mkdir()
        (benchmark_dir / "create_fixture.py").write_text(GENERATOR, encoding="utf-8")
        sha = _materialize_generator(benchmark_dir)
        manifest = _write_manifest(benchmark_dir, sha)

        # Reduce the generated repo to a file-only stub.
        stub = benchmark_dir / "fixture"
        _force_rmtree(stub / ".git")
        assert stub.is_dir() and not (stub / ".git").exists()

        report = validate_benchmark(manifest)

        passed, detail = _check(report, "repository/fixture exists")
        assert passed, detail
        assert "generated via create_fixture.py" in detail
