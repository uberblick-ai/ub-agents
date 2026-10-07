# Keep people on the decisions

Agents do the work; people decide what matters. Make that boundary explicit in the [instructions](/docs/configuration/stop-labels.html#say-when-a-person-decides).

## Park, do not guess

Give roles an [outcome](/docs/configuration/outcomes.html#ask-a-person) that adds a [stop label](/docs/configuration/stop-labels.html), and tell them when to use it.

```yaml
outcomes:
  prepared: {add: [ready]}
  needs-human: {add: [needs-human]}
```

## One ask per decision

Each [`--action`](/docs/configuration/outcomes.html#stop-reports) is one sentence a person can act on without reading the log: who, the choices, a recommendation.

## Merges that need a person

Let the integrator [hand some merges to you](/docs/configuration/outcomes.html#ask-a-person), such as changes to the workflow files themselves.

```yaml
integrator:
  outcomes:
    merged: {}
    maintainer-merge: {add: [needs-human]}
```
