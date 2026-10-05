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

If the candidate conflicts with the base branch, or the project keeps a changelog and the
entry for a user-facing change is missing or inaccurate, send it back to the implementer:
name what to fix in the summary of `ub-agents report --outcome changes-requested`.
Report blocked only when another gate fails or evidence is missing. The framework never grants merge authority, approves its own PR, or
chooses check commands.

Every stop report (`--status blocked` or an outcome adding a configured stop label)
must include `--action "ACTION"`: one non-empty line of at most 300 characters
naming the one thing a person must do. For a decision, name who can answer,
the choices and a recommendation, for example "Owner: choose A or B; recommend A."
Replace placeholders with the actual decision or step; keep gate evidence in
`--summary`. Generic blocked reports use `ub-agents report --status blocked
--summary "Gate evidence: REASON" --action "ACTION"`.
