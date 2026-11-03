## Why it exists

When every merge to main deploys to production, a feature split into small issues reaches users half finished. Here a milestone is the feature. When the milestone has no branch yet, the implementer creates one from main and gives it a preview, and every issue in the milestone merges into that branch instead of main. Before each merge the integrator checks whether main has changed and, if it has, merges main into the milestone branch first, so the preview runs on current main. It fixes a trivial conflict itself, such as a lockfile or a small hotfix line, and hands anything harder to the team with concrete suggestions for resolving it. When the team decides to release the milestone, it opens one pull request from the branch into main, checks its gates and merges it by hand. The loop's [milestone gate](/docs/configuration/queue.html) finishes the earliest milestone first.

## What you gain

- Small issues and small reviews, without shipping a feature in pieces.
- One preview where the team tries the whole milestone before anyone sees it.
- A release that is a single, deliberate merge into main.

## Where a person steps in

Trying the milestone preview and merging the milestone into main, and resolving a conflict between main and a milestone branch that is too complex for the integrator.
