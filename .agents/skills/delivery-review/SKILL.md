---
name: delivery-review
description: >-
  Review one day of loop deliveries in a repository when a maintainer asks:
  what shipped, how many runs and rounds each item took, what its
  retrospectives said, and the few changes to authority, wording or
  instructions that would have saved runs. Produces a report and slides. Do not
  use for a loop role, a PR review or a merge gate.
---

# Delivery review

The loop should move work forward on its own and ask a person only when a
decision needs one. This review measures how close one day came and names the
smallest changes that would bring it closer. Good changes give clearer
authority, better wording, fewer instructions or more autonomy. Never propose
more process: no new role, label, gate, review round or checklist.

## Authority

The review only reads GitHub. It writes the data, the notes and the two pages to
a directory outside the checkout, and publishes the pages where the session can.
It files no issues, comments, labels or board posts, and deletes nothing; the
maintainer decides what to act on.

## Collect

From any checkout of this repository, for the repository the loop runs on
(`uberblick-ai/ub-agents`, `uberblick-ai/uberblick-2`, ...):

```sh
python3 .agents/skills/delivery-review/collect.py --repo OWNER/NAME > DIR/data.json
```

The window is the 24 hours before now; pass `--until` and `--since` (ISO 8601,
UTC) for another. `totals` and `by_agent` count the window. Each entry in
`deliveries` is an issue with the PRs it handed off to, or a PR closing no
issue, and carries its whole history: every run with its result and summary,
operator resets, Action needed notices, comments outside the launcher's records,
and matching retrospectives. A run reading `no report` ended without an outcome.

Retrospectives come from the boards in the repository's `ub-agents.yaml` and need
GitHub GraphQL. Where it is unavailable, `retrospectives.errors` says so; report
the boards as not read rather than guessing.

## Analyze

Look at every delivery with extra runs, a reset, a notice or a `no report` run.
Read its run summaries in `data.json`, and open the linked comment only when a
summary does not name the cause. Write the cause in one sentence. A handoff to a
maintainer that the merge policy requires is not waste; say whether the policy
still earns the wait.

Group causes by mechanism, not wording: the same conflict, the same denied
command, the same rule read two ways. Count the runs each cost. Check `main`
and open issues before calling something open: a cause already fixed or filed is
reported with its link.

Then propose at most four changes, each pulling one lever:

- **authority**: work bounced or waited because nobody, or the wrong role, owned
  the step. Name who should own it.
- **wording**: an instruction was misread or read two ways. Quote the line from
  the role file at `main` and give the replacement.
- **fewer-instructions**: an instruction made work without value. Name the lines
  to delete.
- **autonomy**: a person, a stop or a denied command did what an agent could
  have done. Give the permission, config or tooling change.

A change that only adds text must delete more than it adds. Prefer moving a
mechanical step into `ub-agents.yaml`, the launcher or the test tooling over
prose. A retrospective is a claim: count it only where the records agree.

## Write the notes

Write `DIR/notes.json`:

```json
{
  "headline": "One sentence: what shipped and the biggest lesson.",
  "items": {"227": "One sentence on what cost runs, or why it went clean."},
  "causes": [{"cause": "Changelog conflicts at merge", "cost": "21 runs", "items": [252, 254],
              "state": "Fixed by #276"}],
  "lessons": [{"lever": "autonomy", "title": "Short imperative",
               "change": "The exact change, with any quoted line and its replacement.",
               "where": "ub-agents.yaml", "cost": "6 denied reads", "evidence": [248, 255]}],
  "next": ["One line per thing a person should do next."]
}
```

Write the way the roles should: plain sentences, no internal jargon.

## Render and publish

```sh
python3 .agents/skills/delivery-review/render.py DIR/data.json DIR/notes.json DIR
```

This writes `report.html` and `slides.html`. Publish both where the session can
publish pages, updating the same pages when you re-run the same day; otherwise
give the file paths. Reply with the headline, the two links and one line per
proposed change.

The pages may be shared. Keep credentials, environment values, local paths,
hostnames and log excerpts out of the notes; `collect.py` already strips local
paths from the text it copies.
