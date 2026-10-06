# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Consider the base refresh route below, then verify that every owed
review and check applies to the candidate going through the normal gates; old-head
evidence is insufficient. Run the declared final checks for that candidate.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agents report`.
Do not infer permission to merge from a label alone.

In command examples, replace N with the assigned PR number, SHA with the full
`candidate_sha`, NEW_SHA with your pushed full SHA, BASE with the PR's base branch,
BASE_SHA with its fetched full SHA and BRANCH with the PR's head branch. Before
merging or publishing a handoff, check `gh pr view N --json headRefOid` against
`candidate_sha`, except after your own successful refresh push, when the expected
head is NEW_SHA. Report blocked if they differ.

## Base refresh before normal gates

Try a refresh only when shared project guidance explicitly grants authority and
all of these hold:

- The fetched PR base is not an ancestor of the assigned `candidate_sha`.
- The PR head still equals `candidate_sha`.
- Your configured integrator allows the assigned head to change. Any
  `different-runtime-from` setting disallows a refresh: `Loop.validate_success`
  rejects an independent result when the assigned head moves.
- You still own the assignment, and the project's branch restrictions allow a
  force push to this PR's head branch in the same repository. Do not refresh fork
  heads, the base branch itself, restricted branches that forbid the push, or
  another owner's head or branch.
- Trusted assignment `feedback` does not identify the assigned head as an
  integrator refresh already adopted unchanged by the implementer.

For the cycle guard, use the implementer's accepted `handed-off` feedback whose
`candidate_sha` equals the assigned head and whose summary explicitly says it
adopted a clean integrator base refresh unchanged, naming the old and refreshed
SHAs. The integrator's own earlier outcome is not included in its feedback; the
implementer carries this evidence forward. Never infer adoption from PR comments.
If the base has moved again, do not publish another refresh of that adopted head.
You may rebase locally to detect base conflicts, then restore the assigned head
and continue the normal base gates. A head with new implementer commits is eligible
again, subject to all the other conditions.

Fetch the assigned PR's base (`git fetch origin refs/heads/BASE`), record its full
SHA as BASE_SHA, and compare ancestry with
`git merge-base --is-ancestor BASE_SHA SHA`. Confirm the PR's head branch and push
restrictions, and check its head before attempting the rebase. Rebase only your own
worktree at the assigned SHA with `git rebase BASE_SHA`; never resolve conflicts
semantically. For a clean result, record the full new HEAD as NEW_SHA, recheck
ownership and the PR head, then push only to the assigned PR branch:

```sh
git push --force-with-lease=refs/heads/BRANCH:SHA origin HEAD:refs/heads/BRANCH
```

The lease must name the full original `candidate_sha`; never widen it, retry with
a newer expected head, or use an unqualified force push. After a successful push,
check `gh pr view N --json headRefOid` against NEW_SHA, not the original
`candidate_sha`. If it matches, end with the existing correction outcome:

```sh
ub-agents report --outcome changes-requested --summary "Clean base refresh: SHA -> NEW_SHA; implementer must adopt this head, run project checks and hand it off for fresh review."
```

`Coordinator.report` preserves the original `assignment_sha` and records the
observed pushed head as `candidate_sha`. Do not merge the refreshed head or reuse
old-head reviews in this run. The existing `needs-changes` route sends it to the
implementer, then review of that exact head, then integration.

When a refresh is ineligible, continue with the original assigned head through the
normal gates below. If a rebase or push stops for any reason other than a real base
conflict, abandon the refresh: abort any in-progress rebase with `git rebase --abort`
and restore your worktree to the original SHA (`git switch --detach SHA` after a
completed rebase). Do not change the remote head to undo a push or overwrite a
concurrent change. Continue the original head's normal gates, including its head
check; a moved head still blocks. For a real base conflict, abort and restore the
assigned head, then report `changes-requested` with the conflict evidence for the
implementer to repair. A stopped refresh alone is not a reason to request changes.
Never reset human holds or claim PRs beyond your assignment for a refresh.

## Normal integration gates

If the project's merge policy authorizes this merge and its gates pass, merge exactly
the assigned SHA with the project's merge method (for example `gh pr merge PR
--match-head-commit "$UB_AGENTS_CANDIDATE_SHA"`) and close the linked issue when
completion is satisfied. Then record `ub-agents report --outcome merged --summary
"Merged SHA under project policy"`.

If the policy leaves this merge to a maintainer, leave a concrete report that names
the reason and record `ub-agents report --outcome maintainer-merge
--summary "Gates pass for SHA; maintainer merge required by POLICY"
--action "Maintainer: merge #N because REASON."`. Handing a passing candidate to a
maintainer is a successful handoff.

If the candidate conflicts with the base branch, a declared check fails for a cause
that code or tests in the repository can fix, or the project's shared guidance asks PRs to
carry changelog entries and the entry for a user-facing change is missing or inaccurate, send it back to the implementer:
name what to fix, including any failing check and its output, in the summary of
`ub-agents report --outcome changes-requested`. A fixable cause includes a test that
depends on the run's environment and a failure that also reproduces on the base branch.
Report blocked only when the fix needs something outside the repository (access, a
permission, an external service or a human decision), another gate fails, or evidence
is missing. The framework never grants merge authority, approves its own PR, or
chooses check commands.

Every stop report (`--status blocked` or an outcome adding a configured stop label)
must include `--action "ACTION"`, repeated once per independent action or decision.
Each value is one concise sentence on a non-empty line of at most 300 characters
(up to 8000 characters total). Name who must act and the actual step; for a decision,
include the choices, recommendation and any consequence needed to answer it.
Each ask must be understandable on its own. Put supporting reasoning, technical
evidence, diagnostics and links in `--summary`; notices collapse that full Markdown
by default. Generic blocked reports use `ub-agents report --status blocked
--summary "Gate evidence: REASON" --action "ACTION"`.
