# Review the exact candidate

Read any shared repository guidance (such as AGENTS.md), the original issue
requirements and acceptance criteria, the assigned candidate SHA, code, diff, and
candidate-specific check evidence. Start fresh and do not read the implementation
reasoning transcript. Check the current PR head against UB_AGENT_CANDIDATE_SHA
before recording a verdict.

This starter uses the configured runtime without claiming independent authorship.
If the project requires independent cross-provider review, configure
different-runtime-from and an eligible runtime. Changing effort or
resetting the author's conversation does not establish independent review.

Post concrete findings tied to the assigned SHA. Report changes-requested for
required corrections, or approved if the project's acceptance criteria pass.
Both are successful review handoffs: use `ub-agent report --outcome NAME
--summary "Review verdict for SHA: ..."`.

Do not merge. Native GitHub approvals require an eligible reviewer account and remain
subject to branch protection. Explicit outcomes do not bypass those rules.

The runner applies the configured transition. Do not change workflow labels.
