# Integrate according to project policy

Read the project's acceptance and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks.
Do not infer permission to merge from a label alone.

Immediately before merging or publishing a handoff, check the head with
`gh pr view N --json headRefOid` and compare it with the assignment context's
`candidate_sha`; report blocked if they differ.

If the project's merge policy authorizes this merge and its gates pass, merge exactly
the assigned SHA with the project's merge method (for example `gh pr merge N
--squash --match-head-commit SHA`) and close the linked issue when
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
