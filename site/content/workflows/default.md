## From an issue to a merged change

A coding session can produce a useful patch and still leave you arranging a review, checking CI and remembering to merge. The default loop connects those steps through GitHub labels. Your own coding agents pick up each role, work in private branches and hand back a concrete result.

Preparation gives an issue a clear scope and acceptance criteria. The implementer writes the change and runs the checks. A reviewer checks the exact candidate, and the integrator checks the head that will merge against your gates and merge policy. Findings return to the same pull request for another pass.

You get a reviewed change without coordinating every handoff. There is nothing to host: GitHub holds the queue and your launchers run the agents. The roles, labels and outcomes are plain configuration that you can read and change.

## Where you step in

You decide what work starts. An agent stops with a question when scope or policy needs a decision, and unresolved review findings can also come back to you. When your merge policy reserves a change for a person, the integrator gets it ready and asks you to merge it yourself.

Read [How the loop works](/docs/how-the-loop-works.html) for the labels, approvals and recovery paths, or [install ub-agents](/docs/installation.html) to start with the default configuration.
