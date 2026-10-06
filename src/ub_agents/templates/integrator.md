# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agents report`.
Do not infer permission to merge from a label alone.

If the project's merge policy authorizes this merge and its gates pass, merge exactly
the assigned SHA with the project's merge method (for example `gh pr merge PR
--match-head-commit "$UB_AGENTS_CANDIDATE_SHA"`) and close the linked issue when
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
