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
| `poll-seconds` | Minimum gap between discovery-pass starts, including after a run (default 30 seconds). |
| `stop-labels` | Labels that park an item (default `[needs-human]`). |
| `cleanup` | Optional project cleanup hook and timeout, run before private worktree removal. |
| `runtime-updates` | Optional daily maintenance policy for Claude Code, Codex and the launcher's GitHub CLI (`gh`). |
| `queue` | Priority ranking, dependency waits and optional milestone gating or ordering (defaults to FIFO, waiting for blockers, with milestones ignored). |

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

`ub-agents launch`, including `--once`, appends stdout and stderr to
`.ub-agents/launch.log` in the control checkout. Every file line starts with a UTC
ISO 8601 timestamp; terminal text stays unchanged. Each line is flushed immediately
to both destinations, including the final stop or error message. The log is never
truncated or rotated. Use `tail -f .ub-agents/launch.log` to follow the loop from
another terminal. See [Stopping and restarting](operations.md#stopping-and-restarting)
for signal handling, including during a GitHub request.

Every `launch` session, including `--once` and `launch N [--agent NAME]`, also
publishes a local JSON snapshot at `.ub-agents/sessions/<session-id>.json` in the
control checkout. Publication is always on and has no configuration key.
Writing the snapshot makes no GitHub requests; continuous launch also runs
read-only observation passes during assignments to refresh the planned queue.
`status` and embedded loops without an observer keep their existing
behavior. Snapshots contain issue titles and descriptions already read by the
launcher, so treat them as private project data. The `.ub-agents/` and `sessions/`
directories have mode `0700`; snapshot and temporary files have mode `0600`.

Each launcher has a separate random session ID. Files are replaced atomically and
hold the latest coalesced observations, with **format `version: 1`**, rather than
an event journal. A consumer can enumerate `sessions/*.json` without GitHub access.
The worker updates `published_at` every five seconds during idle waits and running
assignments. A session is stale if its launcher PID is gone on the recorded host,
or publication has stopped for more than 30 seconds. An idle session instead has
a fresh heartbeat and `activity.state: waiting`, with the wait's `until` time.
A clean exit publishes `ended: true` and `stopping`. If writing fails or hangs,
the last snapshot may remain stale; execution and shutdown do not wait for the
writer. The isolated helper exits within one second of launcher exit if a write
hangs. Successful publications prune old ended or stale files, retaining at most
20 other inactive sessions, including abandoned temporary files. Live sessions
are retained. Consumers should check liveness as well as timestamps because PID
reuse is possible.

The version 1 envelope contains:

| Field | Contents |
|---|---|
| `session`, `pid`, `host`, `actor`, `repository`, `config_path` | Launcher identity and configuration; `started_at` and `published_at` use UTC ISO 8601 times. |
| `activity` | `polling`, `waiting` (with `until` and a reason), `running assignment`, or `stopping`. |
| `poll_now` | Optional poll control: `cooldown_until` and `rate_limit_until` are UTC ISO 8601 times or `null`; `waiting: true` confirms a poll waiter is installed, allowing immediate feedback on `r` during an assignment unless cooldown or rate limits apply. Missing or false `waiting` gives no such confirmation. `refreshing: true` means an `r`-forced read-only queue refresh is running during an assignment. Missing or false `refreshing` means no forced refresh; scheduled refreshes do not set it. |
| `assignment` | Current item, kind, title, agent, effective priority word, run, runtime, attempt, lease state and expiry, process state and reason, and this run's `process_log` and `context_path`. Recovery includes `recovered_run` and has no agent log or context. |
| `latest_pass` | Start time, `partial` or `complete`, and the plans reached, plus rows carried during a partial pass. Discovery removes closed or merged items and Eligible rows without that agent's trigger; open Needs attention rows remain until replanned or completion. Rows include item, kind, title, agent, effective `priority` word (or `null`), chosen runtime when available, consecutive `failures`, `max_attempts`, state, reason and observation time. Descriptions have `available`, bounded `text` and `omitted_characters`, or an unavailability reason. Another launcher's owner includes only actor, host and run, with no log paths. |
| `outcomes` | This session's recent reports and recovered outcomes: item, kind, title, agent, run, runtime, handoff when reported, supervisor result, report result, summary, time, acceptance, transition completion, and currently observed human blockers. Older snapshots may omit optional header context. |
| `limits`, `omitted`, `shortened` | Format limits and counts of dropped rows and shortened fields/characters. Individual rows also carry text shortening counts. |

Process states use `claiming`, `starting`, `running`, `exited`, `recovery` and
`unknown`. Only a process recorded by supervision is shown as running; a lease's
`running` state alone is insufficient. Report acceptance is `unaccepted`,
`rejected`, `accepted` (transition not yet complete), or `finalized` (accepted and
transition complete). `completed` can be true while `human_blocker` lists stop
labels such as `needs-human`. Finalized outcomes' blockers carry their last
observation time and are updated when a later pass reaches that target;
unfinalized reports have no applicable blocker yet. Missing values are `null` with
reasons where known; unavailable descriptions explicitly say why.

Snapshots hold at most 100 latest-pass rows and 20 recent outcomes. Each text
value is limited to 2,048 characters and the entire UTF-8 JSON file to 64 KiB;
the size bound can omit additional plan rows or older outcomes. Publication uses
a bounded mailbox and an isolated worker. A slow or failed writer cannot delay
claims, outcome acceptance, recovery, configuration reload, signal handling or
launcher exit; failures produce at most one publication diagnostic per session.
Snapshots are never read for claims, coordination or recovery.

`poll-seconds` measures the minimum time between the starts of successful
continuous discovery passes, including observation passes during a run.
A run's report, transitions and cleanup finish
immediately; after a pass that ran or recovered work, the launcher waits for the
part of that interval still remaining since the latest pass started. If that
pass already took the interval, the
next pass starts immediately. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
waits. Failed-poll retry delays below are independent of this interval, and
`launch --once` never waits after its pass.

When no open issue or PR has a configured trigger label, `launch` and
`launch --once` list the trigger labels and say to add one to start. Continuous
launch's idle message rounds its next poll delay to whole seconds below a minute
or whole minutes otherwise, such as `next poll in 56s` or `next poll in 2 min`.
Work already carrying a trigger retains its eligibility diagnostics.

Empty passes back off under a fixed budget rule, not a configuration key: **ten
idle launchers** sharing one account together get at most half the common **5,000
REST requests/hour** quota. Each launcher's share is **250 REST requests/hour**
(`5000 × 0.5 / 10`), leaving the other half for busy loops and agents' `gh` calls.
After an empty pass using `N` quota-counted REST responses, the next pass starts
`max(poll-seconds, N × 14.4 seconds)` after this pass started, capped at **one hour**.
REST responses count, including extra pages, rate-limit retries and
approval-parking writes; HTTP 304 confirmations, GraphQL and transport failures
without an HTTP response do not. A five-response pass using REST quota waits
72 seconds; a cold 230-request pass waits 3,312 seconds (about 55 minutes). Time
already spent in the pass, including rate-limit waits, counts toward the gap.
Observation passes during a continuous run use the same budget, low-quota doubling,
one-hour cap and rate-limit waits as empty passes. They evaluate the whole ranked
queue without claiming, recovering, approval-parking or printing plan or idle lines.
Only completed observation passes replace the snapshot's queue rows; a failed or
rate-limited pass retains the previous rows and cannot interrupt execution,
heartbeats, reporting, transitions or cleanup, or stop the launcher.
After the run, the next claiming pass returns to normal `poll-seconds` pacing.

The launcher retains `X-RateLimit-Remaining`, `X-RateLimit-Limit` and
`X-RateLimit-Reset` from the latest response for each `X-RateLimit-Resource`
(`core`, `graphql`), read through `gh api --include`. It makes no `GET /rate_limit`
probe. If any resource has **less than 20%** remaining, the empty-pass gap doubles.
The extra wait stops at that resource's reset (the earliest reset if several are
low), never shortens the ordinary gap, and remains capped at **one hour**.
Resources whose reset has passed, and incomplete or unreadable quota headers,
do not add a wait.

