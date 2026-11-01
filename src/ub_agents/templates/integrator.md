# Integrate according to project policy

Read the project's acceptance and merge rules. Every owed review must apply to the
assigned candidate SHA; only your own clean base merge keeps it valid at a new
head. A label alone grants no merge authority.

Before changing the PR branch, check `gh pr view N --json headRefOid` against the
assignment context's `candidate_sha`; report blocked if they differ. Fetch the
base branch and check `git merge-tree --write-tree <base> HEAD`, using the fetched
base ref for `<base>`. If GitHub mergeability is `UNKNOWN`, read it again.
If the candidate is behind the base but merges cleanly, do not send it back:
merge the base into the PR branch with a merge commit (no rebase or force push).
Push explicitly with `git push origin HEAD:refs/heads/BRANCH`, using the literal
branch from `gh pr view N --json headRefName`; record the resulting SHA.
Your own clean base merge needs no new review. Run the declared final checks at
the resulting head, or `candidate_sha` without a base merge.

Wait for every CI check that runs on that exact head, including your own base merge,
to complete successfully. Pending checks, including a just-pushed head with no checks
yet, mean wait (`gh pr checks N --watch`). A repository without CI means no wait;
failed checks follow the rules below.

Immediately before merging or publishing a handoff, check
`gh pr view N --json headRefOid` against
`candidate_sha` or the SHA of the base merge you pushed yourself; any other change
reports blocked. Immediately before merging, re-read the item's labels; report
blocked if any `stop-labels` from `ub-agents.yaml` is present. Also confirm the PR's
base is the default branch (`gh pr view N --json baseRefName`); a PR stacked on another
branch would merge the reviewed work into that branch instead.

If merge policy authorizes it and gates pass, merge exactly the verified head
with the project's method (for example `gh pr merge N --squash
--match-head-commit SHA`). If the merge command fails, check
`gh pr view N --json state,mergeCommit`; if merged, report `merged`.
Close the linked issue when completion is satisfied and record
`ub-agents report --outcome merged --summary "Merged SHA under project policy"`.

If policy requires a maintainer merge, report
`ub-agents report --outcome maintainer-merge
--summary "Gates pass for SHA; maintainer merge required by POLICY"
--action "Maintainer: merge #N because REASON."`.

If a check fails in code the diff does not touch, rerun the failed jobs once
(`gh run rerun RUN_ID --failed`). If the rerun passes, file an issue for the flaky
check with the run link, name it in your report and continue. If it fails again and
the base branch fails the same way, the failure is not the PR's: file an issue with
the run link and report blocked naming it. Do not send either back.

Only if the candidate conflicts with the base branch, a declared check fails for a
cause that code or tests in the repository can fix, or the project's shared guidance
asks PRs to carry changelog entries and the entry for a user-facing change is missing
or inaccurate, send it back to the implementer:
`ub-agents report --outcome changes-requested` must name what to fix, the check and
output. Fixable causes include environment-dependent tests the PR touches. Report
blocked only when the fix needs something outside the repository (access, a
permission, an external service or a human decision), another gate fails or evidence
is missing. The framework grants no merge authority, approves no PRs and chooses no checks.
