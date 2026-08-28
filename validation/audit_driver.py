"""Incremental full-corpus quality audit (release hardening, mission XIII).

Runs oracle-5/nop-5 audits per benchmark and appends one JSON line per task
so progress survives timeouts. Skips already-recorded tasks on restart.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

OUT = Path("validation/audit-full-32x55.jsonl")


def done() -> set[str]:
    if not OUT.exists():
        return set()
    return {json.loads(l)["benchmark"] for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()}


def main() -> int:
    from agentbench.audit import audit_benchmark
    from agentbench.discovery import discover

    seen = done()
    for manifest in discover():
        try:
            spec_name = json.loads(json.dumps(manifest)) if isinstance(manifest, str) else None
        except Exception:
            spec_name = None
        # discover() yields manifest paths
        path = Path(manifest)
        name = path.parent.name
        if name in seen:
            continue
        try:
            report = audit_benchmark(path, oracle_runs=5, nop_runs=5)
            record = {
                "benchmark": report.name,
                "quality_status": report.quality_status,
                "dimensions": [
                    {"name": d.name, "verdict": d.verdict, "detail": d.detail}
                    for d in report.dimensions
                ],
            }
        except Exception as exc:  # noqa: BLE001 - record failures, keep going
            record = {"benchmark": name, "quality_status": "audit_error",
                      "error": f"{type(exc).__name__}: {exc}",
                      "traceback": traceback.format_exc(limit=3)}
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(record["benchmark"], record["quality_status"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