When the launcher becomes idle, and again when the set of low-quota resources
changes, it prints `No eligible work; next poll in <n> min (<k> requests last poll)`.
The request count measures REST quota usage, excluding HTTP 304 confirmations.
It does not repeat the message on every empty pass. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
this idle wait.

Claiming discovery evaluates candidates in rank order and stops once it claims
work. During that run, separate read-only observation passes evaluate all rows;
their results never choose the next claim. After the run, fresh claiming discovery
again walks the rank order and rechecks authority before any write. Lower-ranked
rows are announced and approval-parked only by a claiming pass that reaches them.
The observation worker starts with independent copies of the launcher's discovery
inputs, REST ETags, repository comment cache and comment cursor. Its first scan
continues from that cursor; new claim comments invalidate the claimed item's
inputs. Later cache changes stay local to each client, and the worker keeps its
own request counters and rate-limit state.
`launch --once` and `launch N` do not start observation passes; `status` still
evaluates every row and remains read-only.
Within each pass, an item's comments supply history and approval input, and
`status` renders the same history. Fresh repository permissions are read once per
account across all reached items. These shared reads end with the pass; each
claim-time approval check reads permissions anew.
Each launcher retains per-item discovery inputs in memory: history, approval
inputs and permissions, PR details, and dependency links. Record authors' roles and
the launcher's own role are checked freshly each pass, including on unchanged items.
Changes in the issue list (including `updated_at`) or an item's comment IDs and
update times invalidate that item's reads. A comment aging out of the discovery
lookback also invalidates those reads once, including when it was the item's last
cached comment. A fresh claim-approval denial also drops the item's cached
inputs so the next reached pass can plan its gate. Claims and approval parking
always revalidate with fresh reads;
cached input never authorizes a claim or a write. Restarting a launcher drops its
cache. With configured priorities, cold discovery lists the dependency graph in
pages to preserve inheritance without one REST request per queued issue.

