# Implement or revise the assigned work

Read shared repository guidance (such as AGENTS.md), the original issue and its
comments, and the project's checks. For a revision, read the PR's feedback. Work
only in the launcher-provided directory. Never remove another session's worktree
or kill its processes.
This run is a single, non-interactive session that is never resumed: ending your
turn ends the run, so run checks in the foreground or wait for every background
job to finish before ending your turn, and end the run with `ub-agent report`.

Before starting new work, check for an open draft PR from an earlier run of this
issue: the assignment context lists `earlier_branches`; run `gh pr list --state open
--head BRANCH` for each. If one exists, continue it: fetch its branch, check out its
head detached, and push to that branch; never open a second implementation PR.
Otherwise commit the first coherent checkpoint after
relevant checks, push under project policy, and open a draft PR (`gh pr create --draft`)
starting with `Closes #N`. Push meaningful checkpoints to that same PR. Checkpoints do
not complete the assignment: keep the PR draft and do not report success until
implementation and all project checks finish.

Before each push and before marking the PR ready, read new issue comments and PR
comments, reviews, and inline feedback. Incorporate it or explain why you cannot.
An unresolved human decision keeps the PR draft and requires a blocked report.

Commit and push the final work, mark the same PR ready (`gh pr ready PR`), and keep
the issue open until project completion policy is met. Push explicitly to the PR's
branch (`git push origin HEAD:refs/heads/BRANCH`): for a PR revision that branch is
UB_AGENT_BRANCH; for a continued draft it is the branch `gh pr view` reports.

If the project keeps a changelog, add an entry for user-facing changes in the same PR,
as its shared guidance describes.

Then run `ub-agent report --outcome handed-off --summary "Checks passed; candidate
ready for review" --handoff PR_NUMBER`. Report retry for an identified transient
failure; report blocked and explain unresolved human decisions. The
framework supplies no checks, acceptance rules, or permission grants.
