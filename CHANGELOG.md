# Changelog

User-facing changes to ub-agents. Each change adds its entry under **Unreleased** in the
same PR. A release turns Unreleased into its version section, and the GitHub release
notes are copied from that section.

## Unreleased

## 0.1.6 — 2026-10-02

**Upgrading:** agents now work only on issues and PRs that a maintainer (`maintain` or
`admin`) started by applying a trigger label. After an outside edit or push, a
maintainer re-applies the label or runs `ub-agent approve --number N`. Give the launcher
account `write` only; `doctor` warns if it can start or approve its own work. `check`
now rejects Claude `runtime-args` that set `--output-format`. Upgrading from 0.1.5
needs no coordinated stop: `kill -TERM` each launcher, upgrade, restart.

### Changed

- The launcher enforces maintainer starts and outside-input approvals for every
  issue and PR run, including preparation. Failed pickup or post-claim checks park
  work without spending attempts; agent context includes only trusted or cleared
  feedback. Outside PRs require an approved head, with accepted agent revisions
  in the base repository inheriting eligibility; changed fork heads need explicit
  maintainer approval. `ub-agent approve` accepts PRs, pins their head and clears
  outside comments, reviews and review comments (#39).

### Fixed

- Agent prompts and check-running roles require a single session that is never
  resumed: finish checks in the foreground or wait for every background job,
  then end the run with `ub-agent report` (#70).
- Agents with `different-runtime-from` use only their first configured runtime when
  the PR's current head has no accepted report from the named agent, blocking if that
  runtime's CLI isn't installed. Pending handoffs wait for acceptance and successful
  source release or recovery; identified sources retain the independence checks (#61).
- Claude runs record tool calls, tool results and the final result, including
  permission denials, in `process.log` using verbose streaming JSON. `check` rejects
  Claude `runtime-args` that override `--output-format` (#68).

## 0.1.5 — 2026-10-02

**Upgrading:** no configuration edits are required. Stop every launcher for a project
and upgrade them together before restarting: older launchers ignore the new v2
coordination records, so a mixed fleet can claim and run an item that an upgraded
launcher already owns (#44). A 0.1.4 launcher has no graceful stop, so stop it
between runs. From 0.1.5, `kill -TERM` lets the current run finish and exits 0 (#52).

### Added

- `launch` appends stdout and stderr to `.ub-agent/launch.log` with UTC timestamps,
  flushing each line to both the terminal and file, including for `--once` (#55).
- Repository-role issue approval checks, maintainer starts and content-bound approval
  records that reject ambiguous body revisions; `ub-agent approve --number N` reviews
  current input and clears outside comments. `doctor` warns when the launcher account
  can start or approve its own work.
  Pickup enforcement follows in #39. (#38)

### Changed

- Before each new claim, the launcher reloads `ub-agent.yaml` from its refreshed
  checkout and replans with the current agent, triggers, runtime and outcomes.
  Each run keeps its claimed configuration; invalid reloads stop without charging
  an attempt (#52).
- SIGTERM stops further claims, lets the current run or recovery finish its report,
  label transitions and cleanup, and exits 0; idle launchers exit promptly after
  any in-progress checkout refresh finishes. Later SIGTERM signals allow process
  termination and cleanup to complete. SIGINT and SIGHUP still terminate active
  execution, including during a graceful stop (#52).
- The README recommends Homebrew for installation and upgrades, links to release
  configuration guidance, and keeps checkout installs as a development alternative (#48).
- Coordination comments lead with a readable summary and collapsed JSON. Released
  runs minimize superseded records, and parked runs post an Action needed notice
  with evidence and resume steps. Unchanged blocked and parked items print once
  per launch session (#44).

### Fixed

- Ctrl-C during a GitHub request reports a stop with exit status 130 instead of a
  GitHub or retry failure, preserving reported outcomes for expiry recovery when
  completion is interrupted and still running cleanup hooks (#55).
- Nonzero exits without a report retry with backoff up to `max-attempts`. A report
  made before a nonzero exit is validated and applied normally, preserving label
  transitions and accepted handoff provenance; the exit code is logged (#42).

## 0.1.4 — 2026-10-02

**Upgrading:** no configuration edits are required. The launcher now stops on a dirty
control checkout or one that is off the default branch (#37). To let an existing
integrator send PRs back, add `changes-requested: {add: [needs-changes]}` to its
`outcomes` and the matching paragraph from the starter `integrator.md` (#46).

### Changed

- Before each run, the launcher fetches `origin`, fast-forwards its control checkout on
  the default branch and rereads the role's instructions, so instruction changes take
  effect without a restart. The control checkout must be clean, on the default branch
  and free of local-only commits; otherwise the launcher stops with a message (#37).
- `max-attempts` limits consecutive failures per item and agent. An accepted success
  resets the count, while interrupts, blocked reports and paused transitions leave it
  unchanged. A changed head, state or trigger before execution counts as a failure and
  retries with backoff (#43).
- The starter integrator can send a PR back with a `changes-requested` outcome when
  it conflicts with the base branch or lacks a required changelog entry (#46).

### Fixed

- Continuous `ub-agent launch` retries transient GitHub discovery failures before
  claiming work, with no GitHub writes from failed polls. Backoff grows from 5 to
  60 seconds; a usable rate-limit reset within that cap replaces it. The sixth
  consecutive failed poll stops with restart instructions. `launch --once` and
  `status` still stop on the first error (#33).
- `status` shows each agent's owning run's report (or its latest run's report when
  that agent has no live lease), following recovery leases to the original run.
  A run that has not reported no longer shows an earlier run's outcome, and a row
  no longer shows another agent's report (#35).

## 0.1.3 — 2026-10-02

**Breaking:** 0.1.2 configurations need edits before `check` accepts them. See the
[v0.1.3 release notes](https://github.com/uberblick-ai/ub-agents/releases/tag/v0.1.3).

### Added

- `ub-agent cleanup` previews and removes stale agent worktrees and branches, and runs
  optional project cleanup hooks (#30).

### Changed

- Claims are no longer renewed. A claim lasts the agent's timeout plus 15 minutes, plus
  the cleanup hook's timeout when one is configured (#32).
- Every agent declares `outcomes` and reports `ub-agent report --outcome NAME` (#32).
- Every launcher for a project must use the same GitHub account (#32).
- `runtime-args` may not change the model, the effort or the session (#32).
- An implementer continues an interrupted run's draft PR from the `earlier_branches`
  in its context; a handoff is rejected while another open PR from an earlier run of
  the issue exists. An issue run and a run on the PR from its branch never run at the
  same time (#32).
- `doctor` checks only the prerequisites `launch` cannot check itself, including the
  token's permission to change labels (#32).

### Removed

- `lease-minutes`, `renewal-minutes`, `operators`, per-agent `cwd`, `runtimes:`
  adapters for other CLIs, and `report --status success` (#32).

## 0.1.2 — 2026-10-01

### Added

- `init` writes a generic `AGENTS.md` unless one exists, offers to create the
  configured workflow labels after you confirm (otherwise it prints `gh label create`
  commands), and writes commented, runtime-specific `runtime-args` (#29).
- `doctor` checks that the configured labels exist and warns about agents without
  `runtime-args` (#29).

## 0.1.1 — 2026-10-01

### Added

- Implementers publish early work as a draft PR (#16).
- Queue order: PR work first, then priority labels (`queue.priority`), then oldest
  first, with an optional milestone gate (`queue.milestones: gate`) (#19, #25).
- Issues wait for open GitHub blockers, and blockers and PRs inherit the priority of
  the work they unblock or close (`queue.dependencies`) (#26).
- Agents declare outcomes in `ub-agent.yaml`, and the launcher applies their label
  transitions after validating the result (#22).

## 0.1.0 — 2026-10-01

First release.

- `ub-agent init`, `check`, `doctor`, `launch`, `status`, `report` and `retry` (#7, #14).
- Codex and Claude runtimes, custom CLI adapters and direct commands (#7).
- Cooperative claims, durable outcomes, retries and recovery recorded on GitHub (#7, #11).
- A starter workflow with issue preparation, implementation, cross-runtime review and
  integration under a project merge policy (#7, #17).
