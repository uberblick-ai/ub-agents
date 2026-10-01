# Implement or revise the assigned work

Read any shared repository guidance (such as AGENTS.md), the original issue
requirements, and the project's declared checks. For a revision, use the assigned PR
and its feedback. Work only in the directory provided by the launcher. Never remove
another session's worktree or kill its processes.

Run the project's checks. Commit and push your work under the project's permission
policy. For a new implementation, create a PR whose body links its original issue,
then report the handed-off outcome naming the PR. Keep the issue open
until the project's completion policy is satisfied.

For revisions, the PR checkout is detached: push the committed result explicitly to
the existing PR branch and report handed-off naming that PR. Do not create a
second implementation PR. Use UB_AGENT_BRANCH to identify the branch when available.

After the GitHub handoff, run `ub-agent report --outcome handed-off --summary "Checks
passed; candidate ready for review" --handoff PR_NUMBER`. Report retry for an
identified transient failure; report blocked and explain unresolved human decisions.
No check commands, acceptance rules, or permission grants are supplied by the framework.

The runner applies the configured transition. Do not change workflow labels.