For a rough request budget, an unchanged warm pass sends one request per page of
open issues/PRs, plus the incremental repository comment scan (usually one page),
an optional milestone list, and one permission read per distinct listed record
author (all marked-record authors when `launchers` is omitted). Checking a ready
launcher's own account can add one permission read if it was not already read.
List pages hold up to 100 rows; a full REST page
also needs a request to check for a following page. A cold pass or changed item adds
roughly 3–7 reads for each candidate actually reached, plus one permission read per
distinct account across those candidates, with extra pages for long histories.
Unchanged REST reads with an ETag consume no quota when GitHub confirms freshness
with HTTP 304; the incremental comment scan's moving `since` cursor skips ETag
caching. Configured priorities add a paginated dependency-graph list on cold
discovery and each `status` invocation; very large dependency lists may
need extra pages. Fresh claim/recovery reads, approval-parking writes, execution
heartbeats and completion add their own requests. `status` pays for every row.

For a REST quota discovery cost `R`, the idle interval is at least `R × 14.4` seconds,
so idle quota usage averages at most 250 requests/hour per loop for passes below the
one-hour cap. A pass consuming two quota-counted responses at the default 30 seconds
uses about 240 requests/hour; ten such loops use about 2,400. Cold passes can spend
requests in a burst, and a pass exceeding 250 requests reaches the cap; this pacing is not
a strict rolling-hour limiter. Sum all loops using the account, including other
repositories, and add busy discovery, execution/write costs and agents' calls.
GraphQL has a separate point budget; graph-list query cost depends on its
connections. Long runs reduce discovery frequency.

Continuous `ub-agents launch` retries failed discovery polls and claim POSTs for
request timeouts, connection failures, empty or truncated responses reported by
`gh` as `unexpected end of JSON input`, and HTTP 5xx responses. The fixed backoff
starts at **5 seconds**, doubles after each consecutive failure and caps at
**60 seconds**. The launcher stops
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

Time spent waiting for a rate limit counts toward the `poll-seconds` or empty-pass
budget gap between discovery-pass starts. A completed pass waits only for any gap
still left; if the wait or run already used that time, the next pass starts immediately.

Rate-limited reads do not count toward the poll failure limit or an item's attempts.
Each wait prints `GitHub rate limit reached; waiting until <reset UTC> (<n> min)`
and makes no GitHub writes while waiting. Authentication, permission, missing
repository, other malformed responses and unclassified failures still stop immediately.
The error names the request and tells the operator to fix the cause and restart
`ub-agents launch`.

Each skipped poll for another transient error prints its error and next delay
and does not report an empty queue. Failed discovery reads make no GitHub writes.
Discovery includes initial authentication and fresh reads immediately before a
claim, including the default-branch read for instruction refresh. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
discovery waits. `launch --once` and `status` still fail on their first error.

A retryable claim POST failure ends the pass with the same skipped-poll message
and counts toward the same failure limit. Before planning on the next pass, the
launcher checks whether that claim was created. If present, it withdraws the claim
with `state: withdrawn` and summary `Claim response was lost; withdrawn`.
This spends no attempt and adds no item backoff, so the item can be claimed again
on that pass. If the launcher restarts first, the claim expires normally.
Failures after a confirmed claim retain their existing handling.

