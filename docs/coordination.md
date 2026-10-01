# Coordination contract (version 1)

This is a cooperative serial launcher, not a distributed lock service. GitHub is
durable truth; `.ub-agent/` is disposable. The project is trusted executable
configuration. Workflow labels, checks, acceptance, and merge authority stay there.

## Assignments and attempts

- A run has a random unique ID and one assignment: repository, issue/PR number,
  configured agent, matched trigger, and (on PRs) the observed head SHA.
- Trigger lists mean **any** matching label.
  A runtime list means alternatives in declared order: select the first eligible
  installed executable and execute it once.
- One live lease excludes every other agent on the same issue/PR. This first
  implementation is serial, including recovery. Different machines cooperate.
- Attempts are scoped to **item number + configured agent**. Every started run
  counts, including successful revisions; PR head/label changes do not reset the
  budget. Issue-to-PR handoff starts the PR's own budget while retaining source
  provenance. A crashed expired tentative claim counts conservatively. A contender
  that confirms withdrawal does not. Outcome-only recovery does not cost a start.
- An accepted issue-to-PR handoff suppresses duplicate initial work until an
  explicit reset, even if its trigger remains on the issue. Other successful issue
  tasks may run again when a human reapplies their trigger. PRs returning to a
  matching label run a new assignment with the accumulated PR budget. Reapplying a
  trigger does not reset that budget. Legacy roles should still remove old labels;
  declared transitions do so in the runner.
- `ub-agent retry --number N --agent NAME --reason TEXT` posts a durable reset,
  preserving history. It refuses a live lease and never revokes someone else's run.

## Selection order

1. Eligibility checks run as usual: existing claims, stop labels, retry backoff and
   attempt limits still apply.
2. PR work comes before new issue starts. This includes PR assignments, recovery
   and completion of already-started runs. Neither queue gate holds back this work.
3. With `queue.milestones: gate`, only new issues in the active milestone can
   start. The active milestone is the oldest open milestone with open issues or
   PRs, by creation time with milestone number as the tie-breaker. Later-milestone
   and unmilestoned issues wait until it closes or empties, even if it has no
   eligible work. With no active milestone, all issues are eligible. Planning and
   the fresh claim-time milestone recheck enforce this same policy. In the default
   `ignore` mode, neither consults milestones.
4. With `queue.dependencies: wait` (the default, even without a `queue` block),
   a new issue waits while any GitHub blocked-by issue is open. All blockers must
   close, including blockers in other repositories. Planning and a fresh
   claim-time dependency read enforce this gate. When milestone gating is also
   enabled, a new issue must pass both gates. With `ignore`, links affect neither
   eligibility nor priority and dependency reads are skipped.
5. An open issue's effective priority is the highest of its configured priority
   (label or default) and the effective priority of every open local issue it
   blocks, directly or transitively. Inheritance includes issues without workflow
   triggers and issues waiting at either gate. Closed issues do not propagate
   priority; external blockers gate but do not inherit local priority. Traversal
   terminates even in cycles: every member receives the highest priority reachable
   from the cycle and its open dependents. The launcher never changes labels.
6. A PR's effective priority is the highest of its own configured priority and
   the effective priority of each open local issue it closes, as named by closing
   keywords in its body. It includes the issue's inherited priority in `wait`
   mode, and its own configured priority in `ignore` mode. Review, revision and
   integration therefore inherit urgent work's priority without label changes.
   Each closing issue requires its own keyword; ordinary mentions, closed issues,
   PR references and foreign repository references do not supply priority.
7. Within PR work and within issue work, rank by effective priority, then item
   creation time, then item number. Without a default, unlabeled items rank below
   all configured priority labels. Without priority configuration, ranking is FIFO
   with number as the tie-breaker. Agents on the same item retain YAML order
   within each work class. Priority labels are read without modification.

