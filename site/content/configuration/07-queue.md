# Queue

Decides what runs next. Effective priority goes first, then existing work (pull requests, owned runs and recovery) before new issue starts. Without a `queue` block, existing work goes first, then age and number; issues wait for their blockers.

```yaml
queue:
  priority:
    labels: [priority:urgent, priority:high, priority:normal, priority:low]
    default: priority:normal
  milestones: order
  dependencies: wait
```

## Priority

Priority labels, highest first. Items without one get the default. Pull requests inherit the priority of the open issues they close.

```yaml
priority:
  labels: [priority:high, priority:low]
  default: priority:low
```

## Milestones

`order` ranks new issues of equal priority by earlier open milestones first, with unmilestoned issues last. Higher priority wins even in a later or no milestone, and milestones never hold work back. `gate` holds new issues until the oldest open milestone is done. `ignore` is the default.

```yaml
milestones: order
```

## Dependencies

With `wait`, an issue does not start while a GitHub "blocked by" issue is open, and blockers inherit the priority of what they block. `ignore` turns this off.

```yaml
dependencies: wait
```

## See the order

`ub-agents status` lists work in the same order, with each item's priority and where it came from.

```sh
ub-agents status
```
