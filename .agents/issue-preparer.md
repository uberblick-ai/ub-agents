# Prepare an issue

Read any shared repository guidance (such as AGENTS.md) and the assigned GitHub issue.
Clarify the requested behavior, scope, and acceptance criteria. Preserve the user's
intent. Do not implement code during preparation. Reference the repository's standard
validation instructions unless this change needs an additional check.

Keep the issue short and easy to scan. State the final behavior and the smallest set of
observable acceptance criteria needed to verify it. Do not repeat request history,
general agent instructions, repository policies, routine test lists, or implementation
steps already covered by an authoritative role file, protocol, or project guide; link to
that source when useful. Keep a rule in the issue when this change introduces it,
changes it, or requires the code to enforce it. Group related criteria and describe
outcomes rather than prescribing code structure. Go longer only when a distinct
requirement or unresolved constraint cannot be stated clearly in less space.

Where comments amend or contradict the body, reconcile them into the body's acceptance
criteria. Remove superseded text and, when traceability matters, link to the relevant
comment in a short note. Use the needs-human outcome only for decisions the body and
comments leave open.

For headless Claude, avoid shell expansion (`$VAR`, `${VAR}`, `$(...)` or backticks),
even with allowlisted commands. Insert the literal issue number from the assignment
context's `assignment` into commands; do not read it through shell variables. In the
examples below, replace N with that number and PATH with the literal body-file path
before running the command. Write an updated issue body or comment with the
file-writing tool to a file in your worktree, then publish it with
`gh issue edit N --body-file PATH` or
`gh issue comment N --body-file PATH` as a separate command.
Run `ub-agent report` as its own final command, never chained to publication.
If publication is denied, retry with separate commands using literal values; if it
still fails, report blocked with the evidence instead of ending without a report.

If a human decision is required, explain it on the issue and report
`ub-agent report --outcome needs-human --summary "Decision required: REASON"`.

When requirements can be implemented, report
`ub-agent report --outcome prepared --summary "Issue prepared"`.
Customize these outcomes and rules with the project's owners.
