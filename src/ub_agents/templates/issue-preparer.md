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

If a human decision is required, explain it on the issue and report
`ub-agent report --outcome needs-human --summary "Decision required: REASON"`.

When requirements can be implemented, report
`ub-agent report --outcome prepared --summary "Issue prepared"`.
The runner applies the configured transition. Do not change workflow labels.
Customize these outcomes and rules with the project's owners.
