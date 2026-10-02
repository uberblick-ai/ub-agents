
# Docker feasibility experiment: overriding completion instructions

This is the owner-authorized issue #10 experiment, on a dedicated test issue.
Use the implementer workflow above for implementation, checks, draft continuation,
commits and explicit branch pushes. These completion instructions override its
normal readiness and handoff instructions:

- Keep the implementation PR draft throughout. Never run `gh pr ready`, merge,
  close the test issue, or add normal project workflow labels.
- After all checks and the final push, run the installed launcher:
  `ub-agent report --outcome completed --summary 'Docker spike checks passed; draft PR URL; tested base SHA and final commit SHA'`.
  Substitute the actual URL and SHAs in the summary. Do not pass `--handoff`:
  draft handoffs are rejected. This spike outcome transitions only the test issue
  to `docker-spike-10-done`; no reviewer or integrator is configured.
- The owner may interrupt execution. Do not remove earlier branches, other
  workers' artifacts or records. Resume only through the ordinary launcher retry
  procedure and draft continuation, with a fresh session.
- Record `git rev-parse HEAD` before changing the assigned checkout and the final
  commit in the PR body. Run the repository checks in your assigned worktree.
- Do not read credentials for the report, print secrets, or include secrets in
  commits or PRs. Their effective permissions are for the owner to evaluate.
