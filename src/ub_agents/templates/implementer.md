# Implement or revise the assigned work

Read shared repository guidance (such as AGENTS.md), the original issue and its
comments, and the project's checks. For a revision, read the PR's feedback. Work
only in the launcher-provided directory. Never remove another session's worktree
or kill its processes.

For new work, commit the first coherent checkpoint after relevant checks, push under
project policy, and open a draft PR (`gh pr create --draft`) starting with
`Closes #N`. Push meaningful checkpoints to that same PR. Continue an assigned draft; never create a second
implementation PR. Checkpoints do not complete the assignment: keep the PR draft
and do not report success until implementation and all project checks finish.

Before each push and before marking the PR ready, read new issue comments and PR
comments, reviews, and inline feedback. Incorporate it or explain why you cannot.
An unresolved human decision keeps the PR draft and requires a blocked report.

Commit and push the final work, mark the same PR ready (`gh pr ready PR`), and keep
the issue open until project completion policy is met. For revisions or detached
checkouts, push explicitly to the existing PR branch
(`git push origin HEAD:refs/heads/$UB_AGENT_BRANCH`). Use UB_AGENT_BRANCH when available.

Then run `ub-agent report --outcome handed-off --summary "Checks passed; candidate
ready for review" --handoff PR_NUMBER`. Report retry for an identified transient
failure; report blocked and explain unresolved human decisions. The runner applies
the configured transition. Do not change workflow labels. The launcher owns claim
renewal. The framework supplies no checks, acceptance rules, or permission grants.
