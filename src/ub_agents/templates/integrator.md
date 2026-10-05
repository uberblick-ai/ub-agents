# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agents report`.
Do not infer permission to merge from a label alone.

## Refresh the candidate before final gates

On every integration pickup, fetch the PR's base and head. The remote head must
match the assigned `candidate_sha`; a mismatch ends with `--status retry` with the race evidence, not a merge
or a rewrite under the old assignment. If the base is already an ancestor, run
the normal gates. Otherwise proactively attempt a clean rebase when eligible;
routine maintenance needs no new human decision.

A refresh is eligible only with explicit shared project permission, for a
same-repository PR whose remote branch matches the assignment's `branch`, and
without `different-runtime-from` on the integrator. The launcher elected this
run and excludes overlapping issue/PR branch owners. Before a push, read only
its own coordination comment. Resolve the supplied lease id with
`python3 -c 'import os; print(os.environ["UB_AGENTS_LEASE_ID"])'`, then use that
numeric literal in `gh api repos/OWNER/REPO/issues/comments/LEASE_ID --jq .body`, with literal values.
Its v3 lease must match the assignment's run, repository item and branch, still
be `running` and expire in the future. This is a cooperative ownership check,
not write fencing or a substitute for the push lease. If the read cannot be
completed or permission/topology disallows rebasing, skip the refresh and run
normal gates; do not manufacture implementer work or a human hold. A known lost
or expired lease stops all writes under the existing coordination rules.

Use only your private assigned worktree or a private scratch clone at the
assigned head. Leave operator checkouts and other runs' worktrees alone.
Compute `MB` with `git merge-base OLD_SHA BASE_SHA`. Require
`git rev-list --merges MB..OLD_SHA` to be empty before rebasing; never flatten
merge commits. Then use `git -c rerere.enabled=false rebase --no-autosquash
--no-update-refs --reapply-cherry-picks --empty=stop BASE_SHA`, substituting literal
SHAs. Abort on conflicts, empty commits, rejected flags or signing failures;
never resolve conflicts, drop commits or edit implementation in integration.
A failed or unsupported rebase leaves the original PR intact and proceeds to
normal gates. Actual base merge conflicts or other failed gates go back to the
implementer with `changes-requested`, naming the repair.

Compare `git range-diff MB..OLD_SHA BASE_SHA..NEW_SHA` and require equal counts
from `git rev-list --count` for those two ranges. Every old commit must map to
one new commit in the same order, with the same message and patch; only changed
parent/SHA and patch context/line offsets are allowed. Any changed added/removed
code, unmatched commit or ambiguous correspondence aborts the refresh. The
implementer independently repeats this preservation check before adoption.

Do not publish another refresh solely because main advanced while this same
candidate was adopted and reviewed. The current-head feedback marker
`base-refresh-adopted old=OLD_SHA base=BASE_SHA new=NEW_SHA` identifies that cycle
only when NEW_SHA equals the assigned head. Still try the clean rebase locally
when eligible, then discard it and run normal base-freshness gates on the assigned
head. A new substantive implementer commit starts a new cycle. This prevents
endless successful refresh/review handoffs on a busy base without skipping gates.

For the first clean refresh in the cycle, reread the own lease and remote head,
then push only the assigned PR branch using
`git push --force-with-lease=refs/heads/BRANCH:OLD_SHA origin
HEAD:refs/heads/BRANCH`. A rejected lease or a head moving again after the push
ends with `--status retry` and the race evidence; never change the expected SHA
or use a blind force push.
Verify the remote head equals the new local SHA, then end this run with `changes-requested`
and the exact summary prefix
`base-refresh old=OLD_SHA base=BASE_SHA new=NEW_SHA; adopt, verify preservation
and validate, then hand off for fresh review`.

The launcher records the new remote `candidate_sha` while preserving the original
`assignment_sha`. Never replace the old assignment with the new SHA or merge in
this refresh run. All old-head review and check evidence is stale. The implementer
adopts and validates the updated head, explicitly records integrator-produced
refresh provenance, and re-hands it off before fresh independent review and every
owed final gate. A refresh grants no merge authority. Human holds stay intact;
this runs on integration assignments, with no periodic scanner or hold reset.

If the project's merge policy authorizes this merge and its gates pass, merge exactly
the assigned SHA with the project's merge method (for example `gh pr merge PR
--match-head-commit "$UB_AGENTS_CANDIDATE_SHA"`) and close the linked issue when
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
