# Implement or revise the assigned work

Read shared repository guidance (such as AGENTS.md), the original issue and its
comments, and the project's declared checks. For a revision, read the assigned PR's
feedback. Work only in the launcher-provided directory. Never remove another
session's worktree or kill its processes.

For a new implementation, commit the first coherent checkpoint after relevant
checks, push under project policy, and open a draft PR (`gh pr create --draft`)
whose body starts with `Closes #N`. Push meaningful later checkpoints to that same
PR. If the assignment names an existing draft, continue it. Never create a second
implementation PR for the issue. Checkpoints do not complete the assignment: keep
the PR in draft, leave issue labels in place, and add no review/integration labels.

Before each push and before marking the PR ready, read new issue comments and PR
comments, reviews, and inline feedback. Incorporate it or explain why you cannot.
An unresolved human decision keeps the PR in draft and requires a blocked report.

When implementation and all project checks finish, commit and push the final work,
mark the same PR ready (`gh pr ready PR`), remove ready from the issue, and add
needs-review to the PR. Keep the issue open until project completion policy is met.

For revisions or detached checkouts, push explicitly to the existing PR branch
(`git push origin HEAD:refs/heads/$UB_AGENT_BRANCH`). Revisions remove needs-changes
and add needs-review. Use UB_AGENT_BRANCH when available.

After the GitHub handoff, run `ub-agent report --status success --summary "Checks
passed; candidate ready for review" --handoff PR_NUMBER`. Report retry for an
identified transient failure; report blocked and explain unresolved human decisions.
The launcher owns claim renewal. The framework supplies no check commands,
acceptance rules, or permission grants.
