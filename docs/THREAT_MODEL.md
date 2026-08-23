# AgentBench Threat Model

AgentBench executes untrusted code (the benchmark repository's fixture and
the coding agent) with the goal of producing trustworthy evidence. This
document states what is defended, what is only observed, and what remains
open. **Docker support improves reproducibility and containment; it is not a
perfect security sandbox.**

Trust assumptions:

* The person writing benchmark/experiment YAML is a trusted operator — they
  already control arbitrary shell commands via `evaluations`.
* Everything the agent does, and everything inside the cloned repository at
  run time, is untrusted input.

| Threat | Current mitigation | Remaining limitation | Future mitigation |
| --- | --- | --- | --- |
| Malicious benchmark (hostile fixture/eval commands) | Operator trust boundary: YAML author already controls shell commands; strict schema validation rejects structural tricks | A malicious *committed* fixture can still do anything the operator could | Out of scope until multi-tenant benchmarks exist |
| Malicious / buggy coding agent | Headless flags only; stdout/stderr captured; timeouts + process-tree kill; diff evidence pinned to pre-agent sha | Agent runs with full user permissions and host network access | Docker backend; OS sandbox (next milestone) |
| Host credential leakage into agent | Container env starts empty; only `pass_env` allowlisted variables forwarded; results record presence (`present: true`), never values | Host-backend runs intentionally inherit the environment (agents need auth); forwarded values exist in container memory | Secret managers; per-run scoped tokens |
| Filesystem escape via benchmark paths | Strict relative-path validation in schemas; hidden/reference sources must resolve inside the benchmark dir; Docker mounts validated against resolved allowed roots and UNC/symlink escapes rejected | Host backend has no containment — an agent can write anywhere the user can | Docker/OS-level confinement |
| Docker socket exposure | The socket is never mounted into containers; the docker CLI is invoked only by the harness from outside | An unsandboxed agent can invoke docker itself if the user is in the docker group (host-level risk that predates AgentBench) | Run agents as non-docker-group users; rootless Docker |
| Network access during runs | Explicit `network: enabled|disabled`; disabled maps to `--network none` | Default is enabled because model APIs require network; host backend cannot disable network | Per-domain proxies; host-backend network namespaces (POSIX) |
| Hidden evaluator exposure | Hidden sources live beside the manifest, never copied into the workspace; hidden evals execute host-side after the agent exits; `{hidden_dir}` resolves to nothing inside containers | Files are not encrypted; a sufficiently curious agent operating before evaluation could theoretically probe the filesystem beyond its workspace | Ephemeral encrypted copies fetched at evaluation time |
| Result tampering by agent | Evidence written to a directory the agent never sees (results root not mounted); JSON written after the agent exits | A fully unsandboxed host agent could still write anywhere on disk mid-run, including results (not currently mounted/read-only-enforced for host runs) | Docker backend makes this structurally impossible for containerized steps; host hardening deferred |
| Agent modifying tests / evaluators | Protected paths + declarative change policies (warn/fail/allowed); violations recorded with matched pattern; classification escalates on fail-grade hits | Detection is post-hoc evidence, not prevention; `.gitignore`-style exclusion semantics apply | Read-only mounts for protected subtrees where filesystems allow |
| Dashboard artifact traversal | Run ids must match the stored format; artifacts resolved only through DB-stored directories; per-segment regex + '.'/'..' rejection + realpath containment; HTML-escaped rendering; read-only (no POST handlers) | Local-only bind mitigates remote attackers but any local user can reach the port while it runs | Unix sockets / auth token if ever needed |
| Command injection | Agent commands are argv lists (no shell); evaluation commands are substituted with plain string replacement and executed via the platform shell *by design* (operators author them) | Evaluation command text is trusted-operator input; placeholders substitute verbatim so operators must not wrap them in fragile quoting | Documented quoting rules; argv-form evaluations |
| Symlink attacks inside workspace | Diff capture uses Git semantics; Docker mount validation resolves symlinks against allowed roots | Host backend follows whatever the agent creates | Containerization |
| SQLite index corruption | Short transactions; INSERT OR REPLACE keyed by run id; additive migrations re-appliable; corruption raises to callers who warn and continue | Index is derived: rebuild by deleting the db file and rescanning | WAL mode + backup rotation if scale demands |

Explicit non-goals: cryptographic protection of hidden tests, anti-cheat
enforcement beyond detection, AI-as-judge quality scoring.
