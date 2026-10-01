# Prepare an issue

Read any shared repository guidance (such as AGENTS.md) and the assigned GitHub issue.
Clarify the requested behavior, scope, acceptance criteria, and concrete validation
commands in the issue. Preserve the user's intent. Do not implement code during
preparation.

Where comments amend or contradict the body, reconcile them into the body's acceptance
criteria and name the comment each change came from. Use needs-human only for decisions
the body and comments leave open.

If a human decision is required, explain it on the issue, add needs-human, remove
needs-preparation, and report blocked with a concise summary.

When requirements can be implemented, remove needs-preparation and add ready.
Record the outcome with `ub-agent report --status success --summary "Issue prepared"`.
Customize these labels and rules with the project's owners.
