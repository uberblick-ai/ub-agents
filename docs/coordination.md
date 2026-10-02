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
- One live lease excludes every other agent on the same issue/PR, and on a PR that
  shares an issue run's branch: while either side is live or its cleanup is
  unconfirmed, the other waits. This first implementation is serial, including
  recovery. Different machines cooperate.
- `max-attempts` limits **consecutive failures** scoped to **item number +
  configured agent**. An issue and its handoff PR have separate counts, as do
  different agents on one item. PR head/label changes alone do not reset a count.
  `ub-agent status` reports this count in its existing `attempts` field.
- A successful outcome's transition removes the agent's triggers, so an item runs
  again only when a human reapplies one. An accepted success resets that agent's
  count on that item, including a resumed or revised PR and outcome-only recovery.
- `ub-agent retry --number N --agent NAME --reason TEXT` resets that count to 0 and
  clears its parked state and backoff, preserving history. It refuses a live lease
  and never revokes someone else's run. It does not restore workflow labels.

| How a run ends | Count | Afterwards |
|---|---|---|
| Accepted success at completion or through outcome-only recovery | Reset to 0 | Normal transition |
| Operator interrupt with confirmed cleanup | Unchanged | Eligible on the next launch, without backoff |
| Agent reports `--status blocked`; transition paused by a stop label or a vanished trigger | Unchanged | Parked for a human; not retried automatically |
| Crash before a report (expired lease without an outcome), timeout, exit 0 without a report, `--status retry`, launcher setup failure | +1 | Retried with backoff until `max-attempts` consecutive failures |
| Invalid or rejected success report, nonzero exit without a report, unconfirmed cleanup, or unclassified failure | +1 | Parked for a human; never retried automatically |

A human pause takes precedence over the rejected-success rule when it is the
reason a transition cannot start. Confirmed claim withdrawals cost nothing.
An expired tentative claim without a report counts as a crash. Outcome-only
recovery claims cost nothing; the source run counts according to its recorded
outcome and the recovery verdict, once, even after repeated recovery crashes.
Pending success validation does not charge a failure before a verdict is known.

Retry backoff doubles with the consecutive failure count, capped at
`max-backoff-seconds`. The first failure after a success or reset waits
`retry-backoff-seconds`. For a crash without a report, the delay starts at lease
expiry; otherwise it starts at release after confirmed cleanup. An interrupt
preserves prior failures but adds no delay of its own.

New leases record an `attempt_effect` (`pending`, `failure`, `reset` or `unchanged`)
so restart discovery distinguishes human pauses and interrupts from failures.
Records written by earlier versions retain their original start-count semantics;
this change does not reclassify them. Use `ub-agent retry` to clear them.

## Selection order

1. Eligibility first: live claims, stop labels, retry backoff and attempt limits.
2. PR work before new issue starts: PR assignments, recovery and completion of
   already-started runs. Neither queue gate holds back this work.
