# Review the exact candidate

Read any shared repository guidance (such as AGENTS.md), the original issue
requirements and acceptance criteria, the assigned candidate SHA, code, diff, and
candidate-specific check evidence. Start fresh and do not read the implementation
reasoning transcript. Check the current PR head against the assignment context's
`candidate_sha` before recording a verdict.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agent report`.

If the PR is a draft, report blocked; the implementer must mark it ready first.
Never add ready-to-merge to a draft.

This project requires independent cross-provider review: the launcher runs you on a
different CLI and model from the implementer of this candidate. Changing
effort or resetting the author's conversation does not establish independent review.

If the project keeps a changelog, a missing or inaccurate entry for a user-facing change
is a required correction. Apply any review focus the shared guidance names. Never edit the candidate to fix it
during review. Publish a GitHub COMMENT review (`gh pr review --comment`) naming the
assigned SHA, with concrete findings and the checks you ran. Report changes-requested
for required corrections, or approved if the project's acceptance criteria pass.
Both are successful review handoffs: use `ub-agent report --outcome NAME
--summary "Review verdict for SHA: ..."`.

For headless Claude, avoid shell expansion (`$VAR`, `${VAR}`, `$(...)` or backticks),
even with allowlisted commands. Insert the literal PR number from the assignment
context's `assignment` and the literal full SHA from `candidate_sha` into commands;
do not read them through shell variables. In the examples below, replace N with
that number and PATH with the literal body-file path before running the command.
Write the review body with the file-writing tool to a file in your worktree.
Immediately before publishing, run `gh pr view N --json headRefOid` as a separate
command and compare its output with `candidate_sha`; report blocked if they differ.
Publish with `gh pr review N --comment --body-file PATH`.
Run `ub-agent report` as its own final command, never chained to the head check or
review publication. If publication is denied, retry with separate commands using
literal values; if it still fails, report blocked with the evidence instead of
ending without a report.

Do not merge. Native GitHub approvals require an eligible reviewer account and remain
subject to branch protection. Explicit outcomes do not bypass those rules.
