# Coordination contract (version 1)

This is a cooperative serial launcher, not a distributed lock service. GitHub is
durable truth; `.ub-agent/` is disposable. The project is trusted executable
configuration. Workflow labels, checks, acceptance, and merge authority stay there.

## Assignments and attempts

- A run has a random unique ID and one assignment: repository, issue/PR number,
  configured agent, matched trigger, and (on PRs) the observed head SHA.
- Selection orders open issues/PRs by number, then agents in YAML order. Trigger
  lists mean **any** matching label. A runtime list means alternatives in declared
  order: select the first eligible installed executable and execute it once.
- One live lease excludes every other agent on the same issue/PR. This first
  implementation is serial, including recovery. Different machines cooperate.
- Attempts are scoped to **item number + configured agent**. Every started run
  counts, including successful revisions; PR head/label changes do not reset the
  budget. Issue-to-PR handoff starts the PR's own budget while retaining source
  provenance. A crashed expired tentative claim counts conservatively. A contender
  that confirms withdrawal does not. Outcome-only recovery does not cost a start.
- A completed issue assignment is consumed until `ub-agent retry` explicitly
  resets it. An accepted issue-to-PR handoff suppresses duplicate initial work even
  if its trigger remains on the issue. PRs returning to a matching label run a new
  assignment with the accumulated PR budget. Projects should still remove old labels.
- `ub-agent retry --number N --agent NAME --reason TEXT` posts a durable reset,
  preserving history. It refuses a live lease and never revokes someone else's run.

## Distinct clocks

| YAML setting | Default | Meaning |
|---|---:|---|
| `lease-minutes` | 60 | Ownership validity after successful renewal |
| `renewal-minutes` | 5 | Launcher renewal cadence while executing |
| `agent-timeout-minutes` | 180 | Runtime process execution deadline |
| `retry-backoff-seconds` | 60 | Initial retry delay after confirmed termination |
| `max-backoff-seconds` | 3600 | Cap on exponential retry delay |
| `max-attempts` | 5 | Durable starts allowed per item/agent |

Every setting can be overridden on an agent. Renewal must be less than half the
lease. GitHub requests are bounded to 20 seconds and Git operations to 120 seconds.
Worktree preparation precedes the runtime execution deadline; ownership is checked
again before execution. Default clocks leave ample margin for those reads. Very
short test leases are unsuitable for real network execution.

## Claim election and renewal

Read current labels/head and all coordination comments before creating a claim.
Create a separate tentative lease comment for each contender, then reread. The
lowest GitHub comment ID among unexpired live leases wins. A loser edits only its
own tentative comment to withdraw and never starts a runtime. The winner marks its
record running before execution. Renew **that same comment**, never a timeline of
heartbeat comments. The launcher owns renewal; the model does not.

Every lease names the actor, agent, run, configured CLI/provider/model/effort,
expiry, attempt, input candidate SHA, and branch when known. The claim is tied to
the observed candidate; a changed head or vanished trigger before execution fails
the assignment. Branches created for private issue worktrees are recorded on the
lease and retained for possible human recovery.

This election is tested with concurrent contenders, but GitHub comment reads and
writes are not compare-and-swap. It does **not** establish exactly-once execution,
strict consistency, instantaneous loss detection, or write fencing. Other launchers
and runtimes must obey the contract. Clocks must be reasonably synchronized.

Renewal first verifies ownership and then updates/rechecks it. Expired leases are
never resurrected. A failed ownership read or renewal stops the supervised process
group; the launcher does not report, accept, renew, or release after losing ownership.
Between observations, an agent with GitHub credentials can still write: comments
cannot prevent this. Loss is detected at the renewal cadence or local lease expiry.

## Explicit outcomes

`ub-agent report` uses the supervised environment to verify the run's current
ownership and create one versioned outcome comment. Human summaries lead; JSON is
fenced in `json` blocks. Arbitrary prose is never parsed for routing.

Statuses are `success`, `retry`, and `blocked`. A report is initially `accepted:
false`. On confirmed process/group termination, the launcher rereads GitHub
and verifies successful handoff: consumed labels (or closed item), exact reported
candidate, and issue linkage for an explicit PR handoff. A valid issue-to-PR handoff
can consume an issue assignment while its starting label remains. The launcher
marks the outcome accepted, copies its provenance to the handoff PR, and releases
ownership immediately. It never chooses next labels, runs project checks itself,
or merges by policy of its own.

Exit zero without an outcome is a protocol failure and a bounded retry. Nonzero
without an explicit retry outcome blocks for operator attention; this includes
unclassified authentication failures/refusals without interpreting stderr prose.
Timeouts and interruptions produce durable retry outcomes after confirmed cleanup.
Explicit blocked outcomes and exhausted budgets require a reasoned operator reset.
The lease's released `result` records the launcher's final verdict; a reported
success with failed validation remains unaccepted and does not establish completion.

## Recovery

An unexpired lease always excludes pickup. Expiry permits a new fresh run, never
conversation resumption. Durable attempts/backoff survive restarts.

Recovery discovery reads all repository issue-comment pages directly, including
records on closed items and items whose trigger was removed. This deliberately
naive scan avoids indexing lag and hidden completion. Large repositories may incur
substantial API/read cost; production-scale polling needs pilot evidence before
claiming a cutover. Failed scans stop visibly rather than become empty queues.
An expired run with no outcome and no remaining trigger is shown as blocked for
operator inspection, rather than silently disappearing or blindly reexecuting.

An expired, unfinished lease with an explicit outcome reported within its validity
window gets outcome-only recovery: claim a new bounded recovery assignment,
revalidate GitHub, accept a still-valid success or record the blockage, and release.
Do not execute the previous command again. This also repairs a crash between
acceptance and release. The old expired record is not falsely marked as observed
terminated; the recovery record names the recovered run/lease. Expiry itself is not
positive evidence that an old process on another host has died.

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

Use argv directly; there is no shell interpolation. Runtimes receive a prompt on
stdin; direct commands receive context through `UB_AGENT_CONTEXT` and run identity
through `UB_AGENT_*` variables. Operator environment/auth stores are inherited
normally; no credentials or grants are copied or added. `runtime-args` is the
operator's explicit extension. All sessions start fresh.

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
