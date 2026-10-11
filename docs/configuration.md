# Configuration reference

`ub-agents.yaml` sits at the root of the repository the agents work on. Unknown keys are
errors; `ub-agents check` validates the file. Commands use this file by default;
`--config PATH` selects another configuration file before or after the command.
`init` writes the selected file or `ub-agents.yaml` by default.
If the selected file is missing, `check`, `status`, `launch`, `cleanup`, `retry`,
`approve` and unsupervised `read` exit 1 with a message naming `ub-agents init`.
An explicit `--config PATH` is named in the message. `launch` creates no
`.ub-agents/` or `launch.log` without configuration; `doctor` keeps its diagnostic report.

`ub-agents read N` prints an open or closed issue or PR as filtered JSON under
these input policies; see [reading other items](approvals.md#reading-other-issues-and-prs).
Inside a run use the launcher's literal `report_command` followed by `read N`.
The launcher pins its repository and input configuration; worktree edits and
`--config` do not override them for a supervised read.

## Top level

| Key | Meaning |
|---|---|
| `repository` | GitHub `owner/name`. It must match the checkout's `origin`. |
| `launchers` | Optional nonempty list of GitHub logins that narrows coordination trust; every account still needs `write` or higher. |
| `approvals` | `on` or `off` (quoted or unquoted); defaults from GitHub visibility each pass: `on` for public, `off` for private and internal repositories. See [approvals](approvals.md). |
| `trusted-bots` | Optional list of GitHub bot account logins, default `[]`; case-insensitive, ignores `[bot]` suffixes, bots only, trusted for feedback in assignment context and `read`, with no maintainer or launcher authority. See [trusted bots](approvals.md#trusted-bots). |
| `agents` | The agents, by name. |
| `shared-instructions` | Optional project policy file, relative to the configuration file and inside the project; read before every role's instructions. |
| `limits` | Default clocks and retry limits for every agent. |
| `poll-seconds` | Minimum gap between discovery-pass starts, except immediately after running or recovering work (default 30 seconds). |
| `stop-labels` | Labels that park an item (default `[needs-human]`). |
| `cleanup` | Optional project cleanup hook and timeout, run before private worktree removal. |
| `checkout-setup` | Optional setup command, run after watched control-checkout files change and when creating private worktrees. |
| `runtime-updates` | Optional daily maintenance policy for Claude Code, Codex and the launcher's GitHub CLI (`gh`). |
| `runtime-args` | Optional mapping from CLI (`codex`, `claude`) to default argument lists, inherited by runtime agents that omit their own `runtime-args`. |
| `queue` | Priority ranking, dependency waits and optional milestone selection (defaults to FIFO, waiting for blockers, with milestones ignored). |

`shared-instructions: .agents/ub_agents.md` supplies policy to every run between the
[launcher contract](coordination.md#run-prompt) and its role instructions. It is
optional; configurations without it need no changes. Like role instruction files,
it must exist, remain inside the project (including symlink targets), and be readable
UTF-8. `check`, `doctor` and the pre-run reload validate it; invalid files stop the
launcher before a claim or charged attempt. Text comes from the refreshed control
checkout and stays fixed for the run, even if a candidate edits the policy.

Project build commands and conventions belong in the repository's own `AGENTS.md`,
`CLAUDE.md` or `.claude/CLAUDE.md`. The starter policy's Checks section points to
an existing file, preferring one the configured runtime loads; placeholders appear
only when none exists. Codex loads `AGENTS.md`. Claude prefers `CLAUDE.md`, then
`.claude/CLAUDE.md`, then `AGENTS.md`. If only Claude guidance exists for Codex,
`init` suggests an `AGENTS.md` containing `Read CLAUDE.md` or `Read .claude/CLAUDE.md`.
Loop checks, merge policy, decision authority and review focus belong in the shared
policy; role procedures belong in role files.
Stop-report formatting, input trust, reporting and workflow-label ownership come
from the launcher prompt and need no project copies.

With `approvals: off`, current titles and bodies are input, trigger labels need no
maintainer start, and PR heads need no approval. Feedback is limited to authors
with `write`, `maintain` or `admin`; approval records cannot clear other feedback.
The launcher makes no approval reads or approval-parking writes. When `approvals`
is unset, an unreadable visibility fails the pass before any claim or parking
write. `doctor --verbose` shows the effective value and source; `check` shows the
configured value or that it comes from visibility, without contacting GitHub.

`poll-seconds` defaults to 30 and sets the minimum gap between continuous
discovery-pass starts, including observations during assignments. After running
or recovering work, the next claiming pass starts immediately. Idle and observation
passes may wait longer for the shared discovery budget or low GitHub quota;
rate-limit waits and runtime usage pauses still apply. `launch --once` and
`launch N` never wait after their pass. This setting does not change agent retries.

```yaml
poll-seconds: 30
```

For the detailed rules previously covered here, see
[Polling and GitHub requests](polling.md), including its fixed
[retry limits](polling.md#transient-failures) and
[rate-limit waits](polling.md#rate-limit-waits).
Launch logs are described in [Running launchers](operations.md#launch-output).
Local session files have no configuration key; their format and lifecycle are in
the [session snapshot contract](session-snapshots.md).

## Launcher accounts

By default, coordination records from any account with current `write`, `maintain`
or `admin` repository access count. Launchers can use their engineers' own `gh`
logins and share a queue. Optional `launchers` narrows trust to a nonempty list:

```yaml
launchers: [bot-a, alice]
```

Logins match case-insensitively. `check` rejects a value that is not a list, is
empty, contains non-string or blank logins, or repeats a login (including with
different case). Listed accounts still need `write` or higher. Unlisted authors
are ignored without a role read for coordination. Any trusted account, including
humans, can post coordination records; they can already change labels and push,
so this adds little authority. The record's actor is always the comment author.

Use the same list on every machine. Roles are checked at read time; demotion or
removal from the list revokes all that account's records, including open leases.
Stop that account's launcher first. A failed author role read stops the pass;
only a definite role below `write` excludes a record. A launcher whose own account
is below `write` or unlisted claims nothing and prints the reason. `doctor` warns
about each listed account below `write` or with an unreadable role, and about an
unlisted authenticated account.

**Upgrading:** older launchers trust only their own account. Stop all launchers
and upgrade them together before mixing accounts. Omitting `launchers` requires
no configuration change for an existing single-account setup with write access.

## Checkout setup

```yaml
checkout-setup:
  command: [sh, -c, 'mise trust ./mise.toml && exec mise run install']
  when-changed: [pnpm-lock.yaml, mise.toml]
  timeout-seconds: 600
```

This optional project hook runs in the Python launcher, before the agent starts.
Without `checkout-setup`, no setup command runs. ub-agents does not require mise:
choose the command for your project, such as `[npm, ci]` for an npm project.

The mise example trusts only this checkout's `mise.toml` before installing.
Every new worktree has a new path, so trusting the control checkout alone is not
enough. Agent runtime command permissions do not apply to this hook. The explicit
`sh -c` runs the two steps in order; if trust fails, install does not run.

Use this to reinstall the control checkout's dependencies when a launcher pulls
a lockfile or tool configuration change, and install dependencies in new private
worktrees.
`command` is a nonempty argv list, executed directly in the checkout being set up; name a shell explicitly when needed.
`when-changed` is a nonempty list of literal repository-relative file paths;
absolute paths, directories and `..` components are rejected. Paths can name files added or
deleted by the pull. `timeout-seconds` is a positive number, defaults to 600,
and cannot exceed 3600. Unknown keys are errors.

In the control checkout, before each new run, after fetching, fast-forwarding and reloading configuration,
the launcher compares these files with the last commit where setup succeeded.
Until the first successful setup, it uses the HEAD before the fast-forward.
That baseline is saved before the fast-forward, so stopping launch or failing
configuration reload cannot lose a watched-file change before setup runs.
If any watched file differs, the command runs once before claiming a role.
An unrelated change or a refresh with nothing to pull skips setup, unless a
previous setup failed. A commit can add this setting and a lockfile change together.
This does not detect pulls made by hand outside the launcher.

The terminal view and plain output name the file that triggered setup. Command
output goes only to the reported log file. Setup records and logs live outside
the checkout, under the user's state directory (`$XDG_STATE_HOME/ub-agents/checkouts/`,
or `~/.local/state/ub-agents/checkouts/`), keyed by the control checkout's path.
The command should keep tracked files unchanged and use ignored paths for installed
dependencies so the next refresh still has a clean checkout.

A nonzero exit, start failure, timeout or interruption in control-checkout setup stops launch before a claim,
without spending an attempt or marking the item blocked or retrying. The message
names the cause, log and next step. Failed setup remains pending and runs again on
the next launch, even with nothing to pull, until it succeeds. See
[operations](operations.md#when-things-go-wrong) for recovery.

For an agent with `worktree: true`, the launcher also runs the command once in
each private worktree immediately after creating it, before the agent starts.
`when-changed` applies only to the control checkout; a new worktree always needs
its own dependencies. Shared-checkout agents run no additional setup command.
Private setup uses the same environment and timeout as control-checkout setup.
The terminal view and plain output say setup is running in the worktree; its
output goes to `checkout-setup/process.log` in the run's log directory, separately
from the agent's `process.log`.

For a PR reviewer, setup installs the PR head's lockfile, so its install scripts
run before the agent does; the agent would run them anyway to test the PR.
A private-worktree setup failure counts toward the item's retry limit. Fix the hook or environment before using `ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"` to reset exhausted attempts.

A failure ends the run before the agent starts, cleans up the private worktree,
and reports the cause and log as a workspace-preparation failure. Interruption
terminates setup like an interrupted agent. Timeout and interruption terminate
and confirm the whole process group; crash recovery requires confirmed termination
before releasing the item or removing the worktree. The item's stop labels, open
state, trigger labels and candidate head are checked again after setup finishes.

**Upgrading:** Upgrade every launcher of a project before adding `checkout-setup`
to its `ub-agents.yaml`; older versions reject the unknown key.

## Project cleanup hook

```yaml
cleanup:
  command: [./scripts/cleanup-agent-worktree]
  timeout-seconds: 60
```

Without `cleanup`, no hook runs. `command` must be a nonempty argv list; no shell
or substitutions are used. The timeout defaults to 60 seconds and must be finite,
positive and at most 3600 seconds. `ub-agents check` rejects unknown keys and invalid
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

The fixed 30-minute lease renews every 10 minutes throughout the hook; the hook
timeout does not extend it. If the last confirmed expiry passes by wall clock during the
hook, the supervised hook is stopped and the worktree preserved for recovery; that is
not a normal hook failure. Cleanup after ownership has already been lost runs without
lease writes.

`UB_AGENTS_CLEANUP_CONTEXT` points to a JSON file with `repository`, `run`, `agent`,
`assignment`, `kind` (`issue` or `pr`), `handoff`, `status`, `outcome` (the named
outcome, if any), `worktree` (absolute path) and `branch`. Unavailable fields are
`null`. `status` is the released lease's result during later maintenance, otherwise
the reported status known before removal, or `null`. `handoff` is the PR the run
handed off to. The hook also gets `UB_AGENTS_REPOSITORY`,
`UB_AGENTS_RUN`, `UB_AGENTS_AGENT`, `UB_AGENTS_ASSIGNMENT`, `UB_AGENTS_WORKTREE` and
`UB_AGENTS_BRANCH` (empty when unknown). It gets no reporting lease environment.

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

`ub-agents cleanup` previews every registered worktree directly under this checkout's
`.ub-agents/worktrees/<run>` and local branch named `ub-agents/<agent>/<number>/<run>`.

The preview reports `would remove` or `kept` with a reason. Unregistered entries under the
worktree directory are reported as uncertain and kept. Other tools' worktrees,
remote branches and run logs are outside its deletion scope.

`ub-agents cleanup --apply` rechecks each eligible artifact before deletion and
reports `removed` or `kept`. Worktrees are considered before branches, so a branch
can be removed after its worktree. Preview assumes an eligible worktree's hook
succeeds; apply preserves both artifacts if it fails. Locked worktrees and tracked
changes or untracked files that are not ignored keep a worktree. Launcher release
retains its existing removal behavior and always keeps local branches.

Every artifact needs an unambiguous GitHub run lease from a trusted account with
this machine's recorded hostname. Leases from another host or with no recorded
host are refused, even if posted by the authenticated account. A released lease
is eligible; an expired lease requires a recorded process
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
  milestones: order
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

`milestones` accepts three values: `gate`, `order` or `ignore`, and defaults to `ignore`.

In `order` mode, new issues of equal effective priority rank by open milestones
with open issues or PRs, oldest first by creation time and then milestone number.
Within one priority, issues without a milestone, or with a milestone outside that
list (such as a closed milestone), rank after all listed milestones. Item creation
time and item number break ties. Higher priority in a later or no milestone wins
against lower priority in an earlier milestone. Milestones never hold back an
otherwise eligible issue; later and unmilestoned work can start when earlier work
cannot. PR work, owned runs and recovery precede new issue starts only at equal
effective priority, and their rank ignores milestones.

In `gate` mode, new issues rank by milestone first, then effective priority,
creation time and number within that milestone. It uses the same milestone list
as `order`, choosing the earliest milestone with an issue eligible for this
launcher. Blocked, owned, stop-labelled, untriggered, retry-limited, approval-parked
or runtime-unavailable work, including work outside the selected role, does not
hold back independent later work. Every pass reconsiders earlier work;
no cursor advances or milestone changes state.
On a cold claiming pass, `gate` first plans all candidates from discovery's labels,
triggers, leases, blockers, milestones and runtime availability. Candidates that
cannot be claimed for those reasons need no approval-input reads. It then reads
approval input as selection reaches each ready plan, stopping at the first claim.
The selected milestone's next approved issue is checked before comparing unlisted
issues or existing work against it, so that comparison can require a verdict on
an issue that is not claimed. Denied plans are approval-parked only when reached;
later passes reach the remaining plans. Status and queue-view observation passes
still evaluate every row, including approval parking reasons.
Observation passes retain the shared discovery-budget accounting described above.
Their cold reads can delay a later idle pass beyond `poll-seconds`; claiming
immediately after work still bypasses budget admission. Exhaustive observations
and claiming passes share the same REST quota reservation.
An eligible unmilestoned issue, including one whose milestone is outside the list,
goes ahead only if its effective priority is **strictly higher** than the selected
milestone's next eligible issue. Milestone work wins ties. Later assigned milestones
get no priority exception. When no listed milestone has eligible work here,
unmilestoned issues run in priority order even while milestones remain open.
For example, a high-priority unmilestoned issue precedes ten medium-priority issues
in the selected milestone; a medium-priority unmilestoned issue and a high-priority
issue in a later milestone wait behind those ten.
PR work, owned runs and recovery keep priority order, ignoring milestones, and
precede the next selected new issue at equal or higher effective priority.

Both `order` and `gate` use the milestone list and each listed issue's milestone;
an unreadable milestone list or item stops selection visibly, never justifying
fallback. Neither needs a claim-time or approval-parking milestone recheck;
fresh-plan, dependency and claim election checks still apply. In `ignore` mode,
planning and claiming do not read milestones. This repository explicitly sets `order`.
Explicit `launch N` and `status N` skip queue priority and milestone selection,
while applying every other gate. A numbered launch can start later-milestone or
unmilestoned work.

**Upgrading:** projects using `gate` now start later-milestone work while earlier
milestone issues are not eligible. To keep work in milestone order, give every
open issue in the active milestone a trigger label, or move it out.

`dependencies` accepts only `wait` or `ignore` and defaults to `wait`, including
without a `queue` block. In `wait` mode, an issue cannot start preparation or
implementation while any GitHub blocked-by issue is open, including blockers
in other repositories. Closed blockers do not gate it. The gate is rechecked
before claiming. An open local issue inherits the highest effective priority
of its open local dependents, directly or transitively, without changing labels.
In milestone `order` and `gate` modes, blockers also inherit the earliest
milestone from open local dependents, directly or transitively, even without
configured priority labels. Priority and milestone inheritance choose their
sources independently.
Cycles terminate and share the highest reachable priority and earliest milestone.
With `ignore`, links affect neither eligibility nor priority or milestone rank,
and dependency reads are skipped. PR work, recovery and completion of started
runs remain ungated.
A failed or unreadable dependency read stops selection visibly.
Planning skips link reads only when the issue list's dependency summary reliably
reports zero total blockers; missing or malformed summaries require a full read.
The claim-time recheck always reads the selected new issue's blocker links.

`ub-agents init` writes `queue: {milestones: ignore, dependencies: wait}`. Without a
`queue` block, priorities are unconfigured, milestones are ignored and dependency
waits apply.

Except for the new-issue policy in `gate`, rank is effective priority, then
existing work (PRs, owned runs and recovery) before new issue starts, then
milestone rank for new issues in `order` mode, then item creation time and item
number. Without configured priorities, all items have equal priority, so existing
work still goes first. See
[selection order](coordination.md#selection-order) for eligibility and
PR precedence. `ub-agents status` and `status --json` use the same rank order and
show each item's effective priority (`none` in text, `null` in JSON when no label
or default applies). In `order` and `gate` modes, each issue also shows its
effective milestone next to priority (`none` when unmilestoned), with the source
when inherited, such as `milestone #2 (inherited from #21)`. JSON includes
`milestone` (the effective milestone number, or `null`) and
`milestone_inherited_from` (the source issue number,
or `null`). Ready new issues are listed in the order this launcher would start
them. When `gate` passes over earlier milestones, status, the terminal view and
`launch.log` explain why, for example `Milestone #12 has no eligible issues for this launcher`.
Status JSON includes these explanations in `milestone_skips` when nonempty.
Waiting issues name open blockers,
using `owner/repo#N` for external blockers. Inherited priority names its origin,
for example `priority:urgent (inherited from #21)`. JSON includes
`priority_inherited_from` (the source issue number, or `null`) and `open_blockers`
(a list of issue references). PRs name the issue they close, for example
`priority:urgent (from closed issue #21)`; that wording identifies a closing
reference to an issue that is still open. JSON records it as `priority_from_issue`
(the issue number, or `null`). An item's own priority or milestone wins ties;
among equally ranked inherited sources the lowest issue number is shown.

## Agents

Each agent has exactly one of `runtime` or `command`.

| Key | Meaning |
|---|---|
| `trigger` | Label, or list of labels, that selects the agent, subject to maintainer starts and approvals. |
| `outcomes` | Named successful outcomes and their project-defined label transitions. Required. |
| `retrospectives` | Optional positive integer discussion number in `repository`; the agent's retrospective board. `check` validates the value offline; `doctor` checks that the discussion exists in that repository. |
| `kind` | `issue`, `pr` or `either` (default). |
| `runtime` | `cli:model:effort` with `codex` or `claude` as the CLI, or a list of alternatives tried in order. |
| `instructions` | The agent's task file. Required with `runtime`. Validated and reread from the refreshed control checkout before each new run. |
| `command` | An argv list to run instead of an LLM session. A relative executable resolves against the configuration's directory. |
| `health-check` | Optional nonempty argv list to check a project dependency before claiming new work for this agent. |
| `different-runtime-from` | Another agent's name; requires a PR. When an accepted report identifies that agent's runtime for the current head, this agent must run on a different CLI and model; a different effort doesn't count. Wait for a pending handoff to finish. Without such a report or pending handoff, use only the first configured runtime, blocking if its CLI isn't installed. |
| `worktree` | `true` runs in a private checkout: the PR's exact commit, or a fresh branch for an issue. |
| `runtime-args` | Replaces all top-level runtime argument defaults: a list shared by all alternatives, or a mapping from listed CLI (`codex`, `claude`) to a list. Omit to inherit; `[]` or `{}` disables the defaults. |
| Limit keys | Override `limits` for this agent. |

### Project health checks

An agent can wait for a project dependency, such as a corpus MCP server, before
claiming work:

```yaml
agents:
  issue-preparer:
    health-check: [./scripts/check-corpus]
    # runtime, trigger, instructions and outcomes as usual
```

The launcher runs this command without a shell in the control checkout, only when
the agent has otherwise-ready work and before claiming it or writing new assignment
records. Relative executables resolve like an agent `command`. The project decides
what to check; ub-agents only runs the configured command. The timeout is fixed at
60 seconds. A successful check is reused for up to five minutes within the launcher;
a failure is checked again on the next poll with ready work for that agent.

A nonzero exit, start failure or timeout makes the agent's ready rows `waiting`,
naming the command and the last nonempty stderr line, falling back to stdout or
the start or timeout error. The launcher emits one line when the check starts
failing or its error line changes, and one when it passes again; launch output
also goes to `.ub-agents/launch.log`. Waiting rows appear in the terminal view's
Eligible section, outside Needs attention. There are no claims, attempts, label
changes or item comments for new work while the check fails. Other agents keep
claiming, and `launch --once` exits normally.

`ub-agents status` runs the check itself, including without a launcher on the same
machine. When the check passes, claiming resumes automatically without `retry`.
Checks do not interrupt active runs or prevent recovery of recorded outcomes, and
they do not reset previously parked items. `check` validates the argv list without
running it; `doctor` does not run it either. These project checks are separate from
[daily runtime maintenance](#daily-runtime-maintenance).

**Upgrading:** Upgrade every launcher of a project before adding `health-check`;
older versions reject the unknown agent key.

### Retrospectives

Set `retrospectives: 203` on an agent to enable its supervised posting command:

```sh
<report_command> retrospective --body-file PATH
```

Use the launcher's literal `report_command` from the assignment context, with a
readable, nonempty UTF-8 body file in the run's scratch directory. The command
posts a top-level comment and prints its URL. It resolves the configured discussion
by number and refuses to post unless its URL is exactly
`https://github.com/<repository>/discussions/<number>`. It works only inside a
supervised run and takes the repository and the agent's board from the launcher's
pinned `run.json`; worktree edits and `--config` cannot change the target. There are no
repository, number or id options. Missing context, an unconfigured board, an
unreadable or whitespace-only body, a URL mismatch or a failed GitHub call exits
nonzero. Success and failure write no coordination records, labels or outcome.

Only agents with this key receive the retrospective guidance and literal command.
A loss paragraph needs a real loss (an extra run or review round, rework, or about
fifteen minutes on a denied command, a long search or a missing pointer), or
something the run needed but missed, and a change that would have prevented it.
It states the cost, cause and smallest useful change in one short paragraph.

A post may consist of, or end with, at most two optional topic lines, even when the
run lost nothing:

```text
Would have helped: topic A, topic B
Suggest creating: topic C, topic D
```

`Would have helped` names existing guidance, records or pointers the run lacked or
had to reconstruct. `Suggest creating` names lasting choices no record covers,
left for later human review instead of being recorded during the run, subject to
project rules. Omit each empty line. Use short comma-separated topic names and
reuse a name from an existing record or earlier post when it fits. Include no
per-topic rationale, scores, identifiers or other structure; link the item, which
holds the evidence. No post is required on every run. Post at most once per item,
without repeating an earlier run's post. Public posts must contain no credentials,
environment values, local paths, hostnames or log excerpts. Write the body with a
file-writing tool in the run's scratch directory and post before reporting; a
failed post blocks nothing.

A person groups topic lines over several weeks and reads the linked items for
rationale before deciding which records to create. Repetition is evidence, not a
creation threshold: one consequential compatibility or security choice can merit
a record. Records are never created automatically; this route uses no scoring,
analytics service or scheduler.

A project's own rules decide when a run creates a record. They can defer optional
new records to topic lines, so a missing proposed record alone does not block
completion. Binding decisions, behavior guarantees and human-approval gates stay
in force; this framework guidance does not override project rules.

### Pickup and refresh

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

Before its first pass, `launch` checks that the control checkout is clean, on the
repository's default branch (not detached), and has no commits outside the local
`origin` ref for that branch. Missing origin refs require `git fetch origin`.
`doctor` uses the same read-only check but reports checkout problems as warnings,
so setup on a branch does not fail; it never fetches or fast-forwards.
`launch` also refuses startup when a configured trigger or transition label is
missing on GitHub, pointing to `ub-agents doctor` for label creation commands.
Stop-only labels remain optional at startup.

Before each new agent run, the launcher fetches `origin` and fast-forwards the
control checkout's default branch, then reloads `ub-agents.yaml` and rereads the
configured role and shared instruction files. It replans the claim with the refreshed agent,
triggers, runtime and declared outcomes. If that item no longer plans for that
agent, it is not claimed.
Run `launch` from a clean checkout on that branch with no local-only commits.
An unsafe checkout, failed Git refresh, or missing, outside-project or unreadable
instruction file stops the launcher for the operator to fix and restart; it
charges no attempt and does not mark the assignment blocked or retrying. Refresh
runs only between supervised executions and cleanup hooks, and is skipped for
durable-outcome recovery. Each run keeps its claimed configuration and prompt text.
An invalid reloaded configuration stops with a nonzero exit and the same error as
`ub-agents check`, without charging an assignment attempt.

The launcher does not reload code. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signal handling
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
    runtime: "claude:claude-opus-5-5:high"
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

An agent reports `ub-agents report --outcome NAME --summary TEXT [--handoff PR] [--action TEXT] [--option TEXT]`.
Here and in project instructions, replace `ub-agents` with the launcher's literal
`report_command` from the assignment context (also supplied as `UB_AGENTS_REPORT`).
It runs a copy of the launcher's code taken at startup, using the launcher's
interpreter, regardless of PATH, the working directory or `PYTHONPATH`, for both
console-script and `python -m ub_agents` launchers. The copy lives in the launcher's
user state directory outside the checkout; refreshes and package upgrades do not
change in-run commands or Python helpers. Shared locks retain it while users run;
unused copies are removed on exit or on a later launcher start after a killed user.
The prompt writes this command literally; agents need no shell-variable expansion
and must not report through their worktree's development copy.
This reports success; an unknown name is rejected. The prompt lists the declarations
and directs the agent to leave workflow labels alone. Direct commands follow the same
contract. The running lease snapshots the declarations; candidate configuration edits
do not change the current run.

Stop reports are `--status blocked` and named outcomes whose lease declaration adds
a configured stop label. They require at least one `--action "TEXT"` or `--option "TEXT"`.
Repeat actions for independent asks that are all needed; repeat options for
alternative ways to clear one blocker, with the recommendation first. Each value
is one non-empty line of at most 300 characters (8000 total across both),
understandable on its own. Name who can act, the step and any essential consequence.
Single-backtick inline code is preserved; end an option with a colon, a space and
a single-backtick command to show a separate code block. The
[coordination contract](coordination.md) describes the compatible record fields.
Keep full Markdown reasoning, evidence, diagnostics and links in `--summary`. For example:

```sh
ub-agents report --outcome needs-human --summary "Preparation blocked by storage policy." --option "Owner: choose local storage for privacy." --option "Owner: choose cloud storage for sharing."
```

Missing or invalid asks and options are refused before any write. The launcher also rejects
reports that bypass this validation for new leases; historical outcome records
remain readable. `--status retry` and outcomes adding no stop label need no action.
Repositories with their own role files or command runtimes that file stop reports
must supply an action or option. Notices show asks in bold and alternatives as
numbered options with the first recommended. Resume instructions stay visible;
reasoning and evidence are collapsed by default. Legacy records
remain readable and use a concise review request when no action was recorded.
The terminal view's attention reason uses the summary's first sentence for options
notices and the recorded asks for notices without options.

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
unpausing, a person sets the desired workflow labels or uses `ub-agents retry` to
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
| `agent-timeout-minutes` | 180 | Deadline for agent execution only. Claims last 30 minutes and renew every 10 minutes throughout setup, execution, completion and cleanup, including recovery and rate-limit waits. Lease duration and renewal interval are fixed. Older claims retain their recorded expiry. |
| `max-attempts` | 5 | Consecutive failures per item and agent before pickup stops. |
| `retry-backoff-seconds` | 60 | First failure retry delay; doubles with consecutive failures and restarts after success or reset. |
| `max-backoff-seconds` | 3600 | Longest retry delay. |

## Built-in runtimes

The [run prompt](coordination.md#run-prompt), made of the launcher contract, assignment
context, optional shared policy and role instructions, arrives on stdin:

- `codex:MODEL:EFFORT` runs `codex exec --json --model MODEL --config model_reasoning_effort="EFFORT"`.
- `claude:MODEL:EFFORT` runs `claude --print --output-format stream-json --verbose --model MODEL --effort EFFORT`.

Top-level `runtime-args` accepts a mapping from CLI (`codex`, `claude`) to a list
of strings. Runtime agents inherit it when they omit their own `runtime-args`;
command agents do not inherit it. Each run appends only its selected CLI's list,
including when a later run switches alternatives. A missing or empty CLI entry
adds no arguments. `check` validates every top-level entry, even for CLIs no agent
currently uses, and rejects unknown CLI keys.

An agent's `runtime-args` accepts a list applying to every alternative, or a
per-CLI mapping. Either form replaces the entire top-level mapping, without
extending it or falling back to it for omitted CLI keys. An explicit `[]` or `{}`
disables inheritance. Agent mapping keys must name a known CLI present in that
agent's runtime list. `check` names the agent and key for an invalid entry.

The selected arguments are appended, with `{scratch}` replaced by the run's
absolute scratch path and `{report_command}` by the launcher's absolute report command
(see [Runtime permissions](#runtime-permissions)). They must not change the model
or effort, or resume a session: `check` rejects those flags, because
`different-runtime-from` trusts the recorded `cli:model:effort` and every run starts fresh.
`check` validates each mapping entry with that CLI's rules; a shared list must
satisfy every listed CLI's rules. Claude arguments must not set `-c` (its session
continuation flag) or `--output-format` (including `--output-format=…`), because
the launcher starts fresh sessions and owns the stream format. A redundant
`--verbose` is accepted.
Codex `runtime-args` must not set `--ephemeral`: the launcher may need the fresh
session's limit signal when it is absent from the JSON stream. A redundant `--json`
is accepted.
Codex's `-c` is accepted for configuration settings other than model, effort or
session overrides, even when the agent also lists Claude, if placed in the
`codex` mapping entry. Both forms use the same placeholder validation.

`process.log` records each CLI's stdout and stderr directly. Codex runs log their
JSON event stream. Claude runs log the JSON stream of tool calls, tool results and
the final result, including Claude Code's `permission_denials`. The launcher does
interpret structured usage metadata; an accepted outcome still comes only from
`ub-agents report`.

### Runtime usage pauses

A launcher pauses new runs on one CLI (`claude` or `codex`), across all models
and efforts, only when one of its runs reports a usage limit. It holds the pause
in memory; restarting the launcher clears every pause. Other launchers keep their
own independent pauses, and the current run keeps running.

Claude's rejected `rate_limit_event`, `error: rate_limit`, and `api_error_status: 429`
identify limits. Codex's limit-reached snapshots and structured
`usage_limit_exceeded` errors identify limits. The launcher takes only reset times
from this metadata, ignoring usage percentages. If Codex's `--json` stream ends
without a limit signal, the launcher may read the run's own session's `event_msg`
metadata, identified by `thread.started`, from
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-THREAD_ID.jsonl` (default
`~/.codex/sessions/`). It never resumes a session or reads another run's session.

A pause ends at the reported reset plus a fixed one-minute margin. Missing,
unreadable, non-future resets, or resets more than seven days away, instead pause for
15 minutes from the limit report. A later limit report for the same CLI replaces
its end time. When a Codex snapshot supplies multiple reset times, the latest is used.
A run that ends with a usage limit and no accepted outcome releases as `retry`,
leaving attempts unchanged and adding no retry backoff. An accepted outcome is
preserved even when the run reports a limit.

Runtime alternatives are tried in order, skipping paused CLIs while retaining
`different-runtime-from` rules. An item whose eligible runtimes are all paused
shows `waiting`, without label changes or attempts. Other CLIs and their runtime
alternatives keep working. Continuous launch keeps polling even when every CLI is
paused; ordinary, idle, and empty-poll waits wake by the earliest CLI pause expiry.
Signals interrupt these waits normally. `launch --once` still observes just once.

When a pause starts or its end time changes, one line in launch output names the
CLI and its UTC end time, for example:
`claude usage limit reached; pausing claude runs until 2026-10-04T12:01:00Z`.
The line is also written to `.ub-agents/launch.log`.

`status --json` returns an object with `assignments` (the assignment rows).
With `status NUMBER --json`, it contains only that item's rows. If no rows apply,
it also has an `explanation` string containing the plain output's text after `#NUMBER: `.

### Daily runtime maintenance

Maintenance is opt-in per project. With no `runtime-updates` key, the launcher
runs no updaters. Configure each CLI as `auto`, `off` (the default for an
omitted CLI), or a mapping containing an explicit updater `command` argv:

```yaml
runtime-updates:
  claude: auto
  codex: auto
  gh: auto
  timeout-seconds: 300
```

For custom installs, an operator can instead supply a targeted command:

```yaml
runtime-updates:
  claude: off
  codex:
    command: [mise, upgrade, codex]
  gh:
    command: [mise, upgrade, gh]
  timeout-seconds: 120
```

Commands run directly, without a shell, with the launcher's environment. Relative
executable paths resolve against the configuration directory. Operators must
supply a command that updates only that runtime, preserves its installation
method and channel, and needs no elevated privileges. `check` rejects unknown
keys, invalid policies or argv, direct privilege-elevation commands, and timeouts
outside `0 < timeout-seconds <= 3600`. The default timeout is 300 seconds; version
probes each have a separate five-second bound.

Claude Code and Codex are checked only when an agent's configured `runtime`
alternatives use them; `command:` agents do not invoke their updaters. Every
launcher and agent uses `gh`, so its `auto` or command policy is always checked,
including projects with only `command:` agents. Missing CLIs are reported and
never installed. Automatic detection supports these installations:

| Runtime / installation | Updater |
|---|---|
| Claude native installer | `claude update` |
| Claude npm global (`@anthropic-ai/claude-code`) | `claude update` |
| Claude Homebrew cask | `brew upgrade --cask claude-code` or `brew upgrade --cask claude-code@latest`, matching the executable's owning cask |
| Codex npm global (`@openai/codex`) | `npm install -g @openai/codex@latest`, using the npm verified to own that global prefix |
| Codex Homebrew cask | `brew upgrade --cask codex` |
| Codex Homebrew formula | `brew upgrade --formula codex` |
| GitHub CLI Homebrew formula (macOS or Linux, under `Cellar/gh`) | `brew upgrade --formula gh`, using the Homebrew verified to own that installation |

Homebrew commands run with dependent upgrades, automatic cleanup, application
quitting and sudo disabled, and without interactive confirmation. These settings
apply only to that updater process; see the [Homebrew command reference](https://docs.brew.sh/Manpage#upgrade-options-installed_formulainstalled_cask-).

Native detection recognizes Claude's version directory under
`~/.local/share/claude/versions`. npm detection requires a global package's
metadata to identify the executable, and Homebrew detection requires it to live
in the matching package's `Caskroom` or `Cellar` directory. Unknown installs,
mise shims, ambiguous npm prerelease channels, missing owning package managers,
and unwritable package installations are skipped with an instruction to
configure `runtime-updates.CLI.command` or update manually. apt, dnf and apk
installs that need root are skipped; no command uses `sudo`, upgrades unrelated
packages, changes credentials, models or permissions, or switches install methods.
Homebrew formulae are the only automatic updater for `gh`. Other `gh` installs
are skipped: system packages require manual updates with elevated privileges,
and shims or unknown installs require `runtime-updates.gh.command`.
These automatic-policy skips stay local to the launcher and do not start a
shared cooldown, so another project's configured command can update the same
installation. Each launcher remembers its own automatic skip for 24 hours;
changing the policy to a command takes effect at the next boundary.

Claude's own updater retains its release channel and version bounds and honors
`DISABLE_UPDATES`. `DISABLE_AUTOUPDATER` only disables Claude's background checks
and allows this explicit update. For every Claude installation and updater,
including operator-supplied commands, the launcher checks
`DISABLE_UPDATES` in its environment, Claude's user `settings.json` (including
`CLAUDE_CONFIG_DIR`) and system `managed-settings.json` / `managed-settings.d`
files before invoking the updater. It skips when that policy cannot be read safely.
Claude's `DISABLE_UPDATES` policy does not apply to Codex or `gh`.
These policy skips are local to the launcher: they do not start the shared
cooldown, so a launcher with updates enabled can still update the installation.
The cask name preserves the installed stable or latest channel. See
[Claude's update documentation](https://code.claude.com/docs/en/setup#update-claude-code)
and the [official Codex update commands](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex#quickstart-using-goals).

Checks run only at unclaimed launcher boundaries before new work, separately
from instruction refresh. A run that crosses the due time continues undisturbed.
Usage pauses allow maintenance and health checks to continue; updating or
repairing a runtime preserves its usage pause until the window expires.
Every completed installation check (`updated`, `up-to-date` or `failed`,
including a timeout) starts a shared 24-hour cooldown. An `off`, omitted or
unsupported automatic policy reports a local skip without writing shared
maintenance state, except for recovery of previously recorded failed health.
Active-run or guard contention defers the check without starting that cooldown.
`launch --once` uses the same boundary.
Each completed check prints one line with the runtime, install method, available
before and after versions, result and any failure reason. Local skips print the
runtime, install method and actionable reason.

The cooldown and runtime health survive restarts in per-user local state at
`$XDG_STATE_HOME/ub-agents/runtime-updates`, or
`~/.local/state/ub-agents/runtime-updates` when `XDG_STATE_HOME` is unset, empty or
relative. Files
are keyed by the PATH-resolved executable's installation: native version and
Homebrew version directories share a stable installation identity across
upgrades; custom launch paths remain stable across symlink changes. All local
launchers using that installation share its cooldown and kernel file locks,
including across projects. **Opting out in one project does not stop another
project's launcher from updating the shared installation.** GitHub is not used
for maintenance state.

A start reservation holds a shared maintenance guard through process start;
availability reads share this guard too, so concurrent launchers can reserve
and start the same runtime. Healthy cooldown reads do not acquire an exclusive
guard. If availability changes before a claim's reservation, the launcher prints
`waiting` and retries on a later poll. While an updater holds the guard, other launchers
cannot start that runtime. npm,
Homebrew and operator-command updates replace files in place, so they wait until
all tracked local runs of the installation finish. Claude native updates can
proceed alongside existing runs because Claude retains versions in use. Run
locks are inherited by the runtime process; updater processes inherit their
maintenance locks too. Locks are released when their final holder
exits; a crashed launcher leaves no stale marker, and its still-running child
continues to protect the installation. Descendants that retain an inherited
descriptor, including detached background processes, defer in-place maintenance
until they exit or close it.
All cooperating launchers must use a version with this locking protocol; it
does not track sessions launched outside ub-agents.

Every run, including a `command:` agent, reserves `gh` through execution and
cleanup; agent processes and cleanup hooks inherit its run lock. A `gh` update
therefore waits for all tracked runs to finish. Discovery waits while `gh` is
guarded or recorded unusable, then holds a reservation through the entire pass,
including recovery and claim writes. Maintenance defers during that pass without
starting its cooldown. Failed `gh` health blocks every new run until a
later unclaimed boundary finds it working again, even in projects with updates off.

**Upgrading:** upgrade every launcher of a project before setting
`runtime-updates.gh` in its configuration; earlier launchers reject that key.

Shutdown (`stop_gracefully`, SIGTERM or SIGINT) stops the updater and records a
completed check, then exits before claiming. Updater failures produce a launcher
warning and no assignment attempt or GitHub failure. After every completed check,
including a failure, the launcher re-resolves PATH and runs that next
executable's `--version`; an updater's exit code alone cannot establish success.
If the runtime still works, the launcher continues using it. If it fails this
probe, selection treats it as unavailable and can use an eligible alternative.
At later unclaimed launch boundaries, an unavailable installation's `--version`
is rechecked under the maintenance guard, including in projects with updates
disabled. A successful probe restores availability after a transient failure or
an operator repair in place. Health recovery preserves the original update
cooldown; no updater is retried before it expires. `status` only reads shared
health and does not run a recovery probe. There is no automatic rollback.

### Runtime permissions

The launcher adds no permission flags, so the CLIs start in their default headless
modes and usually cannot edit files or push. Grant each role what it needs:

```yaml
runtime-args: [--sandbox, danger-full-access]    # codex
runtime-args: [--permission-mode, acceptEdits, --permission-prompts, none,
               --allowedTools, "Bash(git *)", "Bash(gh *)", "Bash({report_command} report *)", "Bash({report_command} read *)",
               "Bash({report_command} retrospective *)",
               --add-dir, "{scratch}"]  # claude
```

These list examples belong inside an agent definition. `init` instead includes
one commented top-level mapping entry for the CLI selected by `--runtime`, inherited
by all four starter agents, without the optional retrospective rule. In an
interactive terminal, it explains the grant and asks once whether to enable it
for all four agents; only `y` or `yes` enables it. No, an empty answer or end of
input keeps the mapping commented out. When `CI` is set or stdin or stdout is not
a terminal, it asks nothing and keeps it commented out, matching label provisioning.
Whenever it remains commented out, init prints a next step to uncomment or customize it before
unattended work. The Claude starter includes a commented placeholder for project
check commands: `--allowedTools` denies commands
that are not listed.
The Codex example grants full access, including writes to Git metadata for commits
and commands for pushing. The Claude example grants file edits and the listed
Git, GitHub, report, filtered read and retrospective commands without permission prompts, plus access to the run's
scratch directory. Add permissions for your project's check commands as needed.
Claude's `Bash(ub-agents *)` rule does not match the absolute report command:
[Bash permission rules match the command text](https://code.claude.com/docs/en/permissions#what-a-bash-rule-doesnt-match),
including the executable path. Use `Bash({report_command} report *)`; the launcher
substitutes its exact command before starting Claude, keeping the interpreter,
entry-point path and `report` subcommand fixed. This permits headless reports
without permitting another executable named `ub-agents`. Add
`Bash({report_command} read *)` for filtered issue and PR reads; the starter
Claude example includes both rules.
Add `Bash({report_command} retrospective *)` for agents with a retrospective board;
the scoped example above and this repository's Claude configuration include it.
Keep the expanded command's shell quoting when invoking it. Both this repository's
Claude configuration and the Claude example generated by `init` use this rule.
Upgrade every launcher before adopting it: older builds reject configurations using
the placeholder.

`doctor` checks each runtime agent's effective arguments after inheritance or
replacement. It reports any agent with missing or empty arguments for a listed
CLI in one warning naming them, with one remedy linking this guidance. Configure
the top-level mapping or agent overrides to cover those CLIs. It does not fail or
test whether supplied arguments grant sufficient permissions.

Declare arguments once at the top level to give each CLI its own permissions and
settings across agents, including agents that mix runtimes:

```yaml
runtime-args:
  codex: [--sandbox, danger-full-access, -c, 'mcp_servers.example.enabled=true']
  claude: [--permission-mode, acceptEdits, --permission-prompts, none,
           --allowedTools, "Bash(git *)", "Bash(gh *)",
           "Bash({report_command} report *)", "Bash({report_command} read *)",
           --add-dir, "{scratch}"]
agents:
  reviewer:
    runtime: ["codex:gpt-6.1-sol:xhigh", "claude:claude-opus-5-5:xhigh"]
    instructions: .agents/reviewer.md
    trigger: needs-review
    outcomes: {approved: {add: [ready-to-merge]}}
```

An agent-level list still applies to every alternative and replaces all defaults.
An agent-level mapping also replaces all defaults; an omitted CLI entry receives
no arguments even when that CLI has top-level defaults. Mapping values may use
YAML aliases for selective reuse. Upgrade every launcher before adopting top-level
`runtime-args` or agent mappings: older builds reject these forms.

Codex's `workspace-write` sandbox cannot commit in
private worktrees, whose Git metadata lives in the main checkout.

Each run's scratch directory is outside the control checkout and private worktrees,
at `$XDG_STATE_HOME/ub-agents/<owner>/<repo>/runs/<run>/scratch`. As with runtime-update
state, unset, empty or relative `XDG_STATE_HOME` falls back to `~/.local/state`.
If the resolved scratch path is inside the target checkout, setup fails visibly
without starting the agent. Run logs, context files, `events.jsonl` and other
artifacts stay in `.ub-agents/runs/<run>/` in the control checkout.
Runtimes restricted to the working directory need `runtime-args` that also allow
access to scratch. For example, add Claude's `--add-dir` with the `{scratch}` placeholder:

```yaml
runtime-args: [--add-dir, "{scratch}"]
```

Combine this with the role's other permission arguments. The launcher replaces
`{scratch}` wherever it appears in a `runtime-args` argument, including
`"--add-dir={scratch}"`, with that run's absolute scratch path. Quote `"{scratch}"`
in YAML: unquoted `{scratch}` is parsed as a mapping rather than an argument string.
A placeholder is a single word in braces using letters, digits, `-` or `_`; config
loading accepts only `{scratch}` and `{report_command}`, rejecting others with the
top-level CLI or agent and placeholder named in the error. Other braces, such as JSON `'{"a": 1}'` or TOML `'x={y=true}'`, pass
through unchanged. There is no other templating or environment-variable expansion;
placeholders apply only to `runtime-args`, not `command` or other config keys.
The launcher adds no permission flags of its own.

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

The command records its result by invoking the command from `UB_AGENTS_REPORT`
with `report --outcome investigated --summary TEXT`, exactly like an LLM runtime.
This value is a shell-quoted command, which can contain multiple arguments; a
Python command can invoke it without a shell:

```python
subprocess.run(shlex.split(os.environ["UB_AGENTS_REPORT"]) +
               ["report", "--outcome", "investigated", "--summary", "Investigation complete"],
               check=True)
```

Import `os`, `shlex` and `subprocess` for this example. Every supervised runtime
and configured command receives the same environment variables:

| Variable | Value |
|---|---|
| `UB_AGENTS_CONTEXT` | Path to a JSON file describing the assignment |
| `UB_AGENTS_REPORT` | Absolute, shell-quoted command for the launcher's startup code copy and interpreter; also in context as `report_command`; append `report`, `read` or `retrospective` and its arguments |
| `UB_AGENTS_RUN_CONFIG` | Absolute path to `.ub-agents/runs/<run>/run.json` in the control checkout, pinning the run context and policy for `read`, `retrospective` and `report` |
| `UB_AGENTS_REPOSITORY` | `owner/name` |
| `UB_AGENTS_ASSIGNMENT` | Issue or PR number |
| `UB_AGENTS_RUN` | Run id |
| `UB_AGENTS_SCRATCH` | Absolute path to the run's private scratch directory; `TMPDIR` is set to the same path |
| `UB_AGENTS_LEASE_ID` | The claim's comment id |
| `UB_AGENTS_CANDIDATE_SHA` | The PR's head commit; empty for issue work |
| `UB_AGENTS_BRANCH` | The branch to work on, when known |

The launcher writes one `run.json` with `repository`, `run`, `assignment`,
`lease_id`, `agent`, `approvals`, `trusted-bots`, `triggers` and `retrospectives`.
In-run commands validate that file and match its repository and run to
`UB_AGENTS_REPOSITORY` and `UB_AGENTS_RUN` before contacting GitHub. Missing,
unreadable or invalid context exits nonzero without showing input or posting
records. `read N` outside a run (`UB_AGENTS_RUN` unset) uses local configuration.

Use `UB_AGENTS_SCRATCH` for temporary files instead of writing directly under
`/tmp`. Before starting a runtime or command, the launcher creates this directory
with mode `0700`; creation failure ends the run as a visible setup failure without
starting the agent. Once the run's processes are confirmed stopped, scratch and
its contents and the state directory's per-run directory are removed after success,
failure, timeout or interruption. Run logs and other artifacts remain.
Unconfirmed process termination preserves scratch, and a launcher killed before
cleanup leaves it behind. If scratch removal fails
after confirmed termination, the launcher leaves any remaining files, prints the
error and records a `scratch-removal-failed` event in `events.jsonl`. The run still
completes and releases its lease; this does not mark process cleanup unconfirmed.

## Commands

`ub-agents`, `ub-agents -h`, `ub-agents --help` and `ub-agents help` print the
same overview on stdout and exit 0. Operator commands and commands used inside a
run appear in separate groups; rows omit the program name and wrap within 80 columns.

```text
ub-agents — project-owned engineering loops on GitHub

usage: ub-agents <command> [options]

commands:
  init                   set up this repository: starter configuration, agent
                         instructions and workflow labels
  check                  validate the configuration and instruction files
  doctor [--json]        check the machine, GitHub access, labels and agent
                         runtimes
  launch [NUMBER]        run the queue in the foreground, or handle one item
  status [NUMBER] [--json]
                         matching work, owners, attempts and why items wait
  cleanup [--apply]      preview or remove stale worktrees and branches
  retry NUMBER           let stopped work run again, with a recorded reason
  approve NUMBER         record approval of an issue's or PR's current input

inside a run, through the launcher's report_command:
  report                 record the run's outcome
  retrospective          post to the agent's retrospective board
  read NUMBER            read an issue or PR as filtered JSON

options:
  -h, --help             show this help; after a command, that command's help
  -v, --version          print the version
  --config PATH          project configuration (default: ub-agents.yaml)
```

`help` remains available without appearing in the overview. Use `ub-agents help COMMAND`,
`ub-agents COMMAND -h` or `ub-agents COMMAND --help` for the same compact command help.
The usage line shows positional arguments and required options, with the remaining
options folded into `[options]`. For example, `ub-agents help launch` prints:

```text
usage: ub-agents launch [NUMBER] [options]

Run the queue in the foreground under the configured gates. Without a number,
watch the queue; with a number, handle only that issue or PR, then exit. Use
--agent NAME to serve only one configured agent; without a number, the queue
keeps polling until you stop it. For preparation, run --agent issue-preparer,
stop to resolve human decisions, then launch the normal queue. Queue priority
and milestone policy do not apply to an explicit item; all other gates do. An
agent still needs a matching trigger label, including with --agent.

options:
  --agent NAME           serve only this configured agent (queue or NUMBER)
  --once                 observe once, run at most one assignment, then exit
  --no-ui                plain lines instead of the terminal view
  --config PATH          project configuration (default: ub-agents.yaml)
  -h, --help             show this help

examples:
  ub-agents launch
  ub-agents launch --agent issue-preparer
  ub-agents launch 143 --agent implementer
```

All help forms work without project configuration, GitHub authentication or network
access, and create no files. Square brackets mean optional: `launch [NUMBER]` accepts
an optional item number, while `retry NUMBER` and `approve NUMBER` require one.
`retry` also requires `--reason REASON`, as shown in its command help.

`ub-agents -v` and `ub-agents --version` print `ub-agents 0.2.1` and exit 0.
An unknown command, including `ub-agents help NAME`, prints
`ub-agents: unknown command "NAME"`, a blank line and the overview on stderr, then
exits 2. Other usage errors exit 2 and print the command's usage line and error on
stderr; `ub-agents launch --bogus` shows `usage: ub-agents launch [NUMBER] [options]`.

- `ub-agents help [COMMAND]` shows the overview or detailed help for that command.
- `ub-agents init [--repository owner/name] [--runtime cli:model:effort]` writes the
  starter `ub-agents.yaml`, `.agents/ub_agents.md` and four role files next to the
  selected `--config` file, and adds `.ub-agents/` to `.gitignore`. It never creates
  or edits `AGENTS.md` or `CLAUDE.md`. It names the guidance each configured runtime
  loads: Codex uses `AGENTS.md`; Claude uses `CLAUDE.md` or `.claude/CLAUDE.md`, else
  `AGENTS.md`. It warns when a runtime loads none, because agents need project build
  and test instructions. The shared policy's Checks section points to the discovered
  guidance, or has placeholders when none exists. Fill in its merge gates, decision
  authority and review focus. Every starter role uses a private worktree, including
  issue preparation. Any existing configuration, shared policy or role starter file
  stops init before any file writes.
  In an interactive terminal, init reads repository labels and explains each missing
  trigger, outcome `add`/`remove`, and stop label. It creates only those labels after
  an explicit `y` or `yes`; the default is no. Declining makes no GitHub writes and
  prints one runnable `gh label create` command per missing label. Without a terminal
  (including CI and piped input), or when labels cannot be read, it prints commands
  for all configured workflow labels and explains why. Noninteractive init makes no
  GitHub reads or writes beyond repository inference when `--repository` is omitted.
  It never changes or deletes existing labels or uses `--force`. Local starter files
  are written regardless of the label-creation answer. A separate question offers to
  enable permission arguments matching `--runtime` for every starter agent; the
  default is no. Declining, end of input and noninteractive runs keep them commented
  out and print the setup step; see [Runtime permissions](#runtime-permissions).
- `ub-agents check` validates the configuration and instruction files.
- `ub-agents doctor [--json] [--verbose]` shows every warning and failure with its
  remedy, grouped in machine, configuration, GitHub and runtimes order. Each area
  has one summary line for passed and skipped checks; each distinct label has one
  result and counts once in area summaries and final failure and warning counts.
  Areas whose non-failing checks were all skipped use `skip`; areas with only
  warnings or failures have no summary. `--verbose` shows the full per-check list,
  including remaining GitHub requests and the reset time in UTC from real request headers.
  Doctor warns below 10% remaining and whenever it is rate limited.
  It checks everything `check` does, plus Python, the platform,
  `git`, `gh`, GitHub access, configured workflow labels, runtimes and local state.
  A token that cannot change labels is a required failure, because the launcher
  applies outcome transitions itself. Each missing label's result names every agent
  and use, with one `gh label create` remedy. A label with any trigger or outcome
  transition use is a required failure; a label used only to stop work is a warning.
  Label matching is case-insensitive and an unreadable label list is a required
  failure. The non-required `github-launcher-role` check warns when the launcher
  account has `maintain` or `admin`, because agents could start and approve their
  own work, when its role is below `write`, or when its role cannot be read.
  It also warns about each listed account without `write` or higher or with an
  unreadable role, and when the authenticated account is unlisted.
  Use a dedicated account with `write`;
  see [Issue approvals](approvals.md#repository-roles). Runtime agents without
  effective `runtime-args` for any listed CLI, after top-level inheritance or agent
  replacement, produce one warning naming them and linking the permission guidance.
  After its report, interactive doctor offers to create the missing labels with the
  same explanation and prompt as init; the default is no. It writes labels only after
  a confirmed `y` or `yes` and never changes existing labels. It then reads labels
  again, and its final counts and exit status reflect that second read. Declining,
  end of input, no terminal, CI and `--json` create nothing and keep the creation
  commands in the report. Other diagnostics make no writes.
  When the selected configuration is missing, it reports
  `no ub-agents.yaml here; run ub-agents init`, using the selected file's name.
  It exits 1 when a required check fails; warnings and skips exit 0.
  The JSON is unchanged by `--verbose` and has `version: 2`, `ok` and `checks`.
  Each check has `id`, `status`, `required`, `agent`, `runtime`, `message` and `remedy`.
  Label checks have the id `github-label:NAME`, `agent: null`, `runtime: null` and
  an additional `uses` list. Each use has `agent` (null for a stop use), `meaning`
  and `required`. The label check's `required` is true if any use is required.
  Names are deduplicated case-insensitively, preserving the first configured spelling.
- `ub-agents approve N` prints the current issue or PR title, body and
  outside comments; for PRs it also prints the head, outside reviews and review
  comments. It posts one [approval record](approvals.md#approving-current-input),
  pinning a PR head and recording the feedback it clears. It requires the
  authenticated `gh` account to have the `maintain` or `admin` repository role and
  refuses input changed during display. Running the command expresses approval
  without an interactive confirmation; it changes no labels and does not replace
  the required maintainer start.
- `ub-agents launch [--once] [--agent NAME]` runs the loop in the foreground.
  Without a number, `--agent` serves only that configured agent, including its
  pending outcome recovery and notices. Other agents' assignments, leases,
  pending outcomes and notices are left for another launcher. Agent names come
  from `agents:`; an unknown name is a usage error listing the configured names
  before queue observation. If a reload removes the selected agent, the launcher
  claims nothing and says it is no longer configured.
  Queue priority, milestone policy and all other eligibility and supervision
  gates still apply. `--once --agent NAME` observes once and runs at most one
  selected assignment; continuous launch keeps polling until stopped, even
  when no selected work is eligible. The terminal view and plain output name
  the selected agent. Idle output distinguishes no open item with its trigger
  labels, naming them, from waiting work counted by state, with reasons on each
  item's row. Items this agent parked for a human decision remain counted after
  their trigger labels are removed.
  For example, run `ub-agents launch --agent issue-preparer` to prepare a
  milestone's issues, using your configured preparation agent name. Watch it
  finish or reach human decisions, stop it, resolve those decisions, then run
  `ub-agents launch` for the normal queue. Filtering does not override milestones,
  bypass blockers or automatically launch another agent.
- `ub-agents launch N [--agent NAME]` evaluates only issue or PR N and exits after
  running one assignment or recovering its pending completion. The number implies
  `--once`; an explicit `--once` is also accepted. Queue priority and milestone
  selection do not apply to N, including after checkout refresh.
  All other eligibility gates apply, including
  launcher trust, state, matching trigger labels, stop labels, approvals, dependencies,
  ownership, attempts, backoff and runtime availability. Without `--agent`, the
  first eligible agent in configuration order acts; with it, only that configured
  agent is evaluated. `--agent` still requires that agent's trigger label;
  it does not choose a default role or authorize untriggered work.
  An unknown agent is a usage error. If no agent can act,
  it leaves one short refusal line per evaluated agent visible after the terminal
  view closes, or explains closed work and unmatched triggers. Plain output ends
  with the same reasons after discovery counters. Ownership names the owning role,
  account, host and lease end without log paths, and the owning item when another
  item's run holds the branch; stop labels explain the Action needed
  notice and restoring a trigger, even when no trigger remains. A decline during
  refresh or claiming reports that final verdict. If no assignment or recovery
  starts, it exits nonzero; GitHub read failures remain errors. Normal approval
  parking still applies. Reads are scoped to N's inputs and gates; other work is
  not discovered or ranked. It uses the same launch log,
  signal handling and execution exit codes as `launch --once`.
- `ub-agents cleanup [--apply]` previews stale owned artifacts; `--apply` rechecks and
  removes eligible worktrees and local branches, running the project hook first.
- `ub-agents status [NUMBER] [--json]` shows matching work, claims, consecutive failures in the `attempts` field, and outcomes.
  Without a number, it lists the queue as usual. With a number, it evaluates only
  that open or closed issue or PR under the same gates as `launch NUMBER`, showing
  each evaluated agent's row: `ready` when launch would run it, or `recover` when
  launch would recover its pending completion. Priority and milestone use the
  item's own values, without inheritance. Queue priority and milestone policy
  do not apply to N; all other gates do. When no rows
  apply, it prints launch's one-line explanation, including trigger labels to add
  or the item's closed state. It makes no GitHub writes and starts no runtime
  maintenance. It exits 0 when it can report, even for parked, blocked or
  untriggered work; unreadable work exits 1, and invalid numbers exit 2.
  A live lease's summary names its actor, host, claim time, runtime and lease end,
  including the time remaining. Times use UTC `HH:MMZ`, with a date when outside
  the current UTC day. Only leases on this host have their recorded process group
  inspected: live members display `running` and the path to `process.log`; an
  exited group explains launcher completion or recovery after lease expiry. If
  that owning run reported an outcome, the reason explains acceptance after expiry.
  Other reasons distinguish a starting
  run, a claim without a host, another host, outcome recovery and an unknown
  process state with its inspection error. Inspection failures leave `status`
  successful. It makes no additional GitHub requests, recovers or releases nothing,
  and preserves the text for rows without a live lease, including shared-branch ownership.
  JSON adds `process` (`running`, `exited`, `starting`, `claiming`, `other-host`,
  `recovery` or `unknown`) and `process_reason`, both `null` without a live lease.
  Existing JSON fields retain their values: `state` stays `owned` for a confirmed
  running owner, and `reason` retains the ownership explanation.
  Each agent's `reported:` (JSON `outcome`) describes that agent's live lease's run,
  or its latest lease's run when it has no live lease. Recovery leases show the
  original run's report. If that run has not reported, `reported:` is omitted and
  JSON `outcome` is `null`. The JSON `lease` field shows the item's live owner, even
  when it is another agent.
- `ub-agents report --outcome NAME --summary TEXT [--handoff PR]` records a declared
  successful outcome. Use `--status retry|blocked` for failures. It works only inside
  a supervised run.
- `<report_command> retrospective --body-file PATH` posts to the supervised
  agent's configured discussion board and prints the comment URL; it changes no
  run outcome. See [Agents](#agents).
- `ub-agents retry N --reason TEXT [--agent NAME]` resets one agent's consecutive failure count on
  an item once you have fixed the cause. It prints the reset record's link and a
  next-step line explaining closure, stop labels to remove, trigger labels to add,
  or pickup by a running launcher on its next poll. Labels stay unchanged; use
  `ub-agents status` for progress and other pickup gates.
  For `retry`, omitting `--agent` selects the first configured agent
  whose `kind` matches the item or is `either`, and prints its name before acting.
  Trigger labels and other eligibility gates do not affect this selection. If no
  agent applies, the command fails without recording anything. Specify `--agent`
  when resetting a particular agent's attempts.
- `approve` and `retry` require a positive item number. The deprecated
  `--number N` alias remains available for one release and is hidden from help;
  giving the number both ways is a usage error.
- `--config PATH` selects a different configuration file before or after any
  project command. `report`, `retrospective` and `help` accept it only before the
  command and read no configuration. Giving it in both positions is a usage error. For example,
  `ub-agents --config x.yaml launch` and
  `ub-agents launch --config x.yaml` both write `.ub-agents/launch.log` next to
  `x.yaml`.
