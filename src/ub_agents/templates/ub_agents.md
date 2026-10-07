# Project loop policy

## Checks

{checks}

Run the relevant checks before handoff and record the results. A check that passes
only after changing its environment or skipping tests has not passed.

## Merging

The integrator squash-merges only when the project's merge policy authorizes it
and every required review and check applies to the current head. Define those gates
before launch. Leave changes to workflow, permissions and release policy to
`@org/maintainers`; customize this list with the project's owners.
Never enable auto-merge, approve your own PR, change branch protection or publish
releases. Implementation PR bodies start with `Closes #N`.

## Human decisions

`@org/maintainers` answers scope and policy questions; replace this handle with the
project's person or team. An issue's author may answer only if they have write
access. Use the responsible role's correction or handoff route when that role must
act next.

## Review focus

Replace this section with the project's review priorities and required evidence.
