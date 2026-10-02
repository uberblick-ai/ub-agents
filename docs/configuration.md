# Configuration reference

`ub-agent.yaml` sits at the root of the repository the agents work on. Unknown keys are
errors; `ub-agent check` validates the file.

## Top level

| Key | Meaning |
|---|---|
| `repository` | GitHub `owner/name`. It must match the checkout's `origin`. |
| `agents` | The agents, by name. |
| `limits` | Default clocks and retry limits for every agent. |
| `poll-seconds` | Minimum gap between discovery-pass starts, including after a run (default 30 seconds). |
| `stop-labels` | Labels that park an item (default `[needs-human]`). |
| `cleanup` | Optional project cleanup hook and timeout, run before private worktree removal. |
| `queue` | Priority ranking, dependency waits and an optional milestone gate (defaults to FIFO, waiting for blockers, with milestones ignored). |

`ub-agent launch`, including `--once`, appends stdout and stderr to
`.ub-agent/launch.log` in the control checkout. Every file line starts with a UTC
ISO 8601 timestamp; terminal text stays unchanged. Each line is flushed immediately
to both destinations, including the final stop or error message. The log is never
truncated or rotated. Use `tail -f .ub-agent/launch.log` to follow the loop from
another terminal. See [Stopping and restarting](../README.md#stopping-and-restarting)
for signal handling, including during a GitHub request.

`poll-seconds` measures the minimum time between the starts of successful
continuous discovery passes. A run's report, transitions and cleanup finish
immediately; the launcher then waits only for the part of that interval still
remaining. If the run already took the interval, the next pass starts immediately.
See [Stopping and restarting](../README.md#stopping-and-restarting) for signals
during waits. Failed-poll retry delays below are independent of this interval,
and `launch --once` never waits after its pass.

Claiming discovery evaluates candidates in rank order and stops once it claims
work. Lower-ranked rows are evaluated, announced and approval-parked by a later
pass that reaches them. `status` evaluates every row and remains read-only.
Each launcher retains per-item discovery inputs in memory: history, approval
inputs and permissions, PR details, and dependency links. Changes in the issue
list (including `updated_at`) or the incremental repository comment scan invalidate
that item's reads. A fresh claim-approval denial also drops the item's cached
inputs so the next reached pass can plan its gate. Claims and approval parking
always revalidate with fresh reads;
cached input never authorizes a claim or a write. Restarting a launcher drops its
cache. With configured priorities, cold discovery lists the dependency graph in
pages to preserve inheritance without one REST request per queued issue.

For a rough request budget, an unchanged warm pass costs one request per page of
open issues/PRs, plus the incremental repository comment scan (usually one page),
and an optional milestone list. List pages hold up to 100 rows; a full REST page
also needs a request to check for a following page. A cold pass or changed item adds
roughly 5–10 reads for each candidate actually reached, with extra pages for long
histories and additional authors' permission checks. Configured priorities add a
paginated dependency-graph list on cold discovery; very large dependency lists may
need extra pages. Fresh claim/recovery reads, approval-parking writes, execution
heartbeats and completion add their own requests. `status` pays for every row.

For interval `P` seconds and average discovery cost `R`, budget up to
`R × 3600 / P` requests per loop per hour, then add execution/write costs. For
example, a two-request unchanged pass at 30 seconds is about 240 requests/hour;
five loops sharing one account use about 1,200 before execution. Sum all loops
using the account, including loops on other repositories, and leave headroom
within GitHub's account limit (commonly 5,000 REST requests/hour). GraphQL has a
separate point budget; graph-list query cost depends on its connections. Long
runs reduce the number of discovery passes per hour.

Continuous `ub-agent launch` retries failed discovery polls for request timeouts,
connection failures and HTTP 5xx responses. The fixed backoff starts at **5 seconds**,
doubles after each consecutive failure and caps at **60 seconds**. The launcher stops
on the **sixth consecutive failed poll**; a completed poll resets the count, including
one that finds no work. These values are not configuration keys and are independent
of agent execution retries under `limits`.

A **403 or 429** is a rate limit when the response has `X-RateLimit-Remaining: 0`,
`Retry-After`, or GitHub's "API rate limit exceeded" or "secondary rate limit"
message. Headers on real requests are authoritative; the launcher does not use
`GET /rate_limit`. Primary limits wait until `X-RateLimit-Reset` plus **5 seconds**
of margin. Secondary limits wait for `Retry-After` seconds. Missing or unreadable
wait metadata falls back to **one minute**. Each wait is capped at **one hour**;
afterward the read is retried, and another rate limit starts another wait.

Time spent waiting for a rate limit counts toward the minimum `poll-seconds` gap
between discovery-pass starts. A completed pass waits only for any gap still left;
if the wait or run already used that time, the next pass starts immediately.

Rate limits do not count toward the poll failure limit or an item's attempts.
Each wait prints `GitHub rate limit reached; waiting until <reset UTC> (<n> min)`
and makes no GitHub writes while waiting. Authentication, permission, missing
repository, malformed response and unclassified failures still stop immediately.
The error names the request and tells the operator to fix the cause and restart
`ub-agent launch`.

Each skipped poll for another transient error prints its error and next delay,
makes no GitHub writes and does not report an empty queue. Discovery includes
initial authentication and fresh reads immediately before a claim, including the
default-branch read for instruction refresh. See
[Stopping and restarting](../README.md#stopping-and-restarting) for signals during
discovery waits. `launch --once` and `status` still fail on their first discovery
error.

From claim election through release, including completion recovery, rate-limited
reads wait and retry under the active lease. If the wait would reach or outlast
lease expiry, the launcher takes the lost-ownership path and leaves expiry recovery
to finish durable completion. See
[Stopping and restarting](../README.md#stopping-and-restarting) for signals during
owned-run waits. Rate-limited writes retain their existing handling and are not
replayed by this retry mechanism.

## Project cleanup hook

```yaml
cleanup:
  command: [./scripts/cleanup-agent-worktree]
  timeout-seconds: 60
```

Without `cleanup`, no hook runs. `command` must be a nonempty argv list; no shell
or substitutions are used. The timeout defaults to 60 seconds and must be finite,
positive and at most 3600 seconds. `ub-agent check` rejects unknown keys and invalid
values. The command runs with the operator's configuration checkout as its working
directory, so `./scripts/...` resolves there, even when a candidate changes its own
configuration or scripts.

The hook runs before launcher removal of an owned private worktree, including
execution timeout, interruption and lost ownership, and before `cleanup --apply`
removal. It runs only after agent execution has been confirmed stopped. Shared
agent working directories have no hook. The hook gets its own process group,
log directory and deadline; surviving helpers are terminated and checked using
the same supervision as agent processes. An already requested launcher stop does
not skip the cleanup hook.

The lease covers the hook: a claim lasts the agent timeout, the fifteen-minute grace
and the hook timeout. If the lease nevertheless expires by wall clock during the
hook, the supervised hook is stopped and the worktree preserved for recovery; that is
not a normal hook failure. Cleanup after ownership has already been lost runs without
lease writes.

`UB_AGENT_CLEANUP_CONTEXT` points to a JSON file with `repository`, `run`, `agent`,
`assignment`, `kind` (`issue` or `pr`), `handoff`, `status`, `outcome` (the named
outcome, if any), `worktree` (absolute path) and `branch`. Unavailable fields are
`null`. `status` is the released lease's result during later maintenance, otherwise
the reported status known before removal, or `null`. `handoff` is the PR the run
handed off to. The hook also gets `UB_AGENT_REPOSITORY`,
`UB_AGENT_RUN`, `UB_AGENT_AGENT`, `UB_AGENT_ASSIGNMENT`, `UB_AGENT_WORKTREE` and
`UB_AGENT_BRANCH` (empty when unknown). It gets no reporting lease environment.

Exit zero permits removal. A nonzero exit, start failure or timeout with confirmed
termination keeps the worktree and branch, logs `cleanup-hook-failed` in the run's
`events.jsonl`, and records `cleanup_hook_error` on the lease if ownership remains.
The agent outcome, label transitions and queue remain unchanged. Later maintenance
can retry the hook. Unconfirmed hook termination preserves artifacts and stops the
loop or cleanup command; local diagnostics and, while owned, the lease record
`cleanup: unconfirmed`. Such artifacts remain ineligible until an operator has
established termination and resolved the uncertainty records.

Use the hook for project resources such as test services tied to the run. Document
operator-only cleanup or recovery steps in a project operations document and link
it from `AGENTS.md`; keep those steps out of agent task instructions. Make the hook
safe to retry, because a crash after hook success can leave a worktree to clean later.

## Stale artifact cleanup

`ub-agent cleanup` previews every registered worktree directly under this checkout's
`.ub-agent/worktrees/<run>` and local branch named `ub-agent/<agent>/<number>/<run>`.
It reports `would remove` or `kept` with a reason. Unregistered entries under the
worktree directory are reported as uncertain and kept. Other tools' worktrees,
remote branches and run logs are outside its deletion scope.

`ub-agent cleanup --apply` rechecks each eligible artifact before deletion and
reports `removed` or `kept`. Worktrees are considered before branches, so a branch
can be removed after its worktree. Preview assumes an eligible worktree's hook
succeeds; apply preserves both artifacts if it fails. Locked worktrees and tracked
changes or untracked files that are not ignored keep a worktree. Launcher release
retains its existing removal behavior and always keeps local branches.

Every artifact needs an unambiguous GitHub run lease owned by the authenticated
actor. A released lease is eligible; an expired lease requires a recorded process
group and confirmed absence of its members. Older leases without that record stay
uncertain. Live leases, unreadable state, redirected paths, unconfirmed cleanup and
outcomes awaiting recovery keep artifacts. A later run that continued the same
branch must also be eligible before branch deletion.

Local hook diagnostics are checked for released and expired runs, including when
no hook is currently configured. Each hook directory records its process-group
`pid` and a `stopped` marker after confirmed termination. An unfinished hook's group
must be confirmed absent before a retry or deletion. A missing or unreadable pid,
redirected diagnostics, or an inconclusive process check keeps artifacts. This also
covers a launcher crash between spawning the hook and recording its pid. Operators
must establish termination before resolving uncertain hook records; cleanup never
signals a previous run's processes.

Branches stay when checked out in a kept worktree, used by an open PR, or when the
tip is not known remotely. The command fetches `origin` branch history (including
in preview), and proves tip reachability from those fetched branches or from a
fetched PR head from the same branch and repository. Closed PR heads can preserve
checkpoint commits after a squash merge deletes the remote branch. Fetch/read
failures keep artifacts. No GitHub branches are deleted.

## Queue

```yaml
queue:
  priority:
    labels: [priority:urgent, priority:high, priority:normal, priority:low]
    default: priority:normal
  milestones: gate
  dependencies: wait
```

`priority.labels` is a nonempty list of unique, nonempty label names, highest first.
An item with several configured labels takes the highest. An item with none takes
`priority.default`, which must be one of the configured labels. If `default` is
omitted, unlabeled items rank below every configured label. Without `priority`, all
items have equal priority. Unknown keys in `queue` or `priority` are errors.
The launcher reads priority labels and never changes them.

A PR's effective priority is the highest of its own configured priority and the
effective priority of the open local issues it closes. Closing keywords in its
body (such as `Closes #21`, `Fixes: owner/repo#21` or `Resolves` followed by a local
issue URL) identify those issues; each reference requires a keyword. Closed issues,
PR references and references to other repositories do not contribute. This PR
rule applies in both dependency modes: `ignore` disables issue dependency
inheritance, but PRs still inherit their closing issues' own configured priority.

`milestones` accepts only `gate` or `ignore` and defaults to `ignore`. In `gate`
mode, new issues wait for the oldest open milestone with open issues or PRs to
close or empty; PR work and recovery remain eligible.
In `ignore` mode, planning and claiming do not read milestones. This repository
explicitly sets `gate`.

`dependencies` accepts only `wait` or `ignore` and defaults to `wait`, including
without a `queue` block. In `wait` mode, an issue cannot start preparation or
implementation while any GitHub blocked-by issue is open, including blockers
in other repositories. Closed blockers do not gate it. The gate is rechecked
before claiming. An open local issue inherits the highest effective priority
of its open local dependents, directly or transitively, without changing labels.
Cycles terminate and share the highest reachable priority. With `ignore`, links
affect neither eligibility nor priority, and dependency reads are skipped.
PR work, recovery and completion of started runs remain ungated. With milestone gating, a new issue must pass both gates.
A failed or unreadable dependency read stops selection visibly.
Planning skips link reads only when the issue list's dependency summary reliably
reports zero total blockers; missing or malformed summaries require a full read.
The claim-time recheck always reads the selected new issue's blocker links.

`ub-agent init` writes `queue: {milestones: ignore, dependencies: wait}`. Without a
`queue` block, priorities are unconfigured, milestones are ignored and dependency
waits apply.

Within each work class, priority is followed by item creation time and then item
number. See [selection order](coordination.md#selection-order) for eligibility and
PR precedence. `ub-agent status` and `status --json` use the same rank order and
show each item's effective priority (`none` in text, `null` in JSON when no label
or default applies). Waiting issues name the active milestone number and open
blockers, using `owner/repo#N` for external blockers. Inherited priority names its
origin, for example `priority:urgent (inherited from #21)`. JSON includes
`priority_inherited_from` (the source issue number, or `null`) and `open_blockers`
(a list of issue references). PRs name the issue they close, for example
`priority:urgent (from closed issue #21)`; that wording identifies a closing
reference to an issue that is still open. JSON records it as `priority_from_issue`
(the issue number, or `null`). An item's own priority wins ties; among equally
urgent inherited sources the lowest issue number is shown.

## Agents

Each agent has exactly one of `runtime` or `command`.

| Key | Meaning |
|---|---|
| `trigger` | Label, or list of labels, that selects the agent, subject to maintainer starts and approvals. |
| `outcomes` | Named successful outcomes and their project-defined label transitions. Required. |
| `kind` | `issue`, `pr` or `either` (default). |
| `runtime` | `cli:model:effort` with `codex` or `claude` as the CLI, or a list of alternatives tried in order. |
| `instructions` | The agent's task file. Required with `runtime`. Validated and reread from the refreshed control checkout before each new run. |
| `command` | An argv list to run instead of an LLM session. A relative executable resolves against the configuration's directory. |
| `different-runtime-from` | Another agent's name; requires a PR. When an accepted report identifies that agent's runtime for the current head, this agent must run on a different CLI and model; a different effort doesn't count. Wait for a pending handoff to finish. Without such a report or pending handoff, use only the first configured runtime, blocking if its CLI isn't installed. |
| `worktree` | `true` runs in a private checkout: the PR's exact commit, or a fresh branch for an issue. |
| `runtime-args` | Extra arguments for the runtime CLI, such as permission flags. |
| Limit keys | Override `limits` for this agent. |

Approval enforcement is mandatory for `issue`, `pr` and `either` agents, including
preparation and direct `command` runs. The launcher uses the union of issue/either
triggers for issue starts and pr/either triggers for outside PR starts. No setting
can bypass the [approval rules](approvals.md). Failed checks at pickup or after
claiming park the item, spend no attempt and resume after approval without `retry`.
Context supplies the post-claim title, body and trusted or cleared comments; PRs
also supply their head, reviews and review comments. Other GitHub comments are not
agent input; later outside edits do not stop a running assignment.

Trusted-authored PRs need no start and outside feedback does not suspend them.
Outside PRs need a maintainer trigger and an eligible head: a pinned approval
record, a maintainer approving review on that head, or accepted agent ancestry
from an eligible assigned head in the base repository. Every changed fork head
needs explicit maintainer approval. Outside edits or feedback after approval suspend
outside PRs again. Fork PRs can be reviewed, but agents cannot revise them and
revision runs remain blocked.

Before each new agent run, the launcher fetches `origin` and fast-forwards the
control checkout's default branch, then reloads `ub-agent.yaml` and rereads the
configured instruction files. It replans the claim with the refreshed agent,
triggers, runtime and declared outcomes. If that item no longer plans for that
agent, it is not claimed.
Run `launch` from a clean checkout on that branch with no local-only commits.
An unsafe checkout, failed Git refresh, or missing, outside-project or unreadable
instruction file stops the launcher for the operator to fix and restart; it
charges no attempt and does not mark the assignment blocked or retrying. Refresh
runs only between supervised executions and cleanup hooks, and is skipped for
durable-outcome recovery. Each run keeps its claimed configuration and prompt text.
An invalid reloaded configuration stops with a nonzero exit and the same error as
`ub-agent check`, without charging an assignment attempt.

The launcher does not reload code. See
[Stopping and restarting](../README.md#stopping-and-restarting) for signal handling
and the upgrade recipe.
See [execution boundaries](coordination.md#execution-boundaries) for the full rules.

## Outcomes and transitions

```yaml
agents:
  implementer:
    runtime: "codex:gpt-6.1-sol:high"
    trigger: [ready, needs-changes]
    instructions: .agents/implementer.md
    outcomes:
      handed-off: {add: [needs-review], remove: [old-workflow-state]}
  integrator:
    runtime: "claude:opus:high"
    trigger: ready-to-merge
    instructions: .agents/integrator.md
    outcomes:
      merged: {}
      maintainer-merge: {add: [needs-human]}
```

`outcomes` must be a nonempty mapping of names to transitions. A transition accepts
only `add` and `remove`, each a list of nonempty string labels (empty lists are
allowed). Omitted lists are empty. `check` rejects unknown keys, non-string labels,
adding the agent's own trigger, and removing a stop label, including through an
agent's trigger. Adding a stop label is allowed as a human gate.

An agent reports `ub-agent report --outcome NAME --summary TEXT [--handoff PR]`.
This reports success; an unknown name is rejected. The prompt lists the declarations
and directs the agent to leave workflow labels alone. Direct commands follow the same
contract. The running lease snapshots the declarations; candidate configuration edits
do not change the current run.

After ownership, candidate SHA and issue-link validation, the runner removes all
of this agent's trigger labels and any `remove` labels from the assignment. It adds
`add` labels to the named handoff PR, or to the assignment if none is named. Labels
outside those lists stay unchanged. When the destination is the assignment and a
label appears in both lists, `add` defines its final state. `--status retry|blocked`,
execution failure, execution timeout, execution interruption and invalid success
reports cause no transition.

Before starting, the runner rereads the assignment: a vanished trigger blocks the
transition. A stop label on either the assignment or handoff PR pauses it with no
label changes. The outcome remains unaccepted and is never applied later. After
unpausing, a person sets the desired workflow labels or uses `ub-agent retry` to
rerun the role. A stop label added after transition start stays in place while the
recorded transition completes, parking the item for subsequent pickup.

The outcome records its name, resolved changes and a start marker, so an
interrupted transition is finished from that record by
[recovery](coordination.md#recovery) without rerunning the role or spending an
attempt. An interrupt while reading or finalizing a completed run leaves its lease
live for expiry recovery, preserving the agent's report even when the interrupted
request may already have written the transition start marker.

## Limits

| Key | Default | Meaning |
|---|---|---|
| `agent-timeout-minutes` | 180 | Deadline for one run. A claim's lease lasts this long plus fifteen minutes for setup and completion, plus the cleanup hook timeout when one is configured; the launcher never renews it. A crashed launcher's claim is recoverable only after the lease expires, so projects with long runs may prefer shorter per-agent timeouts. |
| `max-attempts` | 5 | Consecutive failures per item and agent before pickup stops. |
| `retry-backoff-seconds` | 60 | First failure retry delay; doubles with consecutive failures and restarts after success or reset. |
| `max-backoff-seconds` | 3600 | Longest retry delay. |

## Built-in runtimes

The prompt, made of the assignment context and the agent's instructions, arrives on
stdin:

- `codex:MODEL:EFFORT` runs `codex exec --model MODEL --config model_reasoning_effort="EFFORT"`.
- `claude:MODEL:EFFORT` runs `claude --print --output-format stream-json --verbose --model MODEL --effort EFFORT`.

`runtime-args` are appended. They must not change the model or effort, or resume a
session: `check` rejects those flags, because `different-runtime-from` trusts the
recorded `cli:model:effort` and every run starts fresh.
For agents with a Claude runtime, `runtime-args` must not set `--output-format`
(including `--output-format=…`); `check` rejects it because the launcher owns the
stream format. A redundant `--verbose` is accepted.

`process.log` records each CLI's stdout and stderr directly. Codex runs log their
full transcript. Claude runs log the JSON stream of tool calls, tool results and
the final result, including Claude Code's `permission_denials`. The launcher does
not interpret this output; the outcome comes only from `ub-agent report`.

### Runtime permissions

The launcher adds no permission flags, so the CLIs start in their default headless
modes and usually cannot edit files or push. Grant each role what it needs:

```yaml
runtime-args: [--sandbox, danger-full-access]    # codex
runtime-args: [--permission-mode, acceptEdits, --permission-prompts, none,
               --allowedTools, "Bash(git *)", "Bash(gh *)", "Bash(ub-agent *)"]  # claude
```

`init` includes the matching example, commented out, for every starter agent using
the CLI selected by `--runtime`. Uncomment or customize it before unattended work.
The Codex example grants full access, including writes to Git metadata for commits
and commands for pushing. The Claude example grants file edits and the listed
Git, GitHub and report commands without permission prompts. Add permissions for
your project's check commands as needed. `doctor` warns, without failing, for each
runtime agent with no `runtime-args`; it does not test whether supplied arguments
grant sufficient permissions.

`runtime-args` apply to every alternative in a runtime list, so use CLI-specific flags
only on agents with a single runtime. Codex's `workspace-write` sandbox cannot commit in
private worktrees, whose Git metadata lives in the main checkout.

## Commands instead of agents

```yaml
agents:
  investigate:
    command: [./scripts/investigate-issue]
    kind: issue
    trigger: investigate
    outcomes:
      investigated: {}
    agent-timeout-minutes: 20
```

The command records its result with `ub-agent report --outcome investigated --summary
TEXT`, exactly like an LLM runtime, and receives the same environment variables:

| Variable | Value |
|---|---|
| `UB_AGENT_CONTEXT` | Path to a JSON file describing the assignment |
| `UB_AGENT_REPOSITORY` | `owner/name` |
| `UB_AGENT_ASSIGNMENT` | Issue or PR number |
| `UB_AGENT_RUN` | Run id |
| `UB_AGENT_LEASE_ID` | The claim's comment id |
| `UB_AGENT_CANDIDATE_SHA` | The PR's head commit; empty for issue work |
| `UB_AGENT_BRANCH` | The branch to work on, when known |

## Commands

- `ub-agent init [--repository owner/name] [--runtime cli:model:effort]` writes the
  starter `ub-agent.yaml`, shared `AGENTS.md` and `.agents/` files next to the selected
  `--config` file, and adds `.ub-agent/` to `.gitignore`. Fill in the shared guidance's
  project-check placeholders. An existing `AGENTS.md` is kept unchanged and reported;
  any existing configuration or role starter file stops init before any file writes.
  In an interactive terminal, init reads repository labels and explains each missing
  trigger, outcome `add`/`remove`, and stop label. It creates only those labels after
  an explicit `y` or `yes`; the default is no. Declining makes no GitHub writes and
  prints one runnable `gh label create` command per missing label. Without a terminal
  (including CI and piped input), or when labels cannot be read, it prints commands
  for all configured workflow labels and explains why. Noninteractive init makes no
  GitHub reads or writes beyond repository inference when `--repository` is omitted.
  It never changes or deletes existing labels or uses `--force`. Local starter files
  are written regardless of the label-creation answer. Each agent includes commented
  permission arguments matching `--runtime`; see [Runtime permissions](#runtime-permissions).
- `ub-agent check` validates the configuration and instruction files.
- `ub-agent doctor [--json]` reports remaining GitHub requests and the reset time in
  UTC from real request headers. It warns below 10% remaining and whenever doctor
  itself is rate limited. It checks everything `check` does, plus Python, the platform,
  `git`, `gh`, GitHub access, configured workflow labels, runtimes and local state.
  A token that cannot change labels is a required failure, because the launcher
  applies outcome transitions itself. Missing trigger or outcome transition labels
  are required failures naming their agents; missing stop labels are warnings. Both give a `gh label create` remedy.
  Label matching is case-insensitive and an unreadable label list is a required
  failure. The non-required `github-launcher-role` check warns when the launcher
  account has `maintain` or `admin`, because agents could start and approve their
  own work, or when its role cannot be read. Use a dedicated account with `write`;
  see [Issue approvals](approvals.md#repository-roles). Runtime agents without
  `runtime-args` produce a warning linking the permission guidance. Doctor makes no
  writes. It exits 1 when a required check fails; warnings and skips exit 0. The
  JSON has `version`, `ok` and `checks`,
  and each check has `id`, `status`, `required`, `agent`, `runtime`, `message` and
  `remedy`.
- `ub-agent approve --number N` prints the current issue or PR title, body and
  outside comments; for PRs it also prints the head, outside reviews and review
  comments. It posts one [approval record](approvals.md#approving-current-input),
  pinning a PR head and recording the feedback it clears. It requires the
  authenticated `gh` account to have the `maintain` or `admin` repository role and
  refuses input changed during display. Running the command expresses approval
  without an interactive confirmation; it changes no labels and does not replace
  the required maintainer start.
- `ub-agent launch [--once]` runs the loop in the foreground.
- `ub-agent cleanup [--apply]` previews stale owned artifacts; `--apply` rechecks and
  removes eligible worktrees and local branches, running the project hook first.
- `ub-agent status [--json]` shows matching work, claims, consecutive failures in the `attempts` field, and outcomes.
  Each agent's `reported:` (JSON `outcome`) describes that agent's live lease's run,
  or its latest lease's run when it has no live lease. Recovery leases show the
  original run's report. If that run has not reported, `reported:` is omitted and
  JSON `outcome` is `null`. The JSON `lease` field shows the item's live owner, even
  when it is another agent.
- `ub-agent report --outcome NAME --summary TEXT [--handoff PR]` records a declared
  successful outcome. Use `--status retry|blocked` for failures. It works only inside
  a supervised run.
- `ub-agent retry --number N --agent NAME --reason TEXT` resets one agent's consecutive failure count on
  an item once you have fixed the cause.
- `--config PATH` selects a different configuration file.
