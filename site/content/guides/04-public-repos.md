# Run the loop on a public repository

On a public repository, anyone can open issues, comment and send pull requests, and agents act on that text with your launcher account's access. A malicious issue can try to steer them: an instruction hidden in the markup, a request to add someone's package, or "run this script to reproduce".

## What protects you

[Approvals](/docs/configuration/approvals.html) are on by default for public repositories. Outside text reaches agents only after a maintainer starts the item with a trigger label.

- Starting approves the comments and edits made before it along with the issue, so read them before you start.
- Outside edits after the start stop the item until a maintainer starts it again.
- Hidden markup, such as HTML comments and invisible characters, is removed from outside text before agents read it.
- Fork pull requests are reviewed as a diff. Their code never runs on a launcher; your CI tests them.
- Work assigned outside text carries `outside-input`, including its handoff PRs and started sub-issues. The label stays after comments are deleted.

## What a start means

Starting outside work tells agents to follow its text through to a pull request, with everything the launcher can reach. Read it as if you were about to do the work yourself:

- Does it ask for a new dependency, a network call, or a change to CI, permissions or agent instructions?
- Does it ask anyone to run a command or a script?

## Limit what a start can reach

Agents can do only what the launcher account and machine allow; set them up as in [Give launchers their own account](/docs/best-practices/launcher-account.html). On public repositories, init's merge policy leaves PRs labelled `outside-input` to maintainers. In an existing project, create the label with `ub-agents doctor` and add [the same rule](/docs/configuration/approvals.html#outside-input-label) to `.agents/ub_agents.md`.
