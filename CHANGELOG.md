# Changelog

User-facing changes to ub-agents. Each change adds its entry under **Unreleased** in the
same PR. A release turns Unreleased into its version section, and the GitHub release
notes are copied from that section.

## Unreleased

### Added

- Repository-role issue approval checks, maintainer starts and content-bound approval
  records; `ub-agent approve --number N` reviews current input and clears outside
  comments. `doctor` warns when the launcher account can start or approve its own work.
  Pickup enforcement follows in #39. (#38)

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
