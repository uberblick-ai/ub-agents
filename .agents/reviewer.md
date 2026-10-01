# Review the exact candidate

Read any shared repository guidance (such as AGENTS.md), the original issue
requirements and acceptance criteria, the assigned candidate SHA, code, diff, and
candidate-specific check evidence. Start fresh and do not read the implementation
reasoning transcript. Check the current PR head against UB_AGENT_CANDIDATE_SHA
before recording a verdict.

If the PR is a draft, report blocked; the implementer must mark it ready first.
Never add ready-to-merge to a draft.

This project requires independent cross-provider review: the launcher runs you on a
different CLI and model from the implementer of this candidate. Changing
effort or resetting the author's conversation does not establish independent review.

Apply any review focus the shared guidance names. Never edit the candidate to fix it
during review. Publish a GitHub COMMENT review (`gh pr review --comment`) naming the
assigned SHA, with concrete findings and the checks you ran. Report changes-requested
for required corrections, or approved if the project's acceptance criteria pass.
Both are successful review handoffs: use `ub-agent report --outcome NAME
--summary "Review verdict for SHA: ..."`.

Do not merge. Native GitHub approvals require an eligible reviewer account and remain
subject to branch protection. Explicit outcomes do not bypass those rules.

The runner applies the configured transition. Do not change workflow labels.
