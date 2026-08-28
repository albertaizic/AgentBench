"""Generate a correct unified diff by editing the local fixture repo.

Usage: python validation/make_patch.py <benchmark-name> <edits.py>
The edits module must define `edits(files: dict[str, str]) -> dict[str, str]`
mapping full rewritten file contents (only changed files need keys).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    name, edits_path = sys.argv[1], Path(sys.argv[2])
    fixture = Path(f"benchmarks/{name}/fixture").resolve()
    spec = importlib.util.spec_from_file_location("edits", edits_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as td:
        clone = Path(td) / "fixture"
        subprocess.run(["git", "clone", "--quiet", str(fixture), str(clone)],
                       check=True)
        files = {
            str(p.relative_to(clone)).replace("\\", "/"): p.read_text(encoding="utf-8")
            for p in clone.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        new_files = dict(files)
        new_files.update(mod.edits(dict(files)))
        for rel, content in new_files.items():
            target = clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "-A"], cwd=clone, check=True,
                       capture_output=True)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--no-color"],
            cwd=clone, capture_output=True, text=True, check=True).stdout
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(f"{name}.patch")
    out.write_text(diff, encoding="utf-8", newline="\n")
    print("wrote", out, f"({len(diff.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
