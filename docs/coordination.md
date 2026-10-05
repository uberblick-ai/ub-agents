# Coordination contract

This is a cooperative serial launcher, not a distributed lock service. GitHub is
durable truth; `.ub-agents/` is disposable. The project is trusted executable
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
  `ub-agents status` reports this count in its existing `attempts` field.
- A successful outcome's transition removes the agent's triggers, so an item runs
  again only when a human reapplies one. An accepted success resets that agent's
  count on that item, including a resumed or revised PR and outcome-only recovery.
- `ub-agents retry N --reason TEXT [--agent NAME]` resets that count to 0 and
  clears its failure block and backoff, preserving history. Approval parking still
  requires maintainer approval. It refuses a live lease
  and never revokes someone else's run. It does not restore workflow labels.
  Its two output lines link the reset record and explain the next step from the
  item's current state and labels: closed first, then present stop labels, then
  missing triggers, otherwise pickup by a running launcher on its next poll.
  If stop labels are present and triggers are missing, both need attention.
  Use `ub-agents status` for progress and other pickup gates.

| How a run ends | Count | Afterwards |
|---|---|---|
| Accepted success at completion or through outcome-only recovery | Reset to 0 | Normal transition |
| Approval fails at pickup or after claiming | Unchanged | Parked until approved, without `retry` |
| Operator interrupt with confirmed cleanup | Unchanged | Eligible on the next launch, without backoff |
| Agent reports `--status blocked`; transition paused by a stop label or a vanished trigger | Unchanged | Parked for a human; not retried automatically |
| Crash before a report (expired lease without an outcome), timeout, exit without a report (zero or nonzero), `--status retry`, launcher setup failure | +1 | Retried with backoff until `max-attempts` consecutive failures |
| Invalid or rejected success report, unconfirmed cleanup, or unclassified failure | +1 | Parked for a human; never retried automatically |

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

Every lease records an `attempt_effect` (`pending`, `failure`, `reset` or `unchanged`)
so restart discovery distinguishes human pauses and interrupts from failures.

## Selection order

1. Eligibility first: live claims, stop labels, retry backoff, attempt limits and
   [maintainer starts and outside-input approval](approvals.md) when approvals are on. Every issue run
   needs a start; outside PRs also need an eligible head. Trusted PRs need no start.
   Outside feedback suspends outside PRs, while issues and trusted PRs only exclude
   uncleared feedback.
2. PR work before new issue starts: PR assignments, recovery and completion of
   already-started runs. Neither queue gate holds back this work, and milestone
   ordering does not change its rank.
