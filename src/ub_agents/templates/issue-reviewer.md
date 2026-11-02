# Review a prepared issue

Independently check the prepared issue in the assignment context against the
repository's code and project guidance. Check that its scope fits one reviewable PR,
the requested behavior is feasible, and the acceptance criteria are clear, observable
and sufficient. Do not edit the issue or implement code.

A criterion without a check, or a manual check that could be automated, is a required
correction.

When the issue is ready to implement, report
`ub-agents report --outcome approved --summary "Prepared issue approved"`.

For required corrections, report `ub-agents report --outcome changes-requested
--summary "FINDINGS"`. State concrete findings in the summary; the issue-preparer
receives them as feedback, applies them and reports `prepared`.

For decisions that need a person, report
`ub-agents report --outcome needs-human --summary "Issue review blocked: REASON"
--option "Owner: choose A." --option "Owner: choose B."`.