From claim election through release, including completion recovery, rate-limited
reads wait and retry under the active lease, even when the reset is beyond its
current expiry. Renewal continues during the wait. If the last confirmed expiry
actually passes, the launcher takes the lost-ownership path and leaves expiry recovery
to finish durable completion. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
owned-run waits. Rate-limited writes other than claim POSTs retain their existing
handling and are not replayed by this retry mechanism.

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

`milestones` accepts `gate`, `order` or `ignore` and defaults to `ignore`.

In `gate` mode, new issues wait for the oldest open milestone with open issues or
PRs to close or empty. Creation time and then milestone number select that active
milestone. Later and unmilestoned issues wait even when the active milestone has
no eligible issue. Planning, claiming and approval parking enforce this gate;
PR work, owned runs and recovery remain eligible.

In `order` mode, new issues rank first by open milestones with open issues or PRs,
oldest first by creation time and then milestone number. Issues without a milestone,
or with a milestone outside that list (such as a closed milestone), rank after all
listed milestones. Within each milestone, effective priority, item creation time
and item number decide order. An earlier milestone wins even against higher
priority in a later milestone. Milestones never hold back an otherwise eligible
issue; later and unmilestoned work can start when earlier work cannot.
PR work, owned runs and recovery keep their existing priority order before new
issue starts. Ordering uses the milestone list and each listed issue's milestone;
an unreadable milestone list stops selection visibly. In `ignore` mode, planning
and claiming do not read milestones. `ub-agents check` accepts all three modes.
This repository explicitly sets `order`; existing `gate` configurations remain
valid. To let later and unmilestoned work start while earlier work cannot, switch
`gate` to `order`.

`dependencies` accepts only `wait` or `ignore` and defaults to `wait`, including
without a `queue` block. In `wait` mode, an issue cannot start preparation or
implementation while any GitHub blocked-by issue is open, including blockers
in other repositories. Closed blockers do not gate it. The gate is rechecked
before claiming. An open local issue inherits the highest effective priority
of its open local dependents, directly or transitively, without changing labels.
In milestone `order` mode, blockers also inherit the earliest milestone from open
local dependents, directly or transitively, even without configured priority
labels. Priority and milestone inheritance choose their sources independently.
Cycles terminate and share the highest reachable priority and earliest milestone.
With `ignore`, links affect neither eligibility nor priority or milestone rank,
and dependency reads are skipped. PR work, recovery and completion of started
runs remain ungated. With milestone `gate`, a new issue must pass both gates;
blockers inherit priority but keep their own milestone.
A failed or unreadable dependency read stops selection visibly.
Planning skips link reads only when the issue list's dependency summary reliably
reports zero total blockers; missing or malformed summaries require a full read.
The claim-time recheck always reads the selected new issue's blocker links.

`ub-agents init` writes `queue: {milestones: ignore, dependencies: wait}`. Without a
`queue` block, priorities are unconfigured, milestones are ignored and dependency
waits apply.

Priority is followed by item creation time and then item number; milestone `order`
adds milestone rank first for new issue starts. See
[selection order](coordination.md#selection-order) for eligibility and
PR precedence. `ub-agents status` and `status --json` use the same rank order and
show each item's effective priority (`none` in text, `null` in JSON when no label
or default applies). In `order` mode, each issue also shows its effective milestone
next to priority (`none` when unmilestoned), with the source when inherited, such
as `milestone #2 (inherited from #21)`. JSON includes `milestone` (the effective
milestone number, or `null`) and `milestone_inherited_from` (the source issue number,
or `null`). In `gate` mode, waiting issues keep the
`Waiting for active milestone #N` reason. Waiting issues also name open blockers,
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
| `different-runtime-from` | Another agent's name; requires a PR. When an accepted report identifies that agent's runtime for the current head, this agent must run on a different CLI and model; a different effort doesn't count. Wait for a pending handoff to finish. Without such a report or pending handoff, use only the first configured runtime, blocking if its CLI isn't installed. |
| `worktree` | `true` runs in a private checkout: the PR's exact commit, or a fresh branch for an issue. |
| `runtime-args` | Extra arguments for the runtime CLI, such as permission flags. |
| Limit keys | Override `limits` for this agent. |

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

Only agents with this key receive a prompt line with the literal command and when
to post: the run lost something, and the agent can name the change that would have
prevented it. Agents without the key receive no retrospective prompt line.

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
It runs the launcher's own installation regardless of PATH, the working directory
or `PYTHONPATH`, for both console-script and `python -m ub_agents` launchers.
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