3. New issues pass the dependency gate and, in milestone `gate` mode, the milestone
   gate, as the [queue reference](configuration.md#queue) defines them. Planning
   and a fresh claim-time read both enforce each gate. Approval parking also
   rechecks them. In milestone `order` mode, milestones never gate eligibility.
4. In milestone `order` mode, new issues rank first by open milestones with open
   items, oldest creation time and then milestone number first. Unmilestoned
   issues and issues whose milestone is outside that list rank last. Open local
   blockers inherit their dependents' earliest milestone, directly or transitively,
   in dependency `wait` mode, even without priority labels.
5. Within PR work and within each issue milestone rank: effective priority (the
   item's own label, inherited from open local dependents, or for a PR from the
   open issues it closes), then item creation time, then item number. With milestone
   `gate` or `ignore`, new issues use this priority order without milestone ranks.
   Agents on the same item keep YAML order. The launcher never changes priority labels.

`ub-agents status` lists rows in this order with each item's effective priority and
its source, adds each issue's milestone and inherited source in `order` mode, and
names what a waiting issue waits for, including `Waiting for active milestone #N`
in `gate` mode. Concurrent launchers rank the
GitHub state each observes and try claims in that order; existing claims resolve
contention, and there is no global order across machines. Dependency reads skip
issues whose list summary reliably reports zero blockers; a failed read stops
selection rather than becoming an empty list. An unreadable milestone list also
stops selection. Gate rechecks are cooperative observation, not an atomic
snapshot; milestone `order` requires no claim-time or approval-parking milestone
recheck.

## Assignment input

The top-level `approvals` setting defaults to on for public repositories and off
for private and internal repositories. Each discovery pass resolves the unset
default from GitHub visibility; an unreadable visibility fails before claims or
parking writes. With approvals off, a configured trigger label starts work
regardless of who applied it, current titles and bodies are input, and feedback
is limited to authors with `write`, `maintain` or `admin`. No approval record can
clear other feedback. PR heads need no approval, and there are no approval reads
or approval-parking writes. Other eligibility gates and stop labels still apply.
See the [approval policy](approvals.md) for details.

With approvals on, checks at pickup park disallowed items with their reason, without claims
or attempts. When approval is the only obstacle for an open, triggered item,
`launch` adds the project's configured stop label (`needs-human` in the starter)
and posts one **Action needed** notice. `status` stays read-only. Unreadable
approval history or input that changes during the read is retried on the next
poll without label or notice writes. Uncleared outside comments do not suspend
issues or trusted-authored PRs.

| Approval gate | Maintainer action to resume (starter labels) |
|---|---|
| No maintainer start | Remove `needs-human` and re-apply a trigger label. |
| Outside title/body edit or outside PR feedback after approval | Re-apply a trigger label, or run `ub-agents approve N`; then remove `needs-human`. |
| Outside PR head not approved | Run `ub-agents approve N` or submit an approving review of the current head; then remove `needs-human`. A trigger label does not approve a head. |

Each gate state gets the stop label and notice at most once. Later polls of the
same unresolved gate add nothing; a new gate after resuming parks the item again.
These writes are advisory: a failure is logged, never writes coordination records
or changes attempt counts, and cannot cause a claim. A later claim minimizes the
notice using the same launcher notice marker as other Action needed notices.

With approvals on, after winning a claim, the launcher rereads and validates input.
A failed check withdraws that claim before execution, preserves attempts and returns
the item to parked; approval makes it eligible without `ub-agents retry`. Preparation
uses the same gate: a maintainer's `needs-preparation` starts it, and the preparer's
rewrite is trusted.

Context holds the post-claim title, body and trusted comments; approvals on can also
include cleared outside comments.
PR context adds the assigned head, reviews and review comments. Uncleared and
later-edited outside feedback, coordination records, launcher notices and approval records are
excluded. The prompt makes this snapshot the assignment input; other GitHub
comments are not input. Outside changes during execution do not stop that run,
replace its context or gate its durable completion and recovery.

Context also includes `feedback`: accepted outcome summaries from other agents on
the assigned item and, for an issue, its handoff PRs. Each entry contains `agent`,
`outcome`, `summary`, `candidate_sha` (or null) and `created` (UTC time). On each
item, only outcomes recorded after the receiving agent's latest accepted outcome
are included; its handoff copy counts as its own outcome. Without an own accepted
outcome there, all other agents' accepted outcomes are included. Outcomes appear
once, in comment record order, even when copied to a handoff PR. Coordination records
from every trusted launcher account supply this input. Revisions must address
`feedback` alongside comments and reviews.
This field does not change approval requirements.

With approvals on, an outside PR head requires a valid pinned approval record, a maintainer approving
review on that head, or an accepted agent outcome from an eligible assignment head
when the head repository is the base repository. Changed fork heads need explicit
maintainer approval, even if an accepted successful run observed them.
A trigger label alone cannot identify a pushed head. Fork PR review is supported;
agent revision of a fork PR remains blocked regardless of the approvals setting.
`ub-agents approve N` accepts
issues and PRs and clears the outside feedback it records; PR records also pin the
head. Its records have no effect with approvals off. See the [complete PR rules](approvals.md#pull-requests).

## Trusted comments

Lease, outcome and retry-reset comments start with one readable line naming the
state, agent, runtime, short candidate SHA when available, and a short summary.
The full JSON record follows inside a collapsed `<details>` block headed
**Coordination record**, with the marker `<!-- ub-agents:v3 -->`. The parser reads
only v3 JSON, including on comments minimized by GitHub. Every lease requires
`attempt_effect`, `declared_triggers` and `stop_labels`; declarations and outcome
transitions use the compact shape described below. Malformed v3 records block
their item.

Stop every launcher for a project and upgrade them together before restarting.

After releasing a run, the launcher minimizes its own lease and outcome comments
from superseded runs of the same agent on that item (and copied outcomes on a
handoff PR), using GitHub's `OUTDATED` classifier. The latest lease and latest
outcome for each agent stay expanded under this rule.

When a released run's accepted outcome hands off a candidate SHA, the launcher also
minimizes every agent's earlier lease and outcome comments on the handoff item
whose displayed SHA differs. The displayed SHA is `candidate_sha`, falling back
to `assignment_sha`. Records for that candidate, records without a SHA, the run
that handed it off, post-claim approval withdrawals, and records linked by the
latest Action needed notice are excluded from this candidate-based minimization.
Comments written after the handoff outcome stay expanded. A lease withdrawn after
losing either the cooperative claim election or the shared-branch election is
minimized immediately; a post-claim approval withdrawal stays expanded to explain
why the item is parked.

Minimizing preserves every record and its authority, feedback and attempt counts;
it never deletes history or resets attempts. Failures are logged as advisory.
The launcher reads minimization state through GraphQL in batches before sending
mutations, so later releases and claims skip comments already minimized. REST
comment reads still supply the coordination records.

Launchers on different GitHub accounts share the same queue. By default, coordination
records count when the comment author's current repository role is `write`, `maintain`
or `admin`. Optional `launchers: [bot-a, alice]` narrows trust to those accounts,
matched case-insensitively; listed accounts must still have `write` or higher.
Any account in this trusted set, including humans with write access, can post
records that count. This adds little authority because those humans can already
change labels and push.

Role reads for record authors share the existing permission read once per account
per discovery pass. Coordination reads check only authors of marked records;
unlisted authors are ignored without a role read. Roles are judged at read time,
even when the item has not changed. Stop an account's launcher before demoting it
or removing it from `launchers`: revocation excludes all its records, including
open leases. A launcher claims nothing while its own account is untrusted and
prints the reason. A failed or unreadable author role read stops the pass rather
than silently discarding a possible owner; only a definite role below `write`
excludes that author's records. Untrusted records are ignored before parsing, and
payload fields cannot grant trust: the actor is always the comment author.
Agents using the operator's GitHub credentials can write trusted records
themselves; author filtering is not a security boundary against a compromised
agent session. Malformed or contradictory trusted records park their item
visibly without stopping unrelated work. Transport and read failures still stop
the loop; they never become an empty queue.

Use the same `launchers` setting on every machine so all launchers agree on the
trusted set.

## Human action and launch output

A released `blocked` run, or an accepted outcome whose completed transition adds
a configured stop label, posts one short **Action needed** comment. It gives the
release or outcome reason, the recorded candidate SHA, links to the claim and
outcome, and, on a PR, its review decision and the CI rollup for that exact SHA.
If the PR head has moved, the current head's review decision is not attributed to
the old candidate. Unavailable evidence is identified in the comment.

For a stop-label outcome, the comment names the stop label to remove and the
agent's triggers to apply to resume work. For a blocked release, it gives an
exact `ub-agents retry N --agent NAME --reason "Human resolved the blocker"`
command and reminds the human to restore a matching trigger and remove stop
labels. Exits without an agent report, zero or nonzero, retry with backoff; they
post a notice only when they exhaust `max-attempts` and park. That notice also
names the launcher host and run log directory. When a transition parks a handoff
PR, the notice is posted on that PR.

These notices carry a separate `ub-agents:action-needed` marker and are not
coordination records: they never affect routing authority, verdicts or attempt
counts. A later claim by any agent on the item, or an explicit retry
reset, minimizes its earlier Action needed notices. Minimization, notice posts
and evidence reads are advisory: a failure is logged and does not change the
durable result. A failed notice post is not retried on each poll.
Notice deduplication considers all trusted authors. Simultaneous posters elect
the lowest comment ID; each loser removes only its own newly posted advisory
duplicate, preserving every coordination record. Duplicate removal is advisory too.

Within one `ub-agents launch` session, an unchanged blocked or parked item is
printed once. A change to its state or reason prints it again. Stop-label outcomes
remain visible as parked even when their transition consumed every trigger.

## Distinct clocks

| YAML setting | Default | Meaning |
|---|---:|---|
| `agent-timeout-minutes` | 180 | Runtime process execution deadline; independent of the fixed 30-minute lease |
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
record running before execution. Each lease lasts 30 minutes and the supervising
launcher renews it every 10 minutes while it owns the work, including setup,
execution, completion, label transitions, rate-limit waits and the cleanup hook.
Recovery claims renew the same way. These intervals cannot be configured; the
agent timeout and cleanup timeout do not change the lease length. A dead launcher's
claim expires within 30 minutes of its last renewal. The supervisor also stops a
run whose last confirmed expiry has passed by wall clock, including after sleep.

Renewal edits the existing comment, moving only `expires` forward after the usual
ETag-cached ownership read. Lease state edits and renewal share a serialized writer,
so renewal preserves release, withdrawal, cleanup uncertainty and state updates,
and other edits retain a renewed expiry. Release and withdrawal expire immediately
and are never renewed. A failed renewal leaves execution running and retries at
the next interval; ownership is lost if another claim owns the item or the last
confirmed expiry passes. A renewal write starts only with enough time remaining
for its 20-second request timeout, so a late renewal cannot revive an expired claim.
If the renewal worker stops unexpectedly, the launcher reports the failure in its
terminal output and `.ub-agents/launch.log`; supervision keeps the last confirmed expiry.

Launchers from the preceding release honour renewed comment expiries. New launchers
also honour older claims' recorded, longer expiries; no coordinated upgrade is
needed for this lease change.

Every lease names the actor, agent, run, configured CLI/model/effort,
expiry, attempt, input candidate SHA, and branch when known. The claim is tied to
the observed candidate. A changed head, closed item or vanished trigger found
after setup but before execution counts as a launcher setup failure: +1 with
bounded backoff. The next run uses the current head once the item is open and a
trigger matches again. A stop label found before execution instead parks the item
without changing its count; removing it still requires `ub-agents retry` to clear
that parked state. A vanished trigger during success transition validation also
parks the item without changing its count, as the outcome table above describes.
Branches created for private issue worktrees are recorded on the lease and
retained for possible human recovery.

This election is tested with concurrent contenders, but GitHub comment reads and
writes are not compare-and-swap. It does **not** establish exactly-once execution,
strict consistency, instantaneous loss detection, or write fencing. Other launchers
and runtimes must obey the contract. Clocks must be reasonably synchronized.

Expired leases are never resurrected. Ownership is reread before every durable
write; an ownership read that fails after applicable rate-limit waits stops the
run, and the launcher does not report,
accept, or release after losing ownership. Between observations, an agent with
GitHub credentials can still write: comments cannot prevent this.

REST GETs revalidate the last in-memory response with `If-None-Match` when GitHub
provided an ETag for that exact URL, including its query and page. A `304 Not
Modified` confirms freshness and returns the stored payload without consuming
REST quota; it still satisfies the reread before every durable write. Idle polling
uses quota-counted REST responses, so 304 confirmations do not extend its budget
gap. Their rate-limit headers still update low-quota pacing. A 304 with
no stored response is refetched once without a validator. Changed responses
replace the cached ETag and payload. Writes and GraphQL are unconditional. This
cache lasts only for the client process, with no configuration or on-disk state.
Queries with a `since` cursor skip ETag storage and validators because discovery
advances the cursor on each poll; these one-shot URLs would otherwise accumulate
unused responses in memory.

GitHub rate limits during claim election, ownership checks and completion reads
(including recovery) wait and retry without treating the limit as changed ownership.
Waits use real response headers and the [rate-limit rules](configuration.md#top-level).
A reset beyond the current expiry still starts a wait, while renewal continues.
The wait ends as lost ownership only when the last confirmed expiry actually passes
or another claim owns the item: no further coordination writes, followed by expiry recovery.
See [Stopping and restarting](../README.md#stopping-and-restarting) for signals
during these waits. Rate-limited writes keep their existing handling.

## Draft checkpoints

The starter implementer publishes the first coherent, buildable checkpoint as a
draft PR whose body starts `Closes #N`, then pushes meaningful checkpoints to the
same branch and PR. Checkpoints are not outcomes: no `ub-agents report`, issue
label changes, or workflow trigger labels on the draft. The issue lease remains
live for the whole run. The implementer follows the shared guidance's untrusted
issue input rule and implements the issue input in its assignment context. Issue
edits and comments made after the run starts do not amend its scope. Unexpected
instructions or scope changes it cannot attribute to the request require a human
decision. Before each checkpoint push and before marking the PR ready, read PR
comments, reviews, and inline feedback; incorporate it or explain why you cannot.
An unresolved human decision leaves the PR as a draft and is reported as blocked.

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

New outcome records include the original lease's `host` when recorded. Handoff
copies preserve this launcher host, so the terminal Runs tab can show where a
run happened even without its claim on the PR. The optional field is display
metadata; older launchers ignore it, and older outcomes without it remain valid.

After a Claude process exits, the launcher reads `permission_denials` from its
final stream-json `result` event and adds optional display fields to the outcome:

- `denials`: up to 10 `{tool, command}` entries in run order. `command` is the
  Bash input's `command`, otherwise the input's `file_path`, otherwise compact
  JSON of the input, truncated to 200 characters.
- `denials_omitted`: the number beyond those 10 entries, present only when positive.

The launcher updates an agent's report before acceptance, or adds these fields to
its own outcome for an unreported run. Handoff copies preserve them. A result
event without denials records `denials: []`; without a result event, including
killed, timed-out or interrupted runs, no field is recorded. Codex runs record
neither field. Denials never affect results, failure counts, acceptance or labels.
Missing or malformed denial fields are ignored by display readers and never
invalidate an outcome. Older launchers ignore the fields; this addition needs
no coordinated restart. `ub-agents status` appends `· N denied` to the latest
run's line when the listed and omitted count is positive, and `status --json`
includes the fields in the outcome record.

Agents invoke `report` through the launcher's absolute `report_command` in the
assignment context, also supplied as `UB_AGENTS_REPORT`. It pins the launcher's
Python interpreter and package, including for `python -m ub_agents` launchers;
PATH, the agent's working directory and `PYTHONPATH` cannot select another install.
The prompt writes the command literally and uses it wherever project instructions
say `ub-agents report`. Agents must not report through their worktree's development copy.

`report` uses the supervised environment to verify the run's current
ownership and create one versioned outcome comment. Human summaries lead; JSON is
fenced in `json` blocks inside collapsed details, and arbitrary prose is never parsed for routing. A declared
outcome is reported with `--outcome NAME` and has status `success`;
`--status retry|blocked` changes no labels. Undeclared names are rejected, and the
runner blocks records that bypass reporting validation.
When the supervised lease's marker names a newer record format, reporting fails
with a diagnostic directing the agent to `UB_AGENTS_REPORT`, without interpreting
the newer payload. This diagnostic is available only in releases that include it;
0.1.10 and 0.1.11 keep their previous missing-lease message.

The running lease snapshots the agent's declared outcomes compactly. Each name
maps to the labels to add, or to an object with `add` and `remove` when extra
removals are configured. Removing every declared trigger is implied. The lease
stores that full set once in `declared_triggers`, and stores `stop_labels` once.
Its existing `triggers` field still lists only the trigger labels present at claim
time. For example, these lease fields preserve both declared triggers even when
only `ready` was present:

```json
{
  "triggers": ["ready"],
  "declared_triggers": ["ready", "needs-changes"],
  "stop_labels": ["needs-human"],
  "outcomes": {
    "handed-off": ["needs-review"],
    "cleaned": {"add": ["needs-review"], "remove": ["old"]},
    "maintainer": ["needs-human"]
  }
}
```

The outcome names its declaration and copies only the added labels and any
explicit extra removals into `transition`, together with `started`. Completion
is recorded by the outcome's top-level `transition_complete` flag. For example,
a completed `handed-off` outcome contains these fields:

```json
{
  "outcome": "handed-off",
  "transition": {"add": ["needs-review"], "started": true},
  "transition_complete": true
}
```

The launcher resolves the implied trigger removals and stop labels from the
original lease. The transition must match that lease's declaration, so
configuration edits or a restarted launcher cannot replace the recorded changes.

A report starts `accepted: false`. Once the process group has terminated, the
launcher rereads GitHub and validates ownership, the exact reported candidate SHA,
and for an issue handoff that the PR is ready, links the issue, and that no other
open PR sits on an earlier run branch. It then applies the transition as the
[configuration reference](configuration.md#outcomes-and-transitions) describes:
reread both items, block on a missing trigger or pause on a stop label with a
durable rejection, persist `started: true`, copy the pending outcome to the handoff
PR, remove assignment labels, add destination labels, persist
`transition_complete: true`, accept the outcome, update its handoff copy, and release.

An exit without an outcome, zero or nonzero, increments the consecutive failure
count and retries with backoff until `max-attempts`, then parks. A report made
before a nonzero exit is kept: after confirmed cleanup the launcher validates and
applies it as it would after exit zero, including its count effect, label transition
and handoff provenance. The exit code is logged in the run's `events.jsonl`; agent
output is never interpreted. Invalid or rejected success reports still park
immediately. Timeouts produce durable retry outcomes after confirmed cleanup.
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

An unexpired lease excludes pickup until release or expiry. Expiry permits a new
fresh run on any host, never conversation resumption.
Durable attempts and backoff survive restarts.

At startup, launcher and `status` discovery read repository issue comments updated
within 30 minutes plus seven days. Renewed live leases stay inside that window,
independently of execution and cleanup timeouts. `ub-agents cleanup` still scans
the full repository comment history. Later discovery scans poll updated comments with
`sort=updated` and `since`, a 60-second overlap and a cursor captured before the
scan. Each page advances `since` to one second before its last update and
deduplicates by comment id, because page offsets skip rows when comments move. A
full page within one second cannot be paginated safely and stops visibly. The cache
and cursor live in memory, and a failed page commits neither. Claiming discovery
reuses unchanged per-item reads and evaluates only rows it reaches in rank order;
`status` evaluates every row. Item comments and approval inputs are always reread
before a claim or approval-parking write, so stale cached records never supply
authority. See [poll timing and request budgeting](configuration.md#top-level).

PR shared-branch ownership checks parse `ub-agents/<agent>/<N>/<run>` and read issue
N's full history, independently of the repository window. Branches outside that
pattern have no shared-branch owner.

Within the discovered history, the queue follows the latest non-withdrawn lease
after the last reset for each item and agent. A released retry or blocked result,
or an expired run without an outcome, stays visible as blocked on closed or
unlabelled items; a missing trigger never authorizes reexecution. After a restart,
older records on closed or untriggered items fall outside discovery and no longer
appear in `status` or the launcher queue.
Re-applying a trigger or stop label surfaces an open item again; item evaluation
reads its full history and can still recover it. The remaining loss case is a crash
after a started transition removed its triggers but before outcome acceptance,
with no launcher running for the whole seven days. Re-apply a trigger or stop label
to recover that outcome. A later accepted success supersedes older crashed
runs and resets the consecutive failure count. Failed scans stop visibly.

An expired, unfinished lease whose outcome was reported within its validity window
gets outcome-only recovery: a renewed 30-minute recovery claim that names the recovered run
and lease, finishes or validates the recorded outcome, accepts success or records
the blockage, and releases. Repeated recovery claims cost no attempts, and a crash
during recovery recovers again without execution. A started transition has already
passed validation, so recovery finishes its recorded changes even if the PR head or
issue link changed since; provenance keeps the originally validated SHA, so an
independent role uses the missing-source fallback on a newer head.
An unstarted transition is validated in full, and a durably rejected outcome is
never applied later. Replay treats removing an absent label or adding a present one
as a no-op; a label in both lists ends up added.

Recovery contenders elect the lowest live comment id as usual. Any trusted launcher
account on any host can settle the source attempt after expiry. Source outcome
comments keep their original authors; handoff copies name the recovering account.
A stopped recovery can itself be recovered after its claim expires, without starting
an agent or restoring the original lease's ownership. Unconfirmed cleanup still
blocks fresh execution and artifact removal until termination is established.

Transitions create no cross-item reservations: removals from the assignment precede
additions to the destination, so a crash between them leaves both items idle until
a trigger is present, and ordinary live leases still govern pickup. An operator
reset (`ub-agents retry`) supersedes the source role's unfinished lease once it
expires, without completing or undoing the transition; inspect both items and
restore the desired triggers. Restoring a trigger alone resets neither attempts nor
a blocked result. Expiry is not positive evidence that an old process on another
host has died.

## Runtime independence and candidate provenance

`different-runtime-from: NAME` requires a PR. When an accepted report from `NAME`
identifies the source runtime for the **current PR head**, it must be backed by a
successful source release or successful outcome recovery; invalid source provenance
still blocks. Source issue outcomes are copied to the PR before the handoff's
trigger labels are added, then the same copy is updated on acceptance. A revision
records provenance for its new head.

A started, unfinished handoff for the current head blocks the independent role
until acceptance and successful source release or recovery. A failure while
publishing the pending copy prevents trigger publication. A failure while accepting
the copy leaves the pending record in place; lease expiry alone does not enable the
fallback. Recovery repairs the same comment without executing the source again.
Planning rereads the source outcome and lease, so a rejected report, a failed
supervised verdict or an operator reset that abandons the handoff no longer makes
its stale copy block the fallback.

If no accepted report from `NAME` exists for the current head, the agent uses only
its first configured runtime, without filtering, unless a started handoff is still
pending. This covers missing, rejected, other unaccepted and earlier-head reports.
If that runtime's CLI isn't installed, planning blocks
with `No eligible runtime executable is installed`; later alternatives are not
tried. The launcher does not detect authorship or infer the unknown source runtime.

When the source is identified, the eligible runtime must have a different CLI
**and** model from the source; `codex` and `claude` imply OpenAI and Anthropic.
Effort is ignored. Restarting the
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
stdin; direct commands receive context through `UB_AGENTS_CONTEXT` and run identity
through `UB_AGENTS_*` variables. Operator environment/auth stores are inherited
normally; no credentials or grants are copied or added. `runtime-args` is the
operator's explicit extension. All sessions start fresh. Before claiming each new
agent run, the launcher fetches the repository's default branch from `origin` and
fast-forwards the operator's control checkout (the root holding `ub-agents.yaml`).
It then reloads `ub-agents.yaml`, replans the claim, and validates and rereads the
configured Markdown. Configuration and instruction text stay fixed for that run
and are never cached across runs, including private
PR executions. Candidate edits to those files are changes to inspect, not
replacement policy for the assignment. Shared candidate guidance
is still task input; an independent agent is directed to use the configured task
instructions to judge it.

Refresh happens only between runs, after all of this launcher's supervised agent
processes and cleanup hooks have ended. Durable-outcome recovery starts no agent
and does not refresh. A clean, already-current checkout needs no merge. Refresh
requires the default branch to be checked out, no staged, modified or untracked
non-ignored files, and no local commits absent from `origin`. It never resets,
stashes or switches branches, and refuses to overwrite ignored local files.
After the fast-forward, configuration and instruction paths and text are validated;
invalid configuration or missing, outside-project or unreadable instructions stop
the launcher. Unsafe checkout
state, fetch or fast-forward failures also stop it with a nonzero exit and a
message telling the operator what to fix before restarting. These failures do
not claim an assignment, charge an attempt, or mark work blocked or retrying.

The GitHub read of the default branch is part of pre-claim discovery. Continuous
launch retries its transient failures under the [poll limits](configuration.md#top-level);
Git fetch and local checkout or instruction failures still stop immediately.

`ub-agents.yaml` is reloaded before each new execution claim. If the refreshed
configuration no longer plans the item for that agent, it is not claimed. New
issue worktrees still start from the remote
default branch. PR worktrees retain their exact candidate SHA. Agents continuing
draft checkpoints still fetch and check out their exact heads; refresh never
rebases them. Coordination between two launchers sharing a checkout, or a concurrent
manual cleanup, is outside this serial execution boundary.

See [Stopping and restarting](../README.md#stopping-and-restarting) for signal
handling during execution, recovery and checkout refresh, and for restarting after
code updates. The SIGTERM refresh rule prevents a fast-forward from being killed
partway through updating the control checkout.

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
