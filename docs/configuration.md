# Configuration reference

This page lists every key in `ub-agents.yaml` with its type, default and validation,
plus contract details the website leaves out. Each section links to its page in the
[configuration guide](https://agents.uberblick.ai/docs/configuration/index.html),
which explains the keys with examples.

`ub-agents.yaml` sits at the root of the repository the agents work on. Unknown keys
are errors. `ub-agents doctor --offline` validates the effective configuration without
contacting GitHub. `--config PATH` selects another file.
[`ub-agents init`](https://agents.uberblick.ai/docs/commands/init.html) writes a
starter file.

## Machine-local overrides

Explained on the website: [Configuration](https://agents.uberblick.ai/docs/configuration/index.html#machine-local-overrides).

An optional, untracked `ub-agents.local.yaml` in the base file's directory, after
resolving symlinks, overrides settings on one machine. The base file stays required.
Relative paths in both files resolve against that directory. A missing, empty or
comment-only local file changes nothing.

- Mappings merge recursively by key, including `agents` and each agent. Omitted keys
  and other agents inherit from the base.
- Scalars and lists replace the whole base value. Lists are never concatenated, so
  repeat every base entry you want to keep.
- There is no syntax to add or delete. An explicit `null` is a value and is validated
  normally.
- An agent defined only in the local file must be complete.

```yaml
# ub-agents.local.yaml: keeps the shared runtime and adds a machine-only one
agents:
  implementer:
    runtime: [codex:gpt-6.1-sol:high, codex:machine-model:high]
```

Both files need unique string keys. Validation runs on the merged result, and errors
caused by the local file name it. Launch rereads both files before each new run. The
local file is not copied into private worktrees. `init` adds it to `.gitignore`; in an
existing project, add that line before creating the file.

## Top level

Explained on the website: [Configuration](https://agents.uberblick.ai/docs/configuration/index.html).

| Key | Type and default | Rules |
|---|---|---|
| `repository` | Required string | GitHub `owner/name`. Must match the checkout's `origin`. |
| `agents` | Required mapping | Nonempty. Names are lowercase slugs: a letter, then letters, digits, `-` or `_`. See [Agents](#agents). |
| `shared-instructions` | Path, optional | Policy read before every role's instructions. Relative to the configuration file, inside the project (including symlink targets) and readable UTF-8. |
| `limits` | Mapping | See [Limits](#limits). |
| `poll-seconds` | Positive number, `30` | Minimum gap between discovery-pass starts. See [polling](polling.md). |
| `stop-labels` | Label or list, `[needs-human]` | Labels that park an item. `[]` configures none. |
| `approvals` | `on` or `off` | When unset, resolved each pass from visibility: `on` for public, `off` for private and internal. See [approvals](approvals.md). |
| `trusted-bots` | List, `[]` | Bot logins trusted for feedback only, unique ignoring case. See [trusted bots](approvals.md#trusted-bots). |
| `trusted-pr-authors` | List, `[]` | Bot logins trusted as same-repository PR authors with approvals on; bodies stay withheld. Unique ignoring case. See [trusted PR authors](approvals.md#trusted-pr-authors). |
| `launchers` | List, optional | See [Launcher accounts](#launcher-accounts). |
| `queue` | Mapping | See [Queue](#queue). |
| `cleanup` | Mapping, optional | See [Project cleanup hook](#project-cleanup-hook). |
| `checkout-setup` | Mapping, optional | See [Checkout setup](#checkout-setup). |
| `runtime-updates` | Mapping, optional | See [Daily runtime maintenance](#daily-runtime-maintenance). |
| `runtime-args` | Mapping, optional | Arguments per CLI. See [Built-in runtimes](#built-in-runtimes). |

`origin` may be `https://github.com/OWNER/REPO`, `git@github.com:OWNER/REPO` or
`ssh://git@github.com/OWNER/REPO`, with an optional `.git` or trailing `/`. An SSH host
alias also works when `ssh -G HOST` reports `hostname github.com`. The check needs no
network, so `doctor --offline` runs it too.

## Launcher accounts

Explained on the website: [Launcher accounts](https://agents.uberblick.ai/docs/configuration/launchers.html).

```yaml
launchers: [bot-a, alice]
```

`launchers` is an optional, nonempty list of GitHub logins, unique ignoring case and
without surrounding whitespace. Without it, coordination records from any account
with `write`, `maintain` or `admin` access count. With it, only listed accounts count,
and they still need `write` or higher. Use the same list on every machine. Stop an
account's launcher before removing it or lowering its role: either revokes all its
records, including open claims. [Trusted comments](coordination.md#trusted-comments)
has the full rules.

## Checkout setup

Explained on the website: [Checkout setup](https://agents.uberblick.ai/docs/configuration/checkout-setup.html).

```yaml
checkout-setup:
  command: [sh, -c, 'mise trust ./mise.toml && exec mise run install']
  when-changed: [pnpm-lock.yaml, mise.toml]
  timeout-seconds: 600
```

| Key | Type and default | Rules |
|---|---|---|
| `command` | Required argv list | Runs without a shell in the checkout being set up, before the agent starts. Runtime permissions do not apply. |
| `when-changed` | Required list | Literal repository-relative file paths. Absolute paths, directories and `..` are rejected. |
| `timeout-seconds` | Number, `600` | Positive, at most 3600. |

Keep tracked files unchanged and install into ignored paths, so the next refresh still
finds a clean checkout.

**Control checkout.** Before each new run, after the refresh, the launcher compares the
watched files with the last commit where setup succeeded, or before any success, with
the HEAD before the fast-forward. If any differ, setup runs once before claiming. Pulls
made by hand are not detected. A failure stops launch before a claim and charges no
attempt. Setup then stays pending and runs on every launch until it succeeds. Its state
and logs live under `$XDG_STATE_HOME/ub-agents/checkouts/` (default
`~/.local/state/ub-agents/checkouts/`).

**Private worktrees.** With `worktree: true`, setup runs once in each new private
worktree, whatever `when-changed` says, except fork PR worktrees. A same-repository PR
installs from its head, so the head's install scripts run. Output goes to
`checkout-setup/process.log` in the run's log directory. A failure ends the run before
the agent starts, removes the worktree and counts toward the attempt limit.

## Project cleanup hook

Explained on the website: [Cleanup hook](https://agents.uberblick.ai/docs/configuration/cleanup.html).

```yaml
cleanup:
  command: [./scripts/cleanup-agent-worktree]
  timeout-seconds: 60
```

| Key | Type and default | Rules |
|---|---|---|
| `command` | Required argv list | Runs without a shell in the control checkout, so `./scripts/...` resolves there even when a candidate changes its scripts. |
| `timeout-seconds` | Number, `60` | Positive, at most 3600. |

The hook runs before the launcher removes a private worktree it owns, including after
a timeout, interruption or lost ownership, and before each removal in the
[startup sweep](#stale-artifact-cleanup). It starts only after the agent's processes
are confirmed stopped; a requested stop does not skip it. Shared working directories
have no hook. If the run's claim expires during the hook, the hook is stopped and the
worktree kept for recovery.

`UB_AGENTS_CLEANUP_CONTEXT` points to a JSON file with these fields, `null` when
unavailable:

| Field | Value |
|---|---|
| `repository` | `owner/name` |
| `run` | Run id |
| `agent` | Agent name |
| `assignment` | Issue or PR number |
| `kind` | `issue` or `pr` |
| `handoff` | The PR the run handed off to |
| `status` | The released claim's result during later cleanup; otherwise the reported status |
| `outcome` | The named outcome |
| `worktree` | Absolute worktree path |
| `branch` | Branch name |

The hook also gets `UB_AGENTS_REPOSITORY`, `UB_AGENTS_RUN`, `UB_AGENTS_AGENT`,
`UB_AGENTS_ASSIGNMENT`, `UB_AGENTS_WORKTREE` and `UB_AGENTS_BRANCH`, empty when
unknown. Other `UB_AGENTS_*` variables are removed.

Exit zero permits removal. A nonzero exit, start failure or timeout keeps the worktree
and branch and logs `cleanup-hook-failed` in the run's `events.jsonl`. The outcome and
labels stand, and a later sweep retries. Make the hook safe to run twice. Put
operator-only recovery steps in a project operations document linked from `AGENTS.md`,
not in agent instructions.

## Stale artifact cleanup

Explained on the website: [Cleanup hook](https://agents.uberblick.ai/docs/configuration/cleanup.html).
There are no keys; this is what launch cleans up on its own.

Every `ub-agents launch`, including `--agent`, `NUMBER` and `--once`, sweeps leftovers
once in the background after its startup checks. There is no periodic sweep. It looks
at worktrees under this checkout's `.ub-agents/worktrees/`, local branches named
`ub-agents/<agent>/<number>/<run>` or the older `ub-agent/...`, and fork PR head refs
left by interrupted runs. It removes each one unless:

1. its run has a live claim on this host;
2. the branch heads an open PR, or is checked out in a worktree the sweep keeps; or
3. the run's processes may still be running, for example after unconfirmed cleanup.

Claims on other hosts, missing claims, ended runs, uncommitted changes and locked
worktrees do not prevent removal. If the coordination scan or PR lookup fails, the
affected artifacts stay. Other tools' worktrees and remote branches are never touched.
A crashed run's claim stays live until it expires, so a quick restart keeps its
worktree until a later launch.

The cleanup hook runs before each worktree removal; a failed hook keeps the worktree
and branch. Decisions go only to `.ub-agents/sessions/<session>.cleanup.log`; without
a writable log, the sweep removes nothing. Before deleting a branch whose tip no
fetched `origin` branch or merged PR head contains, the log records its
`unpushed tip SHA`. Restore it with `git branch <name> <sha>`.

At run end, the launcher deletes the run's local branch only if an `origin` branch
contains its tip; otherwise it keeps and logs the branch.

The sweep also prunes run directories under `.ub-agents/runs/` and ended session files
older than 7 days, unless a live claim, possibly running process, kept worktree or
unreadable diagnostics protect them. The cleanup log reports counts and space freed.
Pruning never follows symlinks. Retention is fixed.

## Queue

Explained on the website: [Queue](https://agents.uberblick.ai/docs/configuration/queue.html).

```yaml
queue:
  priority:
    labels: [priority:urgent, priority:high, priority:normal, priority:low]
    default: priority:normal
  milestones: order
  dependencies: wait
```

| Key | Type and default | Rules |
|---|---|---|
| `priority.labels` | List | Nonempty, unique labels, highest first. |
| `priority.default` | Label, optional | Must be one of `labels`. |
| `milestones` | `ignore` (default), `order` or `gate` | How milestones rank new issues. |
| `dependencies` | `wait` (default) or `ignore` | Whether issues wait for open blockers. |

Without `queue`, items have equal priority, milestones are ignored and dependency waits
apply. [Selection order](coordination.md#selection-order) defines the ranking.

**Priority.** An item takes its highest configured label, or `default`; without a
default, it ranks below every label. The launcher never changes priority labels. A PR
takes the highest of its own priority and that of each open local issue it closes.
Each reference in its body needs a closing keyword, such as `Closes #21`. Closed issues
and other repositories do not count.

**Milestones.** `order` and `gate` use the open milestones with open issues or PRs,
alphabetically by title. An unreadable milestone list or item stops selection. With
`ignore`, milestones are not read.

**Dependencies.** With `wait`, a new issue start waits while any GitHub "blocked by"
issue is open, including in other repositories, rechecked before claiming. An open
local blocker inherits the highest priority of its open local dependents, directly or
transitively, and in `order` and `gate` their earliest milestone. Cycles share what
they reach. A failed dependency read stops selection. With `ignore`, links are not
read. PR work, recovery and started runs never wait for blockers.

## Agents

Explained on the website: [Agents](https://agents.uberblick.ai/docs/configuration/agents.html).

Each agent has exactly one of `runtime` or `command`.

| Key | Type and default | Rules |
|---|---|---|
| `trigger` | Required label or list | Labels that select the agent, subject to [approvals](approvals.md). |
| `outcomes` | Required mapping | See [Outcomes and transitions](#outcomes-and-transitions). |
| `kind` | `issue`, `pr` or `either` (default) | Which items the agent takes. |
| `runtime` | `cli:model:effort` or list | `cli` is `codex` or `claude`. A list gives alternatives in order. |
| `instructions` | Path | Required with `runtime`. Relative to the configuration file, inside the project and readable. Reread before each run. |
| `command` | Argv list | Runs instead of an LLM session. A relative path containing `/` resolves against the configuration's directory. |
| `worktree` | Boolean, `false` | `true` runs in a private checkout: a same-repository PR's exact head, a fork PR's base branch tip, or a fresh branch for an issue. See [pull requests](approvals.md#pull-requests). |
| `runtime-args` | List or mapping | Replaces all top-level `runtime-args`; `[]` or `{}` disables them. |
| `different-runtime-from` | Agent name or nonempty list | Use a different CLI and model from each named runtime agent's accepted outcome: the latest on an issue, or for the current PR head. `null` clears the setting. See [runtime independence](coordination.md#runtime-independence-and-candidate-provenance). |
| `on-conflict` | Outcome name | `pr` agents only; an outcome that adds no stop label. Recorded instead of starting the agent when GitHub reports the PR as conflicting. |
| `health-check` | Argv list | See [Project health checks](#project-health-checks). |
| `retrospectives` | Positive integer | See [Retrospectives](#retrospectives). |
| Limit keys | | Override the timeout and retry [`limits`](#limits). The lease keys are top level only. |

`different-runtime-from` accepts a single name such as `implementer` or a list such
as `[implementer, reviewer]`. Names must be unique, identify other runtime agents,
and cannot refer to command agents. Local configuration replaces the whole value;
use `null` to clear it. Doctor checks runtime independence against each named agent.

### Project health checks

Explained on the website: [Agents](https://agents.uberblick.ai/docs/configuration/agents.html#health-check).

`health-check: [./scripts/check-corpus]` is a nonempty argv list, resolved like
`command`. The launcher runs it without a shell in the control checkout, only when the
agent has otherwise-ready work, before any claim. The timeout is fixed at 60 seconds,
and a pass is reused for up to five minutes. While it fails, the agent's ready items show `waiting` and
get no claims, attempts or label changes; other agents keep working. `status` runs the
check too. `doctor` never runs it.

### Retrospectives

`retrospectives` is a positive discussion number in `repository`, the agent's board.
`doctor` checks that the discussion exists. Only agents with this key get the
`retrospective` command and posting guidance in their [run prompt](coordination.md#run-prompt).
See `ub-agents help retrospective` and
[Agent retros](https://agents.uberblick.ai/docs/best-practices/retrospectives.html).

## Outcomes and transitions

Explained on the website: [Outcomes](https://agents.uberblick.ai/docs/configuration/outcomes.html).

```yaml
outcomes:
  approved: {add: [ready-to-merge]}
  changes-requested: {add: [needs-changes], max-rounds: 3}
  needs-human: {add: [needs-human]}
```

`outcomes` is a nonempty mapping from names to transitions:

| Key | Type and default | Rules |
|---|---|---|
| `add` | List, `[]` | Must not include the agent's own triggers. May add a stop label. |
| `remove` | List, `[]` | Must not include a stop label, and no trigger of the agent may be one. |
| `max-rounds` | Positive integer | Needs a configured stop label, and the outcome must not add one. |
| `sub-issues` | Mapping with only `add` | Nonempty list of `issue` or `either` agent triggers, no stop labels. Agents that take issues only. |

**Transitions.** After validating ownership, the candidate SHA and issue links, the
launcher removes all the agent's triggers and the `remove` labels from the assignment.
It adds the `add` labels to the handoff PR if the report names one, otherwise to the
assignment. Other labels stay. When the destination is the assignment and a label is
in both lists, `add` wins. `--status retry|blocked`, failures, timeouts,
interruptions and invalid reports cause no transition.

The launcher rereads the assignment first. A missing trigger blocks the transition. A
stop label on the assignment or handoff PR pauses it with no label changes, and the
outcome is never applied later: a person sets labels or runs `ub-agents retry`. A stop
label added after the transition starts stays, and the transition completes. An
interrupted transition finishes through [recovery](coordination.md#recovery).

**Rounds.** A round is one accepted record of that outcome by that agent on that item
since its latest `retry`. After `max-rounds` rounds, the next report of that outcome
adds the first stop label instead of its `add` labels.
[Assignments and attempts](coordination.md#assignments-and-attempts) covers resuming.

**Sub-issues.** The launcher adds the `sub-issues` labels to open native sub-issues in
the same repository that the claim's account created after the claim, and stores that
set in the outcome record for recovery. Other sub-issues are named in the summary. An
`either` agent can report this outcome only on an issue. With approvals on, these
sub-issues inherit the parent's start; see [approvals](approvals.md#start-suspension-and-comments).

**Reporting.** Agents report through the launcher's `report_command`; see
[explicit outcomes](coordination.md#explicit-outcomes). The claim snapshots the
declarations, so configuration edits do not change a running assignment. Stop reports,
meaning `--status blocked` and outcomes that add a stop label, need at least one
`--action` or `--option`: repeat `--action` for independent asks and `--option` for
alternatives, recommendation first.

## Limits

Explained on the website: [Limits](https://agents.uberblick.ai/docs/configuration/limits.html).

| Key | Default | Rules |
|---|---|---|
| `lease-minutes` | 30 | Positive number, top level only. How long a claim lasts; each renewal extends it by this much. |
| `lease-renew-minutes` | 10 | Positive number, top level only. Time between renewals. Adding the 20-second request timeout and a one-second margin must still be shorter than `lease-minutes`. |
| `agent-timeout-minutes` | 180 | Positive number. Deadline for agent execution only; the lease keeps renewing. |
| `max-attempts` | 5 | Positive integer. Consecutive failures per item and agent before pickup stops. |
| `retry-backoff-seconds` | 60 | Zero or more. First retry delay; doubles with each failure. |
| `max-backoff-seconds` | 3600 | At least `retry-backoff-seconds`. Longest retry delay. |

Each claim records its own expiry, so launchers with different lease settings can
share a repository. A shorter lease lets another launcher take over sooner after one
dies; a live run keeps renewing until its execution timeout.

## Built-in runtimes

Explained on the website: [Runtimes](https://agents.uberblick.ai/docs/configuration/runtimes.html).

The [run prompt](coordination.md#run-prompt) arrives on stdin:

- `codex:MODEL:EFFORT` runs `codex exec --json --model MODEL --config model_reasoning_effort="EFFORT" --config model_reasoning_summary="detailed"`.
- `claude:MODEL:EFFORT` runs `claude --print --output-format stream-json --verbose --model MODEL --effort EFFORT --thinking-display summarized`.

`process.log` records each CLI's JSON output as is. The launcher reads usage limits
from it, but only `ub-agents report` sets an outcome.

Top-level `runtime-args` maps `codex` and `claude` to argument lists. Runtime agents
without their own `runtime-args` inherit it; command agents do not. Each run appends
only the selected CLI's list. An agent's own `runtime-args` replaces the whole
mapping: a list applies to every alternative, and a mapping's keys must name CLIs in
its runtime list. `doctor --offline` validates every entry.

Arguments must not change the model or effort or resume a session. Claude arguments
must not set `-c` or `--output-format`. Codex arguments must not set `--ephemeral`,
because the launcher may read the session file for usage limits. Codex's `-c` is
allowed for other settings in the `codex` entry. A shared list must satisfy every
listed CLI's rules.

### Runtime usage pauses

Explained on the website: [Runtimes](https://agents.uberblick.ai/docs/configuration/runtimes.html#usage-limits).

When a run reports a usage limit, its launcher pauses new runs on that CLI until the
reported reset plus one minute. A missing or past reset, or one more than seven days
away, pauses for 15 minutes. Pauses live in memory, per launcher; a restart clears
them. Claude limits come from a rejected `rate_limit_event`, `error: rate_limit` or
`api_error_status: 429`. Codex limits come from limit snapshots or
`usage_limit_exceeded`, or the run's own session file under `$CODEX_HOME/sessions/`.
A run that ends at a limit without an outcome is released as `retry`, with no attempt
charged. Alternatives skip paused CLIs; an item with all runtimes paused stays
`waiting`.

### Daily runtime maintenance

Explained on the website: [Runtime updates](https://agents.uberblick.ai/docs/configuration/runtime-updates.html).

```yaml
runtime-updates:
  claude: auto
  codex:
    command: [mise, upgrade, codex]
  gh: auto
  ub: auto
  timeout-seconds: 300
```

| Key | Type and default | Rules |
|---|---|---|
| `claude`, `codex`, `gh`, `ub` | `auto`, `off` (default) or `{command: [...]}` | A command runs as argv without a shell, in the launcher's environment, and resolves like `command`. It must not start with `sudo`, `su`, `doas` or `pkexec`, and should update only that CLI. |
| `timeout-seconds` | Number, `300` | Positive, at most 3600. |

Without `runtime-updates`, nothing runs. Claude Code and Codex are checked only when an
agent's `runtime` uses them; `gh` whenever its policy is not `off`. `ub` is checked
when it would find `.uberblick.json` in the project root or an ancestor, regardless
of the agent runtime. Without that file, a configured `ub` policy reports an unused
skip. `auto` supports:

| Runtime / installation | Updater |
|---|---|
| Claude native installer or npm global | `claude update` |
| Claude Homebrew cask | `brew upgrade --cask claude-code` (or `claude-code@latest`) |
| Codex npm global | `npm install -g @openai/codex@latest`, with the npm that owns that prefix |
| Codex Homebrew cask or formula | `brew upgrade --cask codex` or `--formula codex` |
| GitHub CLI Homebrew formula | `brew upgrade --formula gh` |
| Uberblick Homebrew formula (`uberblick`) | `brew upgrade --formula uberblick` |

Other installations, including shims, system packages, npm prereleases and
installations the user cannot write, are skipped with a reason. No updater uses `sudo`
or switches install methods. For Claude, `DISABLE_UPDATES` in the environment or in
Claude's user or managed settings skips the update, even with a command.
`ub: auto` supports only the `uberblick` formula through its owning Homebrew;
other formulas, casks, system packages, shims and unknown installs need
`runtime-updates.ub.command`, for example `{command: [ub, update]}`.

Checks run between runs, never during one. Each completed check prints one line and
starts a 24-hour cooldown; skips start none. npm, Homebrew and command updates wait
until no tracked run uses the installation. Only runs started by ub-agents are
tracked, so an update can replace files under a Claude or Codex session you started
yourself. Every run in an Uberblick project reserves an installed `ub` through
execution and cleanup, even with updates off, so its MCP server finishes before
an update and new runs start on the new binary. Missing `ub` does not hold runs
unless an earlier update left it unusable.
Cooldowns live under
`$XDG_STATE_HOME/ub-agents/runtime-updates`, per installation, shared by all launchers
on the machine. **Opting out in one project does not stop another project's launcher
from updating the same installation.**

After each check, the launcher runs the CLI's `--version`. If that fails, the CLI is
unavailable until a later check passes, even with updates off; while `gh` is
unavailable, no run starts. An unusable `ub` holds new runs in Uberblick projects
until a health check passes; other projects can still run.

`ub-agents init` writes `ub: auto` only when updates are enabled and the Uberblick
extra is selected.

### Runtime permissions

Explained on the website: [Permissions](https://agents.uberblick.ai/docs/configuration/permissions.html).

The launcher adds no permission flags, so the CLIs usually cannot edit files or push.
Grant each role what it needs in `runtime-args`. When `doctor` warns that an agent has
none for a listed CLI, configure top-level `runtime-args` or agent overrides covering
every CLI in each agent's runtime list.

Online `doctor` also checks the MCP servers named by tool references in role or
shared instructions and runtime arguments, plus project MCP registrations. It
reports whether each runtime would load them in a fresh worktree and probes only
loaded stdio servers, listing their tool counts. These checks are optional warnings
and are skipped with `--offline`. See [doctor's MCP checks](https://agents.uberblick.ai/docs/commands/doctor.html#mcp-servers)
and [runtime setup for Uberblick](https://agents.uberblick.ai/docs/guides/with-uberblick.html).

For Codex, `[--sandbox, danger-full-access]` grants full access, including commits and
pushes. Its default `workspace-write` sandbox cannot commit in private worktrees,
whose Git metadata lives in the main checkout; [Codex sandbox options](design/codex-sandbox.md)
records tested alternatives.

For Claude, write report, read and retrospective rules with the placeholder, such as
`"Bash({report_command} report *)"`. Claude matches the command text including its
path, so `Bash(ub-agents *)` does not match.

In `runtime-args` only, `{scratch}` becomes the run's absolute scratch path and
`{report_command}` the launcher's absolute report command, anywhere in an argument,
as in `"--add-dir={scratch}"`. Quote them in YAML; unquoted `{scratch}` is a mapping.
Any other word of letters, digits, `-` or `_` in braces is an error. Other braces,
such as JSON `'{"a": 1}'`, pass through. There is no other templating.

Scratch is `$XDG_STATE_HOME/ub-agents/<owner>/<repo>/runs/<run>/scratch`, with
`~/.local/state` used when `XDG_STATE_HOME` is unset, empty or relative. It is outside
the checkout, so runtimes limited to their working directory need access, such as
Claude's `--add-dir`. A scratch path inside the checkout fails the run before the
agent starts.

## Commands instead of agents

Explained on the website: [Command agents](https://agents.uberblick.ai/docs/configuration/command-agents.html).

A command reports by running the shell-quoted command in `UB_AGENTS_REPORT` followed by
`report --outcome NAME --summary TEXT`. Split it, for example with Python's
`shlex.split`, to run it without a shell. Every runtime and command gets:

| Variable | Value |
|---|---|
| `UB_AGENTS_CONTEXT` | Path to a JSON file describing the assignment |
| `UB_AGENTS_REPORT` | Absolute command for the launcher's startup code copy and interpreter, followed by `report`, `read` or `retrospective`; also `report_command` in context |
| `UB_AGENTS_RUN_CONFIG` | Absolute path to `.ub-agents/runs/<run>/run.json` |
| `UB_AGENTS_REPOSITORY` | `owner/name` |
| `UB_AGENTS_ASSIGNMENT` | Issue or PR number |
| `UB_AGENTS_RUN` | Run id |
| `UB_AGENTS_SCRATCH` | Absolute path to the run's private scratch directory |
| `UB_AGENTS_LEASE_ID` | The claim's comment id |
| `UB_AGENTS_CANDIDATE_SHA` | The PR's head commit; empty for issues |
| `UB_AGENTS_BRANCH` | The branch to work on, when known |

`run.json` pins `repository`, `run`, `assignment`, `lease_id`, `agent`, `approvals`,
`trusted-bots`, `trusted-pr-authors`, `triggers` and `retrospectives`, plus
`launchers` and `sub-issue-labels` when configured. In-run `read`, `retrospective` and `report` exit
nonzero before contacting GitHub unless it is valid and matches `UB_AGENTS_REPOSITORY`
and `UB_AGENTS_RUN`.

`TMPDIR` is unchanged. The launcher creates scratch with mode `0700` and removes it
once the run's processes are confirmed stopped. A removal failure records
`scratch-removal-failed` in `events.jsonl` without changing the result. Run logs stay
for the [7-day retention](#stale-artifact-cleanup).

## Commands

`ub-agents help COMMAND` shows current options without configuration or network. The
command pages are built from `site/content/commands/`.

| Command | Reference |
| --- | --- |
| `init` | [Create starter configuration and instructions](https://agents.uberblick.ai/docs/commands/init.html) |
| `doctor` | [Validate locally or diagnose launch prerequisites](https://agents.uberblick.ai/docs/commands/doctor.html) |
| `launch` | [Run the queue or one item](https://agents.uberblick.ai/docs/commands/launch.html) |
| `status` | [Inspect work, ownership and outcomes](https://agents.uberblick.ai/docs/commands/status.html) |
| `retry` | [Reset attempts after fixing their cause](https://agents.uberblick.ai/docs/commands/retry.html) |
| `help` | [Help and version](https://agents.uberblick.ai/docs/commands/help.html) |
| `report`, `read`, `retrospective` | [Loop internal commands, run by agents](https://agents.uberblick.ai/docs/commands/loop-internal.html) |
