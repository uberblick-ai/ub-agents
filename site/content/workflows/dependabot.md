## Why it exists

Dependabot is good at noticing updates and bad at finishing them, so its pull requests pile up. Here its own `dependencies` label sends each one straight to the integrator, which works from the title and the diff. The PR body is withheld, because release notes are text from outside your repository. Clean bumps merge, a failing test gets one fix round, and majors wait for a person. [Trusted PR authors](/docs/configuration/approvals.html#trusted-pr-authors) let Dependabot take this path without a maintainer approving every new head.

## What you gain

- Security patches can merge as soon as Dependabot opens them and the checks pass.
- Patch and minor bumps no longer wait for someone to read release notes.
- Majors still go to a person, with the checks already run.

## Where a person steps in

Major updates, and anything one fix round can't settle.
