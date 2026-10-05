Review the change between BASE and HEAD in this isolated checkout.

This is a single unattended PR review. Read AGENTS.md and requirements.md first.
The requirements are the closing issue's requirements at the reviewed revision.
You have repository read, search and bash tools. Use them in multiple turns:
inspect the diff, read surrounding source, follow relevant callers, and run focused
checks with the already installed .venv/bin/python. Do not install dependencies.
Spend your time on concrete correctness failures and unmet requirements. Do not
edit the implementation. Do not access the internet, GitHub or external files.
There are no review comments or later revisions available. Do not delegate.

Finish with a Markdown review beginning with `## Final review`. For each finding,
give severity, a concrete triggering scenario, the observed incorrect behavior,
and repository file/line evidence. Explain the evidence from code or checks.
If you find nothing, say so explicitly. In either case, list the source and callers
examined, checks actually run and their results, and coverage limitations. Separate
confirmed findings from unverified suspicions. Do not claim a check you did not run.
Return the review in your final response; do not publish or report it elsewhere.
