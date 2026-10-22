# Parallel gates experiment (#616)

Prototype only. Do not merge this branch or open a PR for it. The handoff is a
separate PR containing only `docs/design/parallel-gates.md`.

Base: `9b1b6b4afb8bb034b81f4c355334ec77d34f1275`. Tested 2026-10-22 with
Python 3.14 on Linux. No runtime dependencies were added.

## Run

From this branch in an isolated checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
mkdir -p .ub-agents/spike-scratch
.venv/bin/python -m spikes.parallel_gates_dry_run --scratch .ub-agents/spike-scratch
```

The command makes no GitHub or model calls. `tests.support.FakeGitHub` stores real
coordination records; separate real `Loop` instances plan and claim the same PR.
The harness drives `reported_verdict` and `settle` for gate completions and calls
`tick_item` with a recording executor to prove that the integrator starts.
Completion interleavings are deterministic. Two tests also race tentative claims
on two threads; all threads are joined. The existing test helpers isolate local
state and stub instruction refresh and observation publication.

## Result

All ten checks passed:

```text
both pass: two live leases -> visual-ok + security-ok -> integrator executed
changes: running sibling cancelled at completion; no security-ok or integrator
claim race: distinct parallel agents both own; duplicate and exclusive agents wait
issue handoff: existing remove clears source issue; destination passes need a new target-remove operation
head move: stale in-flight work rejected; mixed-head labels leave join waiting
shared branch: linked issue writer excludes both PR gates until release
recovery: expired visual outcome replayed while security stayed live; no reexecution
same-head reopen: previous accepted passes cannot satisfy the new opening
duplicate claim race: one winner, losing tentative lease withdrawn
two gates: first pair -> intermediate join/fan-out -> second pair -> integrator
```

## Limits

- This is programmatic configuration. `parallel` and `trigger_all` are dataclass
  fields only; YAML parsing, doctor, init, retry and documentation are unimplemented.
- New lease fields are experimental additions to v3, without schema hardening or
  an upgrade protocol. Production needs a versioned contract and launcher rollout.
- Parallel roles are simulated candidate readers. The executor starts no models,
  private worktrees, networked tools or long-running reviewer processes.
- Cancellation is neutral at completion, not an early process stop. The cancelled
  lease has unchanged attempt effect; its reported pass is unaccepted. Full CLI,
  attempt accounting, notices and view support for cancellation remain to build.
- Concurrent label finalization, crashes between individual label writes, source
  handoff recovery and cross-item claim races are not modeled. Per-trigger opening
  identities and report-ID invalidation ordering only cover these serialized traces;
  production needs ordered durable transition intents and recovery fencing.
- Mixed-head joins fail closed but do not rearm consumed review triggers. An
  explicit new fan-out is needed to rerun both gates on a changed head.
- The issue/PR shared-branch exclusion remains conservative and is tested that way.
  Status and the terminal are unchanged and can still attribute the first live
  lease to both rows; no combined multi-launcher view is supplied.

The prototype demonstrates feasibility, not a merge-ready implementation.
