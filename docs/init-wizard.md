# Init wizard reference layouts

These ASCII frames make the presentation reviewable without a renderer. The main
reference is 92 columns × 19 rows; the narrow reference is 48 columns × 25 rows.
These are reference frame dimensions, not runtime viewport limits. The runtime
uses the full actual terminal width and the available height, reserving three rows
outside the frame for concise notes. A 92×19 viewport therefore has a 92×16 frame;
a 48×25 viewport has a 48×22 frame. Header and single-content-row footer stay fixed
while the body scrolls. Terminal width changes reflow the body and abbreviate
footer labels; full action meanings and keyboard shortcuts remain visible.

Optional steps always appear in the order Uberblick → priorities → retrospectives,
with Skip focused by default. Priority and retrospective entries are checked first;
the same page then shows the additions before a second Continue confirms them.
Only final default-no Apply makes changes. The [init guide](https://agents.uberblick.ai/docs/commands/init.html)
describes navigation, unattended defaults and recovery.

## Runtime permissions

Select the exact starter grant; default to continuing without it. Cancel leaves no effects.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|5/11 Runtime permissions                                                                  |
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

## Uberblick

Optional follow-up; Skip enables nothing. Link destination: https://uberblick.at.

```text
+------------------------------------------------------------------------------------------+
|ub-agents setup                                                                           |
|7/11 Uberblick shared knowledge                                                           |
|                                                                                          |
|Uberblick is a shared knowledge base for agents and humans alike.                         |
|Learn more at https://uberblick.at                                                        |
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
|8/12 Queue priority                                                                       |
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
|10/12 Retrospective boards                                                                |
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
|12/12 Setup results                                                                       |
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
|5/11 Runtime permissions                      |
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
|12/12 Setup results                           |
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
