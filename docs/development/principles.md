# Principles

## Small modules

Match the surrounding code. Keep modules focused on one responsibility and split
new logic into small functions. The [complexity baseline](checks.md) sets the
limits for new and growing functions.

## Dependencies

Use the standard library plus PyYAML, with `gh` as the GitHub client. Add no
runtime dependencies without maintainer agreement.

Good: use `subprocess.run` with an argument list to call `gh` through the existing
GitHub adapter. Bad: add an HTTP client and a second GitHub authentication path
for one new request.

## Simplest solution first

Prefer the smallest solution that meets the assigned behavior and fits the code
around it. Reuse an existing helper before adding an abstraction. Introduce a
general mechanism when the current requirements need it.

Good: add one validation function for a new configuration value. Bad: introduce a
validation plugin system to handle that one value.
