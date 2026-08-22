"""One-shot final verification: cleanup + full suite count + LIVE claude smoke.

Runs the real `agentbench run` against a local fixture repo with the real
Claude Code binary (no stub), bounded by a hard timeout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

REPO = Path(__file__).parent
AGENTBENCH = str(REPO / ".venv" / "Scripts" / "agentbench.exe")


def _on_readonly(func, target, _exc):
    os.chmod(target, stat.S_IWRITE)
    func(target)


def purge_temp() -> None:
    removed = []
    for entry in Path(tempfile.gettempdir()).glob("agentbench-*"):
        try:
            shutil.rmtree(entry, onexc=_on_readonly)
            removed.append(entry.name)
        except OSError:
            print(f"could not remove {entry.name} (locked)")
    print(f"purged {len(removed)} stale temp dir(s)")


def make_fixture() -> tuple[Path, str]:
    root = Path(tempfile.gettempdir()) / f"agentbench-live-{uuid.uuid4().hex[:8]}"
    origin = root / "origin"
    origin.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "s@s",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "s@s",
    }

    def git(*args: str) -> str:
        out = subprocess.run(["git", *args], cwd=origin, env=env, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    git("init", "-q", "-b", "main")
    (origin / "README.md").write_text("# live smoke\n", encoding="utf-8")
    (origin / "check_canary.py").write_text(
        "import pathlib, sys\nsys.exit(0 if pathlib.Path('canary.txt').exists() else 1)\n",
        encoding="utf-8",
    )
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return root, git("rev-parse", "HEAD")


def live_smoke() -> bool:
    root, sha = make_fixture()
    results = root / "results"
    yaml_path = root / "live.yaml"
    # Structured serialization, never string concatenation: the prompt
    # contains ': ' which breaks hand-built plain scalars (the v0.1 bug).
    document = {
        "name": "live-smoke",
        "repository": str(root / "origin"),
        "commit": sha,
        "prompt": "Create a file named canary.txt at the repository root containing exactly: alive",
        "agent": {"type": "claude-code"},
        "evaluations": [
            {"name": "canary-exists", "command": f'"{sys.executable}" check_canary.py'},
        ],
        "timeout_seconds": 300,
    }
    yaml_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    proc = subprocess.run(
        [AGENTBENCH, "run", str(yaml_path), "--results-dir", str(results)],
        capture_output=True,
        text=True,
        timeout=420,
    )
    print(proc.stdout[-2000:])
    if proc.stderr.strip():
        print("STDERR:", proc.stderr[-800:])
    print(f"exit code: {proc.returncode}")
    payload_path = next((results / "live-smoke").glob("*/result.json"), None)
    stats_ok = False
    if payload_path:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        print(json.dumps({k: payload[k] for k in ("agent", "diff", "overall")}, indent=2)[:900])
        # Acceptance: exactly one change — canary.txt, +1/-0 — nothing else.
        patch = (payload_path.parent / "diff.patch").read_text(encoding="utf-8")
        changed_paths = re.findall(r"^diff --git a/(.+?) b/", patch, flags=re.M)
        print("changed paths:", changed_paths)
        diff = payload["diff"]
        stats_ok = (
            payload["overall"]["status"] == "passed"
            and not payload["agent"]["timed_out"]
            and changed_paths == ["canary.txt"]
            and diff["files_changed"] == 1
            and diff["insertions"] == 1
            and diff["deletions"] == 0
        )
    if proc.returncode == 0 and stats_ok:
        shutil.rmtree(root, onexc=_on_readonly)
        return True
    print(f"SMOKE FIXTURE PRESERVED FOR DIAGNOSIS: {root}")
    return False


if __name__ == "__main__":
    purge_temp()
    try:
        ok = live_smoke()
        print("LIVE SMOKE:", "PASSED" if ok else "FAILED")
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"LIVE SMOKE ERROR: {exc!r}")
