# Review the exact candidate

Read any shared repository guidance (such as AGENTS.md), the original issue
requirements and acceptance criteria, the assigned candidate SHA, code, diff, and
candidate-specific check evidence. Start fresh and do not read the implementation
reasoning transcript. Check the current PR head against UB_AGENTS_CANDIDATE_SHA
before recording a verdict.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agents report`.

If the PR is a draft, report blocked; the implementer must mark it ready first.
Never add ready-to-merge to a draft.

This starter uses the configured runtime without claiming independent authorship.
If the project requires independent cross-provider review, configure
different-runtime-from and an eligible runtime. Changing effort or
resetting the author's conversation does not establish independent review.

If the project's shared guidance asks PRs to carry changelog entries, a missing or
inaccurate entry for a user-facing change is a required correction. So is a check that passes only after changing the environment
it runs in, such as unsetting a variable or skipping a test.

Post concrete findings tied to the assigned SHA. Report changes-requested for
required corrections, or approved if the project's acceptance criteria pass.
Both are successful review handoffs: use `ub-agents report --outcome NAME
--summary "Review verdict for SHA: ..."`.

Do not merge. Native GitHub approvals require an eligible reviewer account and remain
subject to branch protection. Explicit outcomes do not bypass those rules.

Every stop report (`--status blocked` or an outcome adding a configured stop label)
must include at least one `--action "ACTION"` or `--option "OPTION"`. Repeat `--action`
for independent asks that are all needed. Use repeated `--option` for alternative
ways to clear one blocker, with the recommendation first, instead of "choose A or B"
in one ask. Each value is one concise sentence on a non-empty line of at most 300
characters (actions and options together up to 8000). Name who must act and the
actual step, with any consequence needed to answer it. Single-backtick inline code
is preserved; an option ending with `: ` followed by a single-backtick command shows
that command in its own code block.
Each ask must be understandable on its own. Put supporting reasoning, technical
evidence, diagnostics and links in `--summary`; notices collapse that full Markdown
by default. Generic blocked reports use `ub-agents report --status blocked
--summary "Gate evidence: REASON" --action "ACTION"`.
