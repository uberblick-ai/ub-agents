# Review the exact candidate

Read the original issue requirements and acceptance criteria, the assigned candidate
SHA, code, diff, and candidate-specific check evidence. Check the current PR head
with `gh pr view N --json headRefOid` against the assignment context's `candidate_sha`
before reviewing. Report blocked if they differ.

If the PR is a draft, report blocked; the implementer must mark it ready first.
Never add ready-to-merge to a draft.

This project requires independent cross-provider review: the launcher runs you on a
different CLI and model from the implementer of this candidate. Changing effort or
resetting the author's conversation does not establish independent review.

If the project's shared guidance asks PRs to carry changelog entries, a missing or
inaccurate entry for a user-facing change is a required correction. So is a check that passes only after changing the environment
it runs in, such as unsetting a variable or skipping a test.

Apply the shared policy's review focus. Never edit the candidate during review.
Write a review body naming the assigned SHA, concrete findings and the checks run.
In the review body, list each acceptance criterion as checked by running, checked by
reading, or not checked, with a reason for any gap. A missing tool or service does not
justify skipping a check that can be run. List manual checks for the maintainer; on
their own they do not block approval. An unchecked criterion that could have been run
is a required correction.

Immediately before publishing, run `gh pr view N --json headRefOid` as a separate
command and compare its output with `candidate_sha`; report blocked if they differ.
Publish a GitHub COMMENT review with `gh pr review N --comment --body-file PATH`.
Mark each finding P1 or P2. P1 means the PR must not merge as is: an unmet acceptance
criterion, a bug, a security or data-loss risk, or a failing check. Everything else
is P2. The assignment context's outcome_rounds shows the counted outcome, rounds
used and limit. When used is at least the limit, this is the final review: report
approved when only P2 findings remain and list them in the review. Otherwise report
changes-requested and name each open P1 finding in the summary. Before the final
review, report changes-requested for corrections, or approved if acceptance passes.
Both are successful review handoffs: use `ub-agents report --outcome NAME
--summary "Review verdict for SHA: ..."`.

Do not merge. Native GitHub approvals require an eligible reviewer account and remain
subject to branch protection. Explicit outcomes do not bypass those rules.
