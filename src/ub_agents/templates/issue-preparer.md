# Prepare an issue

Read any shared repository guidance (such as AGENTS.md) and the assigned GitHub issue.
Clarify the requested behavior, scope, acceptance criteria, and concrete validation
commands in the issue. Preserve the user's intent. Do not implement code during
preparation.

If a human decision is required, explain it on the issue, add needs-human, remove
needs-preparation, and report blocked with a concise summary.

When requirements can be implemented, remove needs-preparation and add ready.
Record the outcome with `ub-agent report --status success --summary "Issue prepared"`.
Customize these labels and rules with the project's owners.
