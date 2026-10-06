# Review the exact candidate

Read the original issue requirements and acceptance criteria, the assigned candidate
SHA, code, diff, and candidate-specific check evidence. Check the current PR head
with `gh pr view N --json headRefOid` against the assignment context's `candidate_sha`
before reviewing. Report blocked if they differ.

If the PR is a draft, report blocked; the implementer must mark it ready first.
Never add ready-to-merge to a draft.

This starter uses the configured runtime without claiming independent authorship.
If the project requires independent cross-provider review, configure
different-runtime-from and an eligible runtime. Changing effort or
resetting the author's conversation does not establish independent review.

If the project's shared guidance asks PRs to carry changelog entries, a missing or
inaccurate entry for a user-facing change is a required correction. So is a check that passes only after changing the environment
it runs in, such as unsetting a variable or skipping a test.

Apply the shared policy's review focus. Never edit the candidate during review.
Write a review body naming the assigned SHA, concrete findings and the checks run.
Immediately before publishing, run `gh pr view N --json headRefOid` as a separate
command and compare its output with `candidate_sha`; report blocked if they differ.
Publish a GitHub COMMENT review with `gh pr review N --comment --body-file PATH`.
Report changes-requested for required corrections, or approved if the project's
acceptance criteria pass.
Both are successful review handoffs: use `ub-agents report --outcome NAME
--summary "Review verdict for SHA: ..."`.

Do not merge. Native GitHub approvals require an eligible reviewer account and remain
subject to branch protection. Explicit outcomes do not bypass those rules.
