## Why it exists

Dependabot is good at noticing updates and bad at finishing them, so its pull requests pile up. Here its own `dependencies` label sends each one straight to the integrator, which works from the title and the diff and never reads the release notes in the body. Clean bumps merge, a failing test gets one fix round, and majors wait for a person. [Trusted PR authors](/docs/configuration/approvals.html#trusted-pr-authors) let Dependabot take this path without a maintainer approving every new head.

## What you gain

- Security fixes land the day they are published, not when someone finds a quiet hour.
- Nobody reads release notes for a patch bump again.
- Majors still get a human eye, with every gate already green.

## Where a person steps in

Major updates, and anything one fix round can't settle.