`runtime-args` are appended, with `{scratch}` replaced by the run's absolute scratch
path and `{report_command}` by the launcher's absolute report command
(see [Runtime permissions](#runtime-permissions)). They must not change the model
or effort, or resume a session: `check` rejects those flags, because
`different-runtime-from` trusts the recorded `cli:model:effort` and every run starts fresh.
For agents with a Claude runtime, `runtime-args` must not set `--output-format`
(including `--output-format=…`); `check` rejects it because the launcher owns the
stream format. A redundant `--verbose` is accepted.
Codex `runtime-args` must not set `--ephemeral`: the launcher may need the fresh
session's limit signal when it is absent from the JSON stream. A redundant `--json`
is accepted.

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

`init` includes the matching example for every starter agent using the CLI selected
by `--runtime`, without the optional retrospective rule. In an interactive terminal,
it explains the grant and asks once whether to enable the examples for all four
agents; only `y` or `yes` enables them. No, an empty answer or end of input keeps
them commented out. When `CI` is set or stdin or stdout is not a terminal, it asks
nothing and keeps them commented out, matching label provisioning. Whenever they
remain commented out, init prints a next step to uncomment or customize them before
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

`doctor` reports runtime agents with no `runtime-args` in one warning naming them,
with one remedy linking this guidance. It does not fail or test whether supplied
arguments grant sufficient permissions.

`runtime-args` apply to every alternative in a runtime list, so use CLI-specific flags
only on agents with a single runtime. Codex's `workspace-write` sandbox cannot commit in
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
agent and placeholder named in the error. Other braces, such as JSON `'{"a": 1}'` or TOML `'x={y=true}'`, pass
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
| `UB_AGENTS_REPORT` | Absolute, shell-quoted command for the launcher's own installation; also in context as `report_command`; append `report`, `read` or `retrospective` and its arguments |
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
watch the queue; with a number, handle only that issue or PR, then exit.

options:
  --agent NAME           evaluate only this configured agent (needs NUMBER)
  --once                 observe once, run at most one assignment, then exit
  --no-ui                plain lines instead of the terminal view
  --config PATH          project configuration (default: ub-agents.yaml)
  -h, --help             show this help

examples:
  ub-agents launch
  ub-agents launch --once
  ub-agents launch 143 --agent implementer
```

All help forms work without project configuration, GitHub authentication or network
access, and create no files. Square brackets mean optional: `launch [NUMBER]` accepts
an optional item number, while `retry NUMBER` and `approve NUMBER` require one.
`retry` also requires `--reason REASON`, as shown in its command help.

`ub-agents -v` and `ub-agents --version` print `ub-agents 0.1.15` and exit 0.
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
  `runtime-args` produce one warning naming them and linking the permission guidance.
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
- `ub-agents launch [--once]` runs the loop in the foreground.
- `ub-agents launch N [--agent NAME]` evaluates only issue or PR N and exits after
  running one assignment or recovering its pending completion. The number implies
  `--once`; an explicit `--once` is also accepted. All normal eligibility gates
  apply, including launcher trust, approvals, dependencies, milestone gates,
  ownership, attempts, backoff and runtime availability. Without `--agent`, the
  first eligible agent in configuration order acts; with it, only that configured
  agent is evaluated.
  An unknown agent or `--agent` without N is a usage error. If no agent can act,
  it prints each evaluated row's status reason (including live lease and process
  details), or explains missing/closed work and unmatched triggers, and exits
  nonzero without a claim. Normal approval parking still applies. Reads are scoped
  to N's inputs and gates; other work is not discovered or ranked. Priority and
  milestone ordering do not affect this command. It uses the same launch log,
  signal handling and execution exit codes as `launch --once`.
- `ub-agents cleanup [--apply]` previews stale owned artifacts; `--apply` rechecks and
  removes eligible worktrees and local branches, running the project hook first.
- `ub-agents status [NUMBER] [--json]` shows matching work, claims, consecutive failures in the `attempts` field, and outcomes.
  Without a number, it lists the queue as usual. With a number, it evaluates only
  that open or closed issue or PR under the same gates as `launch NUMBER`, showing
  each evaluated agent's row: `ready` when launch would run it, or `recover` when
  launch would recover its pending completion. Priority and milestone use the
  item's own values, without inheritance or milestone ordering. When no rows
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
