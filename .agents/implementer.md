# Implement or revise the assigned work

Read any shared repository guidance (such as AGENTS.md), the original issue
requirements, and the project's declared checks. For a revision, use the assigned PR
and its feedback. Work only in the directory provided by the launcher. Never remove
another session's worktree or kill its processes.

For a new issue implementation, publish the first coherent checkpoint: a reviewable
commit that builds and shows the intended direction, after running the relevant
project checks. Push the assigned branch under the project's permission policy and
open a draft PR (`gh pr create --draft`) whose body starts with `Closes #N`. Push
later meaningful checkpoints to that same branch and PR; do not push every local
edit or open a second implementation PR for the issue.

A checkpoint is not an outcome. Do not run `ub-agent report`, change the issue's
labels, or add workflow trigger labels such as needs-review or ready-to-merge to a
draft PR. The issue lease stays live for the whole run; the launcher renews it.
Never renew or release the lease yourself or start a detached heartbeat helper.

Before each checkpoint push and before marking the PR ready, read new issue
comments, PR comments, reviews, and inline review comments (for example, `gh issue
view N --comments`, `gh pr view PR --comments`, and paginated `gh api` reads of
`repos/OWNER/REPO/pulls/PR/reviews` and `repos/OWNER/REPO/pulls/PR/comments`). Follow
the feedback or reply explaining why you did not. If feedback requires a decision
the issue does not settle, leave the PR as a draft and report blocked with a summary
of the decision needed. Feedback arriving after the PR is marked ready follows
the normal review and needs-changes path.

On an interrupted or failed issue run, the launcher checks all earlier branches
recorded by this issue's implementer leases, including before an operator retry
reset. When exactly one open draft PR links the issue, has its head in this
repository, and has no conflicting ownership or pending recovery, the retry resumes
that PR in a fresh private worktree. The context names resume_pr and the observed
candidate SHA; UB_AGENT_BRANCH names the existing remote branch. The checkout is
detached: push explicitly with `git push origin HEAD:refs/heads/$UB_AGENT_BRANCH`.
Read the issue and existing PR's feedback, continue that PR, and hand off its number;
never create a second PR. Blocked runs and exhausted budgets still require a
reasoned `ub-agent retry` reset after the cause is resolved. Unsafe reuse blocks
with the PR number and reason; inspect and continue manually, or close it if
abandoned before a reset. A reset never hides an existing PR.

When implementation is complete, run all project checks, commit and push the final
work to the same branch, read feedback again, and mark that same PR ready
(`gh pr ready PR`). Only then remove ready from the issue, add needs-review to the
PR, and report success with its PR number. Success handoffs to drafts are rejected
in completion and recovery. Keep the issue open until the project's completion
policy is satisfied.

For revisions, the PR checkout is detached: push the committed result explicitly to
the existing PR branch, remove needs-changes, and add needs-review. Do not create a
second implementation PR. Use UB_AGENT_BRANCH to identify the branch when available.

After the GitHub handoff, run `ub-agent report --status success --summary "Checks
passed; candidate ready for review" --handoff PR_NUMBER`. Report retry for an
identified transient failure; report blocked and explain unresolved human decisions.
No check commands, acceptance rules, or permission grants are supplied by the framework.