3. New issues pass the milestone gate and the dependency gate when configured, as
   the [queue reference](configuration.md#queue) defines them. Planning and a fresh
   claim-time read both enforce each gate.
4. Within PR work and within issue work: effective priority (the item's own label,
   inherited from open local dependents, or for a PR from the open issues it
   closes), then item creation time, then item number. Agents on the same item
   keep YAML order. The launcher never changes priority labels.

`ub-agent status` lists rows in this order with each item's effective priority and
its source, and names what a waiting issue waits for. Concurrent launchers rank the
GitHub state each observes and try claims in that order; existing claims resolve
contention, and there is no global order across machines. Dependency reads skip
issues whose list summary reliably reports zero blockers; a failed read stops
selection rather than becoming an empty list. Like milestone rechecks, this is
cooperative observation, not an atomic snapshot.

## Trusted comments

Only the authenticated GitHub account's comments supply coordination authority, so
every launcher for a project must authenticate as the same account; launchers on
different accounts would not see each other's claims and could run one item twice.
Markers and records from other comment authors are ignored before parsing, and
payload fields cannot grant trust: the actor is always the comment author.
Agents using the operator's GitHub credentials can write trusted records
themselves; author filtering is not a security boundary against a compromised
agent session. Malformed or contradictory trusted records park their item
visibly without stopping unrelated work. Transport and read failures still stop
the loop; they never become an empty queue.

## Distinct clocks

| YAML setting | Default | Meaning |
|---|---:|---|
| `agent-timeout-minutes` | 180 | Runtime process execution deadline; the lease lasts this plus fifteen minutes and any cleanup hook timeout |
| `retry-backoff-seconds` | 60 | Initial retry delay after failure release or unreported lease expiry |
| `max-backoff-seconds` | 3600 | Cap on exponential retry delay |
| `max-attempts` | 5 | Consecutive failures allowed per item/agent before pickup stops |

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
timeout plus a fixed grace for setup, the cleanup hook and completion, so a dead
launcher's claim expires on its own. The supervisor also stops a run whose lease has expired by
wall clock, since monotonic timers pause while a machine sleeps.

Every lease names the actor, agent, run, configured CLI/model/effort,
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

A retried issue run starts in a fresh worktree on a new branch. The assignment
context lists the branches recorded by the issue's earlier runs as
`earlier_branches`; the implementer instructions tell the agent to check each for
an open draft PR, continue it on its branch, and never open a second
implementation PR. At handoff the launcher rejects a success while another open PR
sits on one of those branches, so a duplicate is blocked for inspection rather
than accepted. While a run owns such a PR the issue waits, and while an issue run is
live the PR waits, so one agent at a time touches the branch; the lowest live
comment id wins a simultaneous claim. Earlier branches are retained for human
recovery.

## Explicit outcomes

`ub-agent report` uses the supervised environment to verify the run's current
ownership and create one versioned outcome comment. Human summaries lead; JSON is
fenced in `json` blocks, and arbitrary prose is never parsed for routing. A declared
outcome is reported with `--outcome NAME` and has status `success`;
`--status retry|blocked` changes no labels. Undeclared names are rejected, and the
runner blocks records that bypass reporting validation.

The running lease snapshots the agent's declared outcomes with their resolved
`add` and `remove` lists (`remove` includes every trigger), `triggers` and
`stop_labels`. The outcome names its declaration, copies that transition and carries
a `started` flag; it must match the run's declaration, so configuration edits or a
restarted launcher cannot replace the recorded changes.

A report starts `accepted: false`. Once the process group has terminated, the
launcher rereads GitHub and validates ownership, the exact reported candidate SHA,
and for an issue handoff that the PR is ready, links the issue, and that no other
open PR sits on an earlier run branch. It then applies the transition as the
[configuration reference](configuration.md#outcomes-and-transitions) describes:
reread both items, block on a missing trigger or pause on a stop label with a
durable rejection, persist `started: true`, remove assignment labels, add
destination labels, persist `transition_complete: true`, accept the outcome, copy
it to the handoff PR, and release.

Exit zero without an outcome is a protocol failure and a bounded retry. Nonzero
without an explicit retry outcome blocks for operator attention; stderr prose is
never interpreted. Timeouts produce durable retry outcomes after confirmed cleanup.
Interrupts release with a retry verdict but preserve the failure count and add no
backoff. Explicit blocked outcomes and exhausted budgets require a
reasoned operator reset. The lease's released `result` records the launcher's final
verdict; a reported success with failed validation remains unaccepted. After
confirmed cleanup, a nonsuccess verdict and its count effect are persisted before
reporting or releasing, so a crash in that window cannot promote an early success
or lose the interrupt classification. The live lease still excludes pickup until
release or expiry. If a timeout
or interruption follows an early report, the launcher reconciles that report and
releases with the supervision failure rather than posting a second outcome or
accepting the early one. Unconfirmed cleanup is marked on the owned lease when
GitHub is available, keeps ownership until expiry, and blocks automatic acceptance
or reexecution pending an operator reset. If GitHub is unavailable or ownership is
lost, no comment protocol can make that verdict durable.

## Recovery

An unexpired lease always excludes pickup. Expiry permits a new fresh run, never
conversation resumption. Durable attempts and backoff survive restarts.

Discovery reads every repository issue comment at startup, including records on
closed items and items whose trigger was removed, then polls updated comments with
`sort=updated` and `since`, a 60-second overlap and a cursor captured before the
scan. Each page advances `since` to one second before its last update and
deduplicates by comment id, because page offsets skip rows when comments move. A
full page within one second cannot be paginated safely and stops visibly. The cache
and cursor live in memory, a failed page commits neither, and item comments are
always reread before planning or claiming, so stale cached records never supply
authority.

Discovery follows the latest non-withdrawn lease after the last reset for each
item and agent. A released retry or blocked result, or an expired run without an
outcome, stays visible as blocked on closed or unlabelled items; a missing trigger
never authorizes reexecution. A later accepted success supersedes older crashed
runs and resets the consecutive failure count. Failed scans stop visibly.

An expired, unfinished lease whose outcome was reported within its validity window
gets outcome-only recovery: a bounded recovery claim that names the recovered run
and lease, finishes or validates the recorded outcome, accepts success or records
the blockage, and releases. Repeated recovery claims cost no attempts, and a crash
during recovery recovers again without execution. A started transition has already
passed validation, so recovery finishes its recorded changes even if the PR head or
issue link changed since; provenance keeps the originally validated SHA, so an
independent role blocks on a newer head until a new implementation outcome exists.
An unstarted transition is validated in full, and a durably rejected outcome is
never applied later. Replay treats removing an absent label or adding a present one
as a no-op; a label in both lists ends up added.

Transitions create no cross-item reservations: removals from the assignment precede
additions to the destination, so a crash between them leaves both items idle until
a trigger is present, and ordinary live leases still govern pickup. An operator
reset (`ub-agent retry`) supersedes the source role's unfinished lease once it
expires, without completing or undoing the transition; inspect both items and
restore the desired triggers. Restoring a trigger alone resets neither attempts nor
a blocked result. Expiry is not positive evidence that an old process on another
host has died.

## Runtime independence and candidate provenance

`different-runtime-from: NAME` requires accepted source provenance for the **current
PR head**, backed by a successful source release or successful outcome recovery.
Source issue outcomes are copied to the PR at handoff. A revision records provenance
for its new head. Missing/unaccepted/stale provenance blocks; the rule never falls
back to a guessed author or runtime.

The eligible runtime must have a different CLI **and** model from the source;
`codex` and `claude` imply OpenAI and Anthropic. Effort is ignored. Restarting the
same author/runtime with a fresh run ID cannot satisfy this. Configured identity is
the contract: use concrete model identifiers, since the launcher cannot attest to a
provider's alias resolution.

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
