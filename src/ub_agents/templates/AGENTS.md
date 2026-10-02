# Shared agent guidance

## Project checks

Replace these placeholders with the project's required commands before launch:

- Build: `<project build command>`
- Tests: `<project test command>`
- Other checks: `<project lint or validation command>`

Run the checks relevant to the assigned change and record the results.

## Rules for every agent

- Work only on the assigned issue or PR in the directory the launcher gives you.
  Do not modify other checkouts, worktrees, branches or runs' processes.
- Read the assigned item's requirements and comments, and any review feedback.
- Record the outcome with the installed `ub-agent report` command. Use a declared
  `--outcome NAME` for success, or `--status retry|blocked` when work cannot finish.
  Include a concise summary and `--handoff PR_NUMBER` for an issue-to-PR handoff.
- Start implementation PR bodies with `Closes #N`, replacing N with the assigned
  issue number.
- Never approve your own PR or enable auto-merge. Follow the project's review and
  merge policy.
- The launcher owns the configured label transitions. Leave workflow labels to it
  when reporting a declared outcome.
