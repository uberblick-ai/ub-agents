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

Milestones go in order by title, comparing numbers as numbers, for example v0.5.9 before v0.5.10.

- `ignore` (the default): milestones don't change the order.
- `order`: priority always wins. At equal priority, earlier milestones go first and issues without a milestone go last.
- `gate`: milestones finish in order. Issues from the earliest open milestone go first; only a higher-priority issue without a milestone can jump ahead. When nothing in that milestone can start, for example because it's blocked or waiting for a person, later work runs.
- `strict-gate`: only issues from the earliest open milestone start, and the queue waits for that milestone to be closed, even after its last issue is done. Blocked and parked issues keep their own status reasons. Use it when a person closes each milestone, for example after publishing its release.

```yaml
milestones: gate
```

## Dependencies

With `wait` (the default), an issue doesn't start while a GitHub "blocked by" issue is open. The blocker inherits the priority of the work it blocks and, with `order`, `gate` or `strict-gate`, its earliest milestone. `ignore` turns this off.

```yaml
dependencies: wait
```

## See the order

`ub-agents status` lists ready issues in the order this launcher would start them, with each item's priority and where it came from. With `gate`, it also says why earlier milestones were passed over; with `strict-gate`, it names the milestone waiting to be closed when no issue in it can start.

```sh
ub-agents status
```
