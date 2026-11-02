# Principles

## Small modules

Match the surrounding code. Keep modules focused on one responsibility and split
new logic into small functions. The [complexity baseline](checks.md) sets the
limits for new and growing functions.

## Dependencies

Use the standard library plus PyYAML. Keep `gh` for setup and authentication;
the existing GitHub adapter sends launcher REST and GraphQL requests through
standard-library `http.client`, using the token from `gh auth token --hostname
github.com`. It falls back to `gh api` when no token can be read. Add no runtime
dependencies without maintainer agreement.

Good: route new requests through the existing GitHub adapter. Bad: add an HTTP
client dependency or a second GitHub authentication path for one new request.

## Simplest solution first

Prefer the smallest solution that meets the assigned behavior and fits the code
around it. Reuse an existing helper before adding an abstraction. Introduce a
general mechanism when the current requirements need it.

Good: add one validation function for a new configuration value. Bad: introduce a
validation plugin system to handle that one value.
