# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agent report`.
Do not infer permission to merge from a label alone.

If the project's merge policy authorizes this merge and its gates pass, merge exactly
the assigned SHA with the project's merge method (for example `gh pr merge PR
--match-head-commit "$UB_AGENT_CANDIDATE_SHA"`) and close the linked issue when
completion is satisfied. Then record `ub-agent report --outcome merged --summary
"Merged SHA under project policy"`.

If the policy leaves this merge to a maintainer, leave a concrete report that names
the reason and record `ub-agent report --outcome maintainer-merge
--summary "Ready for maintainer merge: REASON"`. Handing a passing candidate to a
maintainer is a successful handoff.

If the candidate conflicts with the base branch, or the project keeps a changelog and the
entry for a user-facing change is missing or inaccurate, send it back to the implementer:
name what to fix in the summary of `ub-agent report --outcome changes-requested`.
Report blocked only when another gate fails or evidence is missing. The framework never grants merge authority, approves its own PR, or
chooses check commands.
