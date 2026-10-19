# Queue

Decides what runs next: higher priority first. At equal priority, work already under way, such as open pull requests, goes before new issues, and older items go before newer ones. Issues wait for their blockers.

```yaml
queue:
  priority:
    labels: [priority:urgent, priority:high, priority:normal, priority:low]
    default: priority:normal
  milestones: order
  dependencies: wait
```

## Priority

Priority labels, highest first. Items without one get the default. Pull requests take the priority of the issues they close. To add priorities to an existing setup, run [`ub-agents init --create-priorities`](/docs/commands/init.html#add-the-optional-steps-later-on).

```yaml
priority:
  labels: [priority:high, priority:low]
  default: priority:low
```

## Milestones

Milestones go in alphabetical order by title, for example v0.4.6 before v0.5.0.

- `ignore` (the default): milestones don't change the order.
- `order`: priority always wins. At equal priority, earlier milestones go first and issues without a milestone go last.
- `gate`: milestones finish in order. New issues from the earliest open milestone go first; an issue outside it goes ahead only with a higher priority. When nothing in that milestone can start here, for example because it's blocked or waiting for a person, later work runs.

```yaml
milestones: gate
```

## Dependencies

With `wait` (the default), an issue doesn't start while a GitHub "blocked by" issue is open. The blocker inherits the priority of the work it blocks and, with `order` or `gate`, its earliest milestone. `ignore` turns this off.

```yaml
dependencies: wait
```

## See the order

`ub-agents status` lists ready issues in the order this launcher would start them, with each item's priority and where it came from. With `gate`, it also says why earlier milestones were passed over.

```sh
ub-agents status
```
