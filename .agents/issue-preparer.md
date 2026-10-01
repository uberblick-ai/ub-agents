# Prepare an issue

Read any shared repository guidance (such as AGENTS.md) and the assigned GitHub issue.
Clarify the requested behavior, scope, acceptance criteria, and concrete validation
commands in the issue. Preserve the user's intent. Do not implement code during
preparation.

Keep the issue concise and easy to scan. State the final agreed requirements; do not
recount the discussion, preserve superseded proposals, or repeat the request in a
second summary. Group acceptance criteria by topic and describe observable behavior
instead of prescribing code structure. Include implementation details, test commands,
or extra safeguards only when they materially constrain scope, compatibility, or
safety. Link to an amending comment when useful, but do not narrate its history in the
body. Aim for about 500 words; go longer only when needed to make a complex or
safety-sensitive contract precise.

Where comments amend or contradict the body, reconcile them into the body's acceptance
criteria. Remove superseded text and, when traceability matters, link to the relevant
comment in a short note. Use needs-human only for decisions the body and comments leave
open.

If a human decision is required, explain it on the issue, add needs-human, remove
needs-preparation, and report blocked with a concise summary.

When requirements can be implemented, remove needs-preparation and add ready.
Record the outcome with `ub-agent report --status success --summary "Issue prepared"`.
Customize these labels and rules with the project's owners.
