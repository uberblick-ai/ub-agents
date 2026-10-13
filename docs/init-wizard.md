# Init wizard reference layouts

These ASCII frames make the presentation reviewable without a renderer. The main
reference is 92 columns × 19 rows; the narrow reference is 48 columns × 25 rows.
These are reference frame dimensions, not runtime viewport limits. The runtime
uses the full actual terminal width and the available height, reserving three rows
outside the frame for concise notes. A 92×19 viewport therefore has a 92×16 frame;
a 48×25 viewport has a 48×22 frame. Header and single-content-row footer stay fixed
while the body scrolls. Terminal width changes reflow the body and abbreviate
footer labels; full action meanings and keyboard shortcuts remain visible.

After files/workflow labels, the optional-component selector starts with all three
checkboxes unchecked and explains their benefits with enable-later guidance. Only
selected pages appear, in the order Uberblick → priorities → retrospectives, with
Skip focused by default. Selection alone enables nothing; unchecking or skipping
clears proposed feature settings. Page counts depend on selections and missing labels.
Priority and retrospective entries are checked first;
the same page then shows the additions before a second Continue confirms them.
Only final default-no Apply makes changes. The [init guide](https://agents.uberblick.ai/docs/commands/init.html)
describes navigation, unattended defaults and recovery.

## Setup PR review

Review shows the authenticated `gh` account, target repository, actual default
branch and base SHA, current checkout and the proposed `setup-ub-agents` branch.
The branch is renamable; turning off the setup PR selects local files only.
The exact setup-only commit, push target, PR creation and selected label/board
effects appear beside the file preview. Separate default-no confirmations authorize
creating or switching branches and Apply. Back and Cancel before Apply leave all
local and remote state unchanged.

Results list the actual branch, commit, push and PR URL, or each completed effect
and safe recovery steps. A verified matching attempt can reuse its protected setup
files, commit and PR. The user reviews and merges the PR under normal project policy.
Readiness can be retried after external preparation; Start requires the setup PR
to be merged and every ordinary launch gate to pass in a clean default-branch checkout.

`--use-defaults` explicitly consents to this branch/commit/push/PR flow without
prompts. Implicit CI/non-TTY init without that flag retains local files only.

## Runtime permissions

Select the exact starter grant; default to continuing without it. Cancel leaves no effects.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|5/12 Runtime permissions                                                                  |
|                                                                                          |
|Codex grants full access without its sandbox.                                             |
|Exact starter arguments: ["--sandbox", "danger-full-access"]                              |
|                                                                                          |
|The choice applies to all four starter agents.                                            |
|Continue without permissions is the default.                                              |
|Existing local overrides still take precedence.                                           |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|[Accept and enable permissions (y)] [Continue without permissions (n)] [Cancel (esc)]     |
+------------------------------------------------------------------------------------------+
```

## Optional-component selection

Benefits precede the checkboxes. Continue opens only selected pages; Skip leaves all off.
Link destination: https://uberblick.ai.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|7/9 Optional components                                                                   |
|                                                                                          |
|Choose the setup pages to open. You can enable this later.                                |
|                                                                                          |
|Uberblick: shared knowledge for agents and humans alike. https://uberblick.ai             |
|Priorities: order eligible work within queue rules.                                       |
|Retrospectives: retain lessons through later public agent posts.                          |
|                                                                                          |
|[ ] Uberblick shared knowledge                                                            |
|[ ] Queue priorities                                                                      |
|[ ] Retrospectives                                                                        |
|                                                                                          |
|Selected pages still default to Skip; nothing is written until Apply.                     |
|                                                                                          |
|                                                                                          |
|Back ^B       Continue ^N       Skip ^S (default)       Cancel Esc                        |
+------------------------------------------------------------------------------------------+
```

## Uberblick

Selected optional follow-up; Skip enables nothing. Link destination: https://uberblick.ai.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|8/12 Uberblick shared knowledge                                                           |
|                                                                                          |
|Uberblick is a shared knowledge base for agents and humans alike.                         |
|Learn more at https://uberblick.ai                                                        |
|                                                                                          |
|You can enable this later.                                                                |
|This is an optional follow-up, never a runtime dependency.                                |
|No integration, account or workspace is enabled.                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|Back ^B       Continue ^N       Skip ^S (default)       Cancel Esc                        |
+------------------------------------------------------------------------------------------+
```

## Priority preview

Order eligible work within queue rules; Skip keeps equal priority. Values here are illustrative.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|9/13 Queue priority                                                                       |
|                                                                                          |
|Priorities order eligible work within the queue rules.                                    |
|You can enable this later.                                                                |
|                                                                                          |
|This adds to workflow.yaml:                                                               |
|queue:                                                                                    |
|  priority:                                                                               |
|    labels: [urgent, normal]                                                              |
|    default: normal                                                                       |
|                                                                                          |
|Continue confirms the checked preview; missing labels are a separate default-no choice.   |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|Back ^B       Continue ^N       Skip ^S (default)       Cancel Esc                        |
+------------------------------------------------------------------------------------------+
```

## Retrospective preview

Retain lessons through later public agent posts. Link destination: GitHub Docs, creating a discussion. This example uses an existing verified board; requested boards show their title, category and body with config deferred until their real number is verified.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|11/13 Retrospective boards                                                                |
|                                                                                          |
|Retrospectives retain lessons from completed runs. You can enable this later.             |
|PASS: Discussions enabled in org/project; account role: WRITE.                            |
|Existing board #203: exact URL, readable, open/unlocked, write capability verified.       |
|                                                                                          |
|This adds to workflow.yaml:                                                               |
|agents:                                                                                   |
|  implementer:                                                                            |
|    retrospectives: 203                                                                   |
|                                                                                          |
|If manual creation or access repair is needed: Retry / Skip / Cancel.                     |
|No test comments are posted. Creation requests remain proposed until Apply.               |
|                                                                                          |
|                                                                                          |
|Back ^B       Continue ^N       Skip ^S (default)       Cancel Esc                        |
+------------------------------------------------------------------------------------------+
```

## Results

Report actual effects; Retry checks external preparation without launching. Explicit Start rechecks and invokes the normal launcher only after all gates pass.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|13/13 Setup results                                                                       |
|                                                                                          |
|Setup completed; project policy still needs owner review.                                 |
|Created workflow.yaml and .agents/ starter instructions.                                  |
|Updated .gitignore; existing guidance preserved.                                          |
|                                                                                          |
|Launch readiness: BLOCKED                                                                 |
|Fresh starters need review, a PR and merge into the default branch.                       |
|Use a clean default-branch launcher checkout without local commits ahead of origin.       |
|                                                                                          |
|Finish exits. Retry readiness checks current files, tools, GitHub access and launch gates.|
|Finish and start loop checks again, then explicitly launches only when ready.             |
|                                                                                          |
|                                                                                          |
|                                                                                          |
|Finish ^N         Retry readiness ^R         Finish and start loop ^L                     |
+------------------------------------------------------------------------------------------+
```

## Narrow permissions

The same actions share one row; their complete meanings stay in the scrolling body.

```text
+----------------------------------------------+
|ub-agents setup                               |
|5/12 Runtime permissions                      |
|                                              |
|Codex starter grant: full access without      |
|its sandbox.                                  |
|                                              |
|Exact arguments:                              |
|["--sandbox", "danger-full-access"]           |
|                                              |
|Accept and enable permissions (y):            |
|choose these exact arguments.                 |
|                                              |
|Continue without permissions (n):             |
|defer the grant; this is the default.         |
|                                              |
|Cancel (esc): leave setup unchanged.          |
|                                              |
|These arguments apply to all four roles.      |
|Existing local overrides take precedence.     |
|                                              |
|                                              |
|                                              |
|Accept (y)    Without (n)    Cancel esc       |
+----------------------------------------------+
```

## Narrow results

The Start abbreviation means Finish and start loop; scrolling body keeps the full action explanation.

```text
+----------------------------------------------+
|ub-agents setup                               |
|13/13 Setup results                           |
|                                              |
|Setup completed.                              |
|                                              |
|Launch readiness: BLOCKED                     |
|Review policy, commit the starters through    |
|a PR and merge into the default branch.       |
|                                              |
|Prepare the checkout externally.              |
|Retry readiness checks again without          |
|launching.                                    |
|                                              |
|Finish exits.                                 |
|Finish and start loop explicitly checks       |
|all gates again before invoking launch.       |
|                                              |
|Required: clean default branch; no ahead      |
|or divergent commits; committed setup;        |
|readable policy, usable runtime and labels.   |
|                                              |
|                                              |
|Finish ^N      Retry ^R      Start ^L         |
+----------------------------------------------+
```
