## Finish the dependency updates

Dependabot notices updates and opens pull requests, but somebody still has to check them and finish the merge. This workflow sends its dependencies label straight to the integrator, so routine patch and minor bumps do not wait for someone to open the pull request.

The integrator checks the title and diff, runs the required checks and applies your merge policy. A clean patch or minor update merges. If a check fails, the update gets one fix round: an implementer adapts the code, a reviewer checks the fix, and the integrator tries the gates again.

Trusted PR authors let Dependabot take this path without a maintainer approving every new head. You still choose the trusted author and keep the merge gates in your loop policy. See [trusted PR authors](/docs/configuration/approvals.html#trusted-pr-authors).

## Where you step in

Major releases can change behaviour even when tests pass, so a person reads the changelog and merges or closes the pull request. An update that still fails after its one fix round also waits for you: fix it by hand, pin the old version or close it.

Read [Letting agents auto-merge Dependabot pull requests](https://uberblick.ai/blog/2026/agents-auto-merge-dependabot) for the workflow's background and policy.
