## Why it exists

When every merge to main deploys to production, a feature split into small issues reaches users half finished. Here a milestone is the feature. When the milestone has no branch yet, the implementer creates one from main, and every issue in the milestone merges into that branch instead of main. Once the first issue lands, one pull request from the branch into main opens and lists what has landed so far; a small workflow can keep it current. Whenever main changes, it is merged into the branch, by the agents working on the branch or by a workflow, so the milestone always runs on current main. A trivial conflict, such as a lockfile or a small hotfix line, is fixed on the spot; anything harder goes to the team with concrete suggestions for resolving it. The team tries the whole milestone on the branch's preview, or on staging when the host doesn't deploy every branch. When it decides to release, it checks the milestone PR's gates and merges it by hand.

Several milestones can run side by side. To finish them one at a time instead, turn on the queue's [milestone gate](/docs/configuration/queue.html).

## What you gain

- Small issues and small reviews, without shipping a feature in pieces.
- One place, a preview or staging, where the team tries the whole milestone before anyone sees it.
- A release that is a single, deliberate merge into main.

## Where a person steps in

Trying the milestone and merging it into main, and resolving a conflict between main and a milestone branch that is too complex for the agents.
