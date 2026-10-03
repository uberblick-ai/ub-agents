# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agent report`.
Do not infer permission to merge from a label alone.

For headless Claude, avoid command substitution (`$(...)` or backticks), even with
allowlisted commands. Check the head with
`gh pr view "$UB_AGENT_ASSIGNMENT" --json headRefOid` as a separate command before
merging or publishing a handoff; compare its output with UB_AGENT_CANDIDATE_SHA and
report blocked if they differ. Write any GitHub comment with the file-writing tool
to a file in your worktree and publish it with
`gh pr comment "$UB_AGENT_ASSIGNMENT" --body-file PATH` as a separate command.
Run `ub-agent report` as its own final command, never chained to the head check,
merge or comment publication. If an action is denied, retry with these separate
commands; if it still fails, report blocked with the evidence instead of ending
without a report.

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
