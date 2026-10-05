# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agents report`.
Do not infer permission to merge from a label alone.

For headless Claude, avoid shell expansion (`$VAR`, `${VAR}`, `$?`, `$(...)` or backticks),
even with allowlisted commands; the tool result already shows each command's exit
status. Insert the literal PR number from the assignment
context's `assignment` and the literal full SHA from `candidate_sha` into commands;
do not read them through shell variables. In the examples below, replace N with
that number, SHA with that full SHA and PATH with the literal body-file path before
running the command. Check the head with
`gh pr view N --json headRefOid` as a separate command before
merging or publishing a handoff; compare its output with `candidate_sha` and
report blocked if they differ, except that a clean refresh below ends with
the specified `changes-requested` handoff. Write any GitHub comment with the file-writing tool
to a file in your worktree and publish it with
`gh pr comment N --body-file PATH` as a separate command.
Run `ub-agents report` as its own final command, never chained to the head check,
merge or comment publication. If an action is denied, retry with separate commands
using literal values; if it still fails, report blocked with the evidence instead
of ending without a report.

## Refresh the candidate before final gates

On every integration pickup, freshly fetch the PR's base and head, confirm the
remote head equals the assigned `candidate_sha`, and check whether the current
base is already an ancestor of that head. If so, continue with the normal gates.
If not, proactively try a clean rebase; routine branch maintenance needs no new
human decision. A fetch or a prospective merge test alone is not a PR update.

Push a refresh only when shared project policy permits this narrow operation,
the PR is in the base repository, and your assignment owns that PR branch.
Immediately before pushing, reread the live coordination leases: your run must
still be the unexpired winning owner, with no other live or cleanup-unconfirmed
issue/PR lease on the branch. Do not touch another run's worktree or the operator
checkout. If ownership or write authority cannot be established, do not push.
An integrator configured with `different-runtime-from` must not change its assigned
head: hand refresh to the implementer instead, because the launcher rejects a
changed-head result from that independent assignment.

Use a private checkout at the assigned head. Rebase only a linear PR-only commit
sequence onto the fetched base, with autosquash, rerere and other-ref updates
disabled, cherry-picks reapplied and empty results stopped (for example
`git -c rerere.enabled=false rebase --no-autosquash --no-update-refs
--reapply-cherry-picks --empty=stop BASE_SHA`). Inspect the old/new commit ranges
with `git range-diff` and the resulting diff against the base. Never silently
resolve a conflict, drop a commit, edit implementation or flatten merge commits.
Abort on conflicts, empty/dropped commits or substantive differences, and send
those back to the implementer. Unsupported topology also goes to the implementer.

Immediately recheck the remote head and ownership, then push only the PR branch
with an explicit expected-old-head lease:
`git push --force-with-lease=refs/heads/BRANCH:OLD_SHA origin
HEAD:refs/heads/BRANCH`. Use literal full SHAs and the observed branch name.
A rejected lease is a concurrent update: never retry with a newer expectation
or a blind force push in this run. End with `--status retry` and the race
evidence so the next assignment observes the new head; do not overwrite it.

After a successful push, verify the remote head equals the new local SHA; if
it moved again, report retry rather than a successful refresh. Otherwise record
old head, fetched base and new head, and end this run with
`ub-agents report --outcome changes-requested --summary "Clean base refresh
OLD_SHA -> NEW_SHA onto BASE_SHA; adopt and validate NEW_SHA, then hand off for
fresh review"`. This is an automatic implementer handoff, not a failed rebase
or a request for a person. The launcher records the current remote head as the
outcome's `candidate_sha`; never substitute the new SHA into the old assignment
or merge it in this run. All old-head review and check evidence is stale. The
implementer adopts and validates the pushed head and re-hands it off, restoring
current-head runtime provenance before every owed independent review and final
gate runs again on that exact head. A refresh grants no merge permission.

If project policy disallows the push or a safe refresh cannot be completed,
use `changes-requested` to hand the concrete maintenance task to the implementer;
report blocked only for an unresolved authority/ownership gate. Never reset a
human hold. This rule operates when integration is assigned; it adds no periodic
scanner and does not wake stopped items.

If the project's merge policy authorizes this merge and its gates pass, merge exactly
the assigned SHA with the project's merge method (for example `gh pr merge N
--squash --match-head-commit SHA`) and close the linked issue when
completion is satisfied. Then record `ub-agents report --outcome merged --summary
"Merged SHA under project policy"`.

If the policy leaves this merge to a maintainer, leave a concrete report that names
the reason and record `ub-agents report --outcome maintainer-merge
--summary "Ready for maintainer merge: REASON"`. Handing a passing candidate to a
maintainer is a successful handoff.

If the candidate conflicts with the base branch, or the project keeps a changelog and the
entry for a user-facing change is missing or inaccurate, send it back to the implementer:
name what to fix in the summary of `ub-agents report --outcome changes-requested`.
Report blocked only when another gate fails or evidence is missing. The framework never grants merge authority, approves its own PR, or
chooses check commands.