`ub-agent status` and `status --json` list rows in this rank order, show effective
priority and its inherited source, and name the active milestone and open blockers
for waiting issues. See the
[queue configuration](configuration.md#queue).

Concurrent launchers rank the GitHub state each observes and try claims in that
order. Existing claims resolve contention for the same item. There is no global
order across machines.

When there is work to plan, selection uses the issue list's
`issue_dependencies_summary` to skip blocked-by reads for issues with a validated
`total_blocked_by` of zero. All other open issues, including those without workflow
triggers, have their links read to compute inheritance. Missing or malformed
summaries fall back to full reads. Claims always recheck the selected new issue's
blockers, even if its observed summary was zero. Read cost therefore grows with
issues that have blocker links or unknown summaries, rather than all open issues.
An unreadable or failed dependency read stops selection rather than being treated
as an empty list. Like milestone rechecks, this is cooperative observation, not
an atomic snapshot: dependencies can change between reads and after a claim.
They never hold back completion or outcome recovery of already-started runs.

## Trusted operators

Only the authenticated GitHub account's comments supply coordination authority by
default. `operators: [LOGIN, ...]` extends trust to explicit peer accounts; machines
with different credentials must list one another to cooperate. `report` inherits
that scope through `UB_AGENT_OPERATORS`. No credentials or GitHub permissions change.
Agents using the operator's GitHub credentials can write trusted records themselves;
author filtering is not a security boundary against a compromised agent session.

Ignore markers and records from all other comment authors before parsing them.
Payload fields cannot grant trust. `recorded_by` is allowed only on a mirrored
handoff outcome: both the original actor and publisher must be trusted, and the
comment must be on the handoff PR. A lease or reset cannot impersonate another
actor through `recorded_by`. Malformed/contradictory trusted records park their item
visibly without stopping unrelated work. Transport/read failures still stop the
loop; they never become an empty queue.

## Distinct clocks

| YAML setting | Default | Meaning |
|---|---:|---|
| `agent-timeout-minutes` | 180 | Runtime process execution deadline; the lease lasts this plus fifteen minutes |
| `retry-backoff-seconds` | 60 | Initial retry delay after confirmed termination |
| `max-backoff-seconds` | 3600 | Cap on exponential retry delay |
| `max-attempts` | 5 | Durable starts allowed per item/agent |

Every setting can be overridden on an agent. Each GitHub page/request is bounded to 20 seconds; a multi-page discovery
scan may take longer overall. Git operations are bounded to 120 seconds.
Worktree preparation precedes the runtime execution deadline; ownership is checked
again before execution. Default clocks leave ample margin for those reads. Very
short test leases are unsuitable for real network execution.

## Claim election

Read current labels/head and all coordination comments before creating a claim.
Create a separate tentative lease comment for each contender, then reread. The
lowest GitHub comment ID among unexpired live leases wins. A loser edits only its
own tentative comment to withdraw and never starts a runtime. The winner marks its
record running before execution. The lease is never renewed: it lasts the run's
timeout plus a fixed grace for setup and completion, so a dead launcher's claim
expires on its own.

Every lease names the actor, agent, run, configured CLI/provider/model/effort,
expiry, attempt, input candidate SHA, and branch when known. The claim is tied to
the observed candidate; a changed head or vanished trigger before execution fails
the assignment. Branches created for private issue worktrees are recorded on the
lease and retained for possible human recovery.

This election is tested with concurrent contenders, but GitHub comment reads and
writes are not compare-and-swap. It does **not** establish exactly-once execution,
strict consistency, instantaneous loss detection, or write fencing. Other launchers
and runtimes must obey the contract. Clocks must be reasonably synchronized.

Expired leases are never resurrected. Ownership is reread before every durable
write; a failed ownership read stops the run, and the launcher does not report,
accept, or release after losing ownership. Between observations, an agent with
GitHub credentials can still write: comments cannot prevent this.

## Draft checkpoints

The starter implementer publishes the first coherent, buildable checkpoint as a
draft PR whose body starts `Closes #N`, then pushes meaningful checkpoints to the
same branch and PR. Checkpoints are not outcomes: no `ub-agent report`, issue
label changes, or workflow trigger labels on the draft. The issue lease remains
live for the whole run. Before each checkpoint push and before marking the PR
ready, read new issue comments and PR comments, reviews, and inline feedback;
follow them or reply explaining the decision. An unresolved human decision leaves
the PR as a draft and is reported as blocked.

At completion, pass the project checks, push the final work, mark the same PR ready
(`gh pr ready`), and report `--outcome handed-off` with the PR handoff. The runner
removes the issue triggers and adds `needs-review` to that same PR. Drafts never
receive `needs-review` or `ready-to-merge` from the starter workflow. Feedback after
readiness follows the normal review and `needs-changes` path.

A retried issue run starts in a fresh worktree on a new branch. The launcher does
not track earlier drafts; the implementer instructions tell the agent to look for
an open draft PR that links the issue, continue it on its branch, and never open a
second implementation PR. Branches recorded on earlier leases are retained for
human recovery.

## Explicit outcomes

`ub-agent report` uses the supervised environment to verify the run's current
ownership and create one versioned outcome comment. Human summaries lead; JSON is
fenced in `json` blocks. Arbitrary prose is never parsed for routing.

A declared outcome is reported with `--outcome NAME` and has status `success`.
`--status retry|blocked` retains its failure meaning and changes no labels. Agents
with declarations cannot report `--status success`; undeclared names are rejected.
The runner also blocks records that bypass reporting validation. Agents without
`outcomes` keep `--status success` and their own label changes.

A report is initially `accepted: false`. On confirmed process/group termination,
the launcher rereads GitHub and validates ownership, exact reported candidate SHA,
and the original issue link for an explicit PR handoff. A handoff to a draft PR
blocks in normal completion and before transition start in recovery. For legacy agents it checks
consumed triggers or a closed assignment; their valid issue-to-PR handoff may leave
an issue trigger present.

For declared outcomes, the running lease snapshots allowed outcomes and their
resolved transitions. The outcome names its declaration and stores `add`, `remove`
(including all agent triggers), `triggers`, `stop_labels` and a `started` flag. It
must match that run's declaration. Candidate edits or a restarted launcher with
different configuration cannot replace the recorded changes.

Before starting the transition, reread the assignment and handoff PR. A missing
assignment trigger blocks with no label changes. A stop label on either item blocks
as paused, leaves the outcome unaccepted, and records a durable rejection. That
outcome can never be applied later, including if the runner crashes before releasing
the blocked lease. After unpausing, a person sets the labels they want or uses
`ub-agent retry` to rerun the role. The starter preparer's needs-human handoff and
integrator's maintainer-merge handoff are declared successful human gates.

Persist `started: true` before the first label mutation. Remove every agent trigger
and configured `remove` label from the assignment, and add configured `add` labels
to the handoff PR, or assignment if there is none. Reread labels and apply only
missing changes. Unnamed labels remain unchanged. A stop label added after start
does not interrupt the transition; it remains and parks subsequent pickup. A
transition may itself add a stop label.

After all changes, persist `transition_complete: true`, mark the outcome accepted,
copy provenance to the handoff PR, and release ownership. The launcher applies the
project's declarations; it does not invent transitions, run project checks itself,
or grant merge authority.

Exit zero without an outcome is a protocol failure and a bounded retry. Nonzero
without an explicit retry outcome blocks for operator attention; this includes
unclassified authentication failures/refusals without interpreting stderr prose.
Timeouts and interruptions produce durable retry outcomes after confirmed cleanup.
Explicit blocked outcomes and exhausted budgets require a reasoned operator reset.
The lease's released `result` records the launcher's final verdict; a reported
success with failed validation remains unaccepted and does not establish completion.
If a timeout/interruption follows an early report, reconcile that existing report
and release with the supervision failure; do not post a second outcome or accept
the early report. Known unconfirmed cleanup is marked on the owned lease when
GitHub remains available, preserves its ownership until expiry, and blocks automatic
acceptance/reexecution pending positive termination evidence and an operator reset.
If GitHub is unavailable or ownership is lost, that verdict cannot be guaranteed
durable; no comment protocol eliminates the crash/write-failure window.

## Recovery

An unexpired lease always excludes pickup. Expiry permits a new fresh run, never
conversation resumption. Durable attempts/backoff survive restarts.

Recovery discovery reads all repository issue-comment pages directly at startup,
including records on closed items and items whose trigger was removed. Later polls
read updated/new comments using `sort=updated` and `since`, with a 60-second overlap
and a cursor captured before the scan. Within each scan, advance `since` to one
second before the previous page's last update and deduplicate by comment ID; page
offsets would silently skip records when earlier comments move or disappear.
GitHub exposes second-resolution timestamps and at most 100 comments per page.
If a full page cannot advance the timestamp boundary, stop visibly: that bucket
cannot be paginated safely with this API. Page requests have separate deadlines.
The cache/cursor live only in memory; restart rebuilds them from GitHub. A failed
or ambiguous page commits neither cache changes nor the cursor. Item comments
are always read freshly before planning
or claiming, so deleted/stale cached records do not supply authority. This avoids
indexed search and a full historical scan every poll. Initial large-repository
read cost and sustained polling still need pilot evidence before a cutover.

Discovery follows the latest non-withdrawn lease after the last reset for each
item/agent pair. A released retry/blocked result or expired run without an outcome
stays visible on closed/unlabelled items as blocked for operator inspection; a
missing trigger never authorizes automatic reexecution. Later released success
supersedes older crashed contenders/runs without resetting attempt history.
Failed scans stop visibly rather than become empty queues.

An expired, unfinished lease with an explicit outcome reported within its validity
window gets outcome-only recovery: claim a new bounded recovery assignment,
finish or validate the recorded outcome, accept success or record the blockage,
and release. For a declared outcome, finish its recorded label transition before
acceptance. A started transition has already passed success validation; recovery
checks its declaration against the source lease but does not revalidate the
candidate SHA or issue link. It also skips trigger-present and stop-label checks:
prior mutations may have consumed the trigger or added a human gate. An unstarted
transition still validates the candidate and issue link, checks both items for
pausing and the assignment for its trigger. A durably rejected paused outcome is
never applied later.

Transitions create no cross-item reservations. Assignment removals happen before
handoff or assignment additions. A crash between these steps leaves the consumed
triggers absent and the next trigger unpublished, so those items stay idle unless
another trigger is already present. A crash during removals can leave some triggers
present; existing labels and ordinary live leases still govern pickup. A destination
trigger may be picked up before acceptance and source release; independent roles
still require successfully released provenance for their current candidate.

Outcome-only recovery replays the recorded removals, then additions, and accepts the
outcome without rerunning the role. Removing an absent label or adding a present
one is a no-op. If an assignment label occurs in both lists, it is removed before
being added, so `add` determines its final state on completion and replay.

An operator reset (`ub-agent retry --number N --agent NAME --reason TEXT`) supersedes
the source role's unfinished lease for recovery after it expires. It does not
complete or undo the transition, and the old outcome remains unaccepted. Inspect
both items and restore the desired trigger labels to resume the workflow; no
reservation survives the reset. Restoring a trigger alone does not reset attempts
or clear a blocked result. Do not reset while expecting automatic completion of
the old transition; let outcome-only recovery finish instead.

A PR head or issue-link edit after start does not block completion. Provenance still
names the originally validated SHA: an independent role requiring that provenance
blocks on a newer head, and a person can set the revision label to obtain a new
implementation outcome. Label writes, comments and claim elections remain
cooperative GitHub operations, not atomic transactions.

Do not execute the previous command again. This also repairs a crash between
acceptance and release. The old expired record is not falsely marked as observed
terminated; the recovery claim names the recovered run/lease before any finalization,
so a crash during recovery also recovers the outcome without execution. A failed
acceptance check records a blockage, but unavailable GitHub reads leave the lease
unreleased and outcome pending for validation after expiry. This applies during
normal completion and recovery; repeated recovery claims consume no execution
attempts. Expiry itself is not positive evidence that an old process on another
host has died.

## Runtime independence and candidate provenance

`different-runtime-from: NAME` requires accepted source provenance for the **current
PR head**, backed by a successful source release or successful outcome recovery.
Source issue outcomes are copied to the PR at handoff. A revision records provenance
for its new head. Missing/unaccepted/stale provenance blocks; the rule never falls
back to a guessed author or runtime.

The eligible runtime must have a different CLI, provider, **and** model from the
source. Effort is ignored. Restarting the same author/runtime with a fresh run ID
cannot satisfy this. Configured identity is the contract: use concrete supported
identifiers and declare custom providers honestly; the launcher cannot attest to a
provider's model alias resolution. Codex and Claude built-ins identify OpenAI and
Anthropic respectively; other provider routing uses a custom adapter declaration.

Different model executions may share GitHub authentication. GitHub login is not
agent authorship. Native approvals have separate eligibility rules: GitHub forbids
PR authors from approving their own PRs. Machine outcomes do not count as native
approvals and cannot bypass required reviews or branch protection. Project policy
may impose stricter identity rules in its instructions.

Independent assignments start a fresh CLI session with project instructions and
original task/candidate context. They are directed to original requirements,
acceptance criteria, code/diff and candidate-specific evidence, not implementation
reasoning transcripts. If the assigned head moves, the independent result fails
validation. No earlier-head result automatically satisfies a newer candidate.

## Execution boundaries

Custom argv adapters expand only `{model}` and `{effort}`, including when checking
the command executable for runtime selection. Optional adapter `check` argv is
used only by the read-only `doctor` preflight, never by selection or launch.

Use argv directly; there is no shell interpolation. Runtimes receive a prompt on
stdin; direct commands receive context through `UB_AGENT_CONTEXT` and run identity
through `UB_AGENT_*` variables. Operator environment/auth stores are inherited
normally; no credentials or grants are copied or added. `runtime-args` is the
operator's explicit extension. All sessions start fresh. Task instructions are
snapshotted from the operator's configuration checkout at launcher startup,
including for private PR executions. Candidate edits to those files are changes
to inspect, not replacement policy for the assignment. Shared candidate guidance
is still task input; an independent agent is directed to use the configured task
instructions to judge it.

Each process owns a new POSIX session/group. Termination sends TERM then KILL and
checks that no live owned group members remain. Even failed process inspection
signals the known child group but reports uncertainty. Inconclusive cleanup
preserves artifacts, stops the loop, and leaves expiry as recovery authority. Private
worktree removal applies only to a directory created by this run, with a guard
against path redirection; shared project directories are never removed.

Helpers that detach into a different session/group escape this supervision boundary.
There is no cross-process cwd sweep. A launcher killed with SIGKILL, detached helpers,
or malicious executions cannot be reliably fenced by this small framework; broader
host/runtime evidence and any justified hardening are tracked in issue #2.
