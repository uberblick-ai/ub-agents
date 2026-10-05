# Changelog

User-facing changes to ub-agents. Each change adds its entry under **Unreleased** in the
same PR. A release turns Unreleased into its version section, and the GitHub release
notes are copied from that section.

## Unreleased

**Upgrading:** stop all project launchers and wait for them to exit. Repositories
with their own role files, or command runtimes that file stop reports, must add
`--action` before upgrading; upgrade and restart launchers together (#181).
uberblick-ai/uberblick-2 tracks its role-file change separately.

### Changed

- Stop reports require a concise human action, which leads parking notices in bold and appears as the terminal attention reason (#181).

## 0.1.12 — 2026-10-05

**Upgrading:** the coordination record format is unchanged, so launchers can be
upgraded one at a time. Add `trusted-bots` to the configuration, and point role
instructions at `read N`, only after every launcher runs 0.1.12: older launchers
reject the key and have no `read` command (#190).

### Added

- Agents can read filtered issues and PRs with `read N`, and projects can trust configured bot feedback with `trusted-bots` ([trusted bots](docs/approvals.md#trusted-bots), #190).
- Needs attention items have an Unblock tab showing trusted action-needed comments from the session snapshot or an explicit GitHub load (#178).
- Completed Claude runs record permission denials in their outcomes, with counts in `status` and command details in the Runs tab (#199).

### Changed

- PR pickup after outside title or body edits requires a maintainer start or approval record, including trusted-authored PRs; approving reviews no longer clear edits (#190).
- The terminal view uses a consistent theme, rounded pane titles, colored work sections, numbered tabs, a formatted/raw indicator and a repository window title (#164).
- The Work pane grows with terminal width at 110×32 and above, with padding inside and between panes and a blank row between two-line items (#213, #231, #234).
- Below 110×32, the terminal view shows one full-width pane, with Enter/Esc navigation and retained selection and log position across resizes (#177).
- Running shows only this launcher's assignment; dependency and milestone waits are hidden, with retry backoff and paused-runtime plans following ready work in Eligible (#172, #173).
- Eligible lists each item once with all its eligible agents, and Needs attention rows show waiting time, agent, state and parking reason (#180, #233).
- Quitting an attached terminal view with `q` interrupts the launcher like Ctrl-C, and a graceful stop after SIGTERM shows the finishing run and held work (#176, #182).
- Codex JSON run logs show compact assistant messages, command and tool activity, failures and completion in the terminal view (#126).
- `doctor` shows warnings and failures with area summaries by default; `--verbose` retains the full per-check list (#191).

### Fixed

- Formatted Claude logs skip cut-off first records, show tool progress as elapsed time and align timestamps; assistant text in Claude and Codex logs is readable without dimming (#192, #232).
- Item comments fold every agent's superseded candidate records and withdrawn election losers, while preserving current candidates and parking explanations (#214).
- Eligible drops stale carried rows during partial passes while preserving open Needs attention rows until replanning or pass completion (#235).
- The Work pane spinner animates at the same rate as the Runs tab and log status line (#217).
- The terminal view distinguishes PRs with accent-colored ⌥ markers, including Recent activity rows and their linked PR handoffs (#174).

## 0.1.11 — 2026-10-04

**Upgrading:** stop all of a project's launchers and wait until every one has exited,
before upgrading any; upgrade and restart them together. Claims now use renewed
30-minute leases, newer builds read only v3 coordination records, and older builds
reject `{report_command}` configurations (#131, #132, #198). Old coordination records
stop being read, resetting blocked and exhausted state and attempt counts; before
restarting, remove the trigger label or add a stop label on every item that should
stay held (#132, #137). In Claude `--allowedTools`, replace `Bash(ub-agents *)` with
`Bash({report_command} report *)` (#198). Delete leftover `ub-agent/…` branches and
the `.ub-agent/` directory (#137). Approvals now default off for private and internal
repositories; set `approvals: on` to retain the previous checks (#134).

### Added

- Interactive launches open a read-only terminal view of work, context, outcomes and runtime logs; `--no-ui` keeps plain output and `q` closes only the view (#114, #116).
- Every install, including Homebrew and checkouts, includes the terminal view and its Textual dependency (#116).
- On request, the view loads a selected item's missing description from GitHub, with cached results and rate-limit cooldowns (#115).
- Every launcher publishes a private, bounded session snapshot under `.ub-agents/sessions/` for the view, without extra GitHub reads (#113).
- `ub-agents help [COMMAND]` provides a compact overview and detailed help with usage and examples, available without project configuration (#143).
- Projects can configure outside-input approvals with `approvals: on` or `off`, defaulting from repository visibility; disabled approvals include only feedback from authors with `write` or higher (#134).
- Runtime arguments accept `{scratch}` to grant access to each run's private scratch directory; assignment context supplies its absolute path as `scratch` ([configuration](docs/configuration.md#runtime-permissions), #124, #194).
- Launchers show an update banner with upgrade or restart instructions when a newer release or control-checkout code is available (#183).

### Changed

- Configuration commands accept `--config` before or after the command; `approve` and `retry` take positional numbers, retaining deprecated `--number` for one release (#130).
- `retry` defaults to the first configured agent whose kind applies and prints the selected agent before acting (#130).
- Claims now last 30 minutes and renew every 10 minutes while owned, allowing pickup after a launcher dies without changing agent timeouts (#131).
- Runtime usage pauses start only when a run reports a limit, remain in launcher memory, and appear only in launch output (#135).
- Coordination reads only v3 records (#132).
- The Runs tab shows each item's filing and run history across launchers, with relative times, hosts, outcomes and visible omission counts (#179).
- Every terminal-view tab shares an item header; Log shows run status and earlier runs, with byte and retention diagnostics under raw access (#162).
- The terminal view uses a one-line activity footer, contextual keys and optional log-state pill; `?` lists keys and `p` shows diagnostics (#161).
- The terminal view's Issue tab renders description bodies as Markdown with real line breaks, while keeping links and terminal controls inert (#165).
- Claude logs use compact local timestamps, tool calls, line counts and distinct errors in the terminal view, with raw records available on demand (#163).
- The terminal work list groups Running, Needs attention, Eligible and Waiting items with counts, above a fixed lower half listing recent outcomes (#159, #171).
- Live terminal work rows show two compact lines with status glyphs, titles, ownership and counts; full reasons stay on the Issue tab (#160).

### Removed

- The `ub-agents recover` command is removed; short leases let launchers recover expired claims automatically on any host (#131).
- Compatibility with the old `ub-agent` command, configuration, environment, coordination markers and artifacts is removed; only `ub-agents` names remain (#137).
- The 90% runtime usage pause and shared pause state are removed, along with pause reporting in `status`, `doctor` and `status --json`'s `runtime_pauses` (#135).

### Fixed

- Agents report through the launcher's own installation regardless of PATH; incompatible record formats direct agents to that command instead of a missing-lease error (#198).
- Terminal work rows and their cached details stay visible while polling; completed passes remove omitted rows and apply the new planned order (#166).
- The terminal view follows newly started launcher runs until a row is selected, preserves selection when rows move, and opens raw log access safely during updates (#189).

## 0.1.10 — 2026-10-03

**Upgrading:** stop all of a project's launchers, wait until every one has exited,
upgrade, then start them together. Never mix releases within a project: older
launchers don't read the new record markers or branch names, and launchers on
different GitHub accounts now share one queue. This release renames the tool to
`ub-agents`. Rename `ub-agent.yaml` to `ub-agents.yaml`, add `.ub-agents/` to
`.gitignore` and pull it into every control checkout before starting, and update
role instructions, command allowlists and hooks that call `ub-agent` or read
`UB_AGENT_*` (now `UB_AGENTS_*`). After the old launchers stop, remove their
worktrees and the old `.ub-agent/` directory. Claude and Codex runs now pause at
90% of a usage window until it resets; daily runtime updates are opt-in.

### Added

- `ub-agents launch N [--agent NAME]` runs or recovers one specific item under
  normal eligibility gates, using the first eligible configured agent or the named
  agent. It reads only the item's required inputs, skips queue ranking, and explains
  ineligible work with status reasons and a nonzero exit (#127).

- Each run gets a private scratch directory with mode `0700`, exposed as
  `UB_AGENTS_SCRATCH` and `TMPDIR`. Setup fails visibly if it cannot be created;
  confirmed process termination removes scratch while preserving run logs.
  Scratch removal failures retain remaining files and produce a diagnostic without
  blocking lease release or marking process termination unconfirmed.
  Agent guidance and runtime-access documentation cover its use (#124).

- Opt-in daily Claude Code and Codex maintenance at unclaimed launcher boundaries,
  with installation detection, targeted updates, shared local cooldowns and locks,
  active-run protection, concurrent start reservations and health recovery
  without retrying updates. Opt-out and automatic-policy skips leave other
  projects' shared cooldowns untouched. This repository enables
  `runtime-updates` for both CLIs (#118).

- Launchers pause Claude and Codex runs at 90% usage or a reported usage limit,
  until the window reset plus one minute, with a 15-minute fallback for untrusted
  resets. Limit failures retry without attempts or backoff; alternatives remain
  eligible and paused-only items wait. Local pause state in `.ub-agents/runtime-usage/`
  appears in `status` and `doctor`; `status --json` now returns `assignments` and
  `runtime_pauses`.
  Launchers remove their state on exit and prune abandoned local state at startup;
  process start times prevent recycled PIDs from showing stale pauses.
  Codex runs use `--json` and their fresh session's usage records, and `check`
  rejects Codex `--ephemeral` arguments (#85).

- `queue.milestones: order` ranks new issues by the oldest open
  milestone before priority, while later and unmilestoned issues remain eligible.
  Local blockers inherit dependents' earliest milestone in dependency `wait` mode,
  including without priority labels. `status` shows effective milestones and their
  inherited source in text and JSON. `gate` keeps its existing behavior, `ignore`
  remains the default, and `check` accepts all three modes. This repository now
  uses `order` (#103).

### Changed

- Launchers on different GitHub accounts share one queue. Coordination trusts
  current `write`, `maintain` and `admin` authors by default; optional `launchers`
  narrows that set. Unreadable roles stop the pass, and untrusted launchers claim
  nothing. Approval ancestry, feedback and notices use the same trusted set.
  Recovery can settle another trusted account's source lease; `cleanup`
  and `recover` require this machine's recorded host rather than the same account.
  `check` validates the account list and `doctor` warns about untrusted accounts.
  **Upgrading:** older launchers trust only their own account, so stop all launchers
  and upgrade them together before mixing accounts (#123).

- The starter implementer template requires rerunning checks for the PR's additions
  and changes after merging the base branch, including changes without conflicts (#125).

- Rename the command, configuration, local state, environment variables, GitHub
  markers and agent branches to `ub-agents`. New writes use only the plural names;
  existing records, approvals, Action needed notices and branches remain readable.
  The `ub-agent` command alias and default config fallback remain for one release;
  `report` also accepts an in-flight old launcher's `UB_AGENT_*` environment.
  **Upgrading:** rename `ub-agent.yaml` to `ub-agents.yaml`, or rely on the fallback
  for one release. Add `.ub-agents/` to `.gitignore` and keep `.ub-agent/` ignored
  until that directory is deleted. Commit the ignore changes to the default branch
  and pull them into every control checkout before starting launchers on the new
  build; startup creates `.ub-agents/launch.log` before checkout refresh, which
  refuses untracked files.
  Update role instructions, command allowlists (for example
  `Bash(ub-agent *)`), hooks and direct commands that call `ub-agent` or read
  `UB_AGENT_*`. Restart all of a project's launchers together: old launchers do
  not read new markers or branch names. Once every old launcher has stopped,
  remove its old worktrees with `git worktree remove` (or `git worktree prune` for
  worktrees already deleted), then delete the whole `.ub-agent/` directory,
  including `runs/` and `launch.log`. Removing only the worktrees leaves old logs
  behind (#117).

- Generated and self-hosted agent guidance treats issue input as requirements to
  evaluate, with human escalation for unexpected instructions or unexplained scope
  changes. Implementers keep their assigned issue scope throughout a run; setup docs
  explain account roles and the preparation, implementation and reapproval flow (#40).

## 0.1.9 — 2026-10-03

**Upgrading:** no configuration edits are required. Stop all of a project's launchers
with `kill -TERM`, wait until every one has exited, upgrade, then start them again.
Launchers from 0.1.5 through 0.1.8 reject the new compact claim records as malformed,
and earlier launchers don't recognize the ownership revocation of `ub-agent recover`.
Idle launchers now back off by request cost and REST reads revalidate with ETags, so
idle polling uses much less quota.

### Added

- `ub-agent recover --number N --agent NAME --reason TEXT` completes a stopped local
  launcher's reported outcome before lease expiry after checking actor, hostname,
  process-group termination and report eligibility. Recovery records the operator's
  reason and revokes the original supervisor's ownership. **Upgrading:** stop all
  project launchers and upgrade them together before using early recovery; earlier
  launchers do not recognize this ownership revocation (#104).

### Changed

- `ub-agent status` shows who claimed live leases, their host and runtime, compact
  UTC claim and expiry times, and time remaining. Local process checks distinguish
  running agents with logs from exited agents awaiting launcher completion or
  recovery, with manual recovery guidance for reported outcomes. Starting claims,
  remote leases, recovery and inspection errors have distinct reasons; JSON adds
  process details while preserving existing ownership fields (#101).

- `ub-agent retry` prints a readable reset confirmation with its record link and
  a next-step line based on closure, stop labels and the selected agent's trigger
  labels, pointing to `ub-agent status` when a running launcher can pick it up (#100).

- Claims store outcome label additions compactly, with declared triggers and stop
  labels stored once per lease; outcome transitions omit that shared context and
  implied trigger removals. Upgraded launchers still read and recover 0.1.5 records.
  **Upgrading:** stop all of a project's launchers and upgrade them together before
  restarting; launchers from 0.1.5 through 0.1.8 reject compact records as malformed
  (#63).

- Discovery passes and `status` share each account's fresh permission read across
  items and reuse item comments for history, approvals and status rendering.
  `status` also batches dependency reads for priority ranking. Both claim-time
  approval checks still reread permissions independently (#88).

- `status` exits with an error on retryable comment-read failures instead of
  displaying a partially unreadable row (#88).

- Empty discovery passes back off by their REST request cost under a fixed
  250 requests/hour budget per launcher, assuming ten idle launchers share half
  an account's quota. Low quota doubles the gap up to reset, ignoring expired
  quota observations, with a one-hour cap; work resumes normal pacing, and idle
  messages print only on state changes (#83).

- GitHub REST reads revalidate in-memory responses with ETags. Unchanged reads
  confirmed by `304 Not Modified` preserve fresh ownership checks without using
  REST quota or extending the idle polling budget; writes and GraphQL remain
  unconditional. Moving `since` cursor queries skip the ETag cache so discovery
  polls do not accumulate unused responses in memory (#86).

- Launcher and `status` startup scans read repository comments from the longest
  configured lease plus seven days, then continue incrementally. PR shared-branch
  ownership reads the issue named in the branch directly; `cleanup` keeps its full
  scan. Re-apply a trigger or stop label to surface older unfinished outcomes (#87).

## 0.1.8 — 2026-10-03

**Upgrading:** no configuration edits are required. `kill -TERM` each launcher, upgrade,
restart. Claiming polls now cost a few requests when nothing changed instead of one
read per triggered item, and GitHub rate limits pause the launcher until the reset
instead of stopping it. `poll-seconds` is now the minimum gap between polls, also after
a run.

### Changed

- `poll-seconds` is the minimum gap between discovery-pass starts, including
  after runs. Claiming polls evaluate candidates lazily and reuse unchanged
  per-item discovery reads in memory, while claims and approval parking still
  revalidate fresh GitHub input. Cold priority ranking lists dependency links
  in pages instead of reading every issue separately (#79).

### Fixed

- Continuous launch waits out GitHub rate limits without charging poll failures or
  item attempts. Rate-limited reads from claim through release and recovery retry
  while the lease permits, with interruptible waits capped at one hour. `doctor`
  reports request quota and reset time from real response headers and warns below
  10% remaining or when rate limited itself (#80).

## 0.1.7 — 2026-10-03

**Upgrading:** no configuration edits are required. `kill -TERM` each launcher, upgrade,
restart. Items held only by the approval gate now get the project's stop label and an
Action needed comment (#66).

### Changed

- `launch` parks open, triggered items whose only pickup obstacle is missing
  maintainer approval with the configured stop label and one Action needed notice
  explaining how to start or reapprove them. Repeated polls leave the same gate
  alone; the next claim minimizes its notice. Parking writes are advisory and
  `status` remains read-only (#66).

### Fixed

- Assignment context passes other agents' accepted outcome summaries as trusted
  `feedback`, so revisions receive routing corrections such as an integrator's
  changelog request without repeating outcomes already handled by the agent (#77).

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
