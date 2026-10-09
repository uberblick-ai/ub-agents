# Queue

Decides what runs next. Effective priority goes first except for new issues in milestone `prefer` mode. Existing work (pull requests, owned runs and recovery) keeps priority order and goes before the next selected new issue at equal or higher priority. Without a `queue` block, existing work goes first, then age and number; issues wait for their blockers.

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

`prefer` chooses the earliest open milestone with an issue eligible for this launcher, then orders its issues by effective priority, creation time and number. Milestones are ordered by creation time, then milestone number, and must have open issues or pull requests. If earlier work is blocked, owned elsewhere, stop-labelled, untriggered, retry-limited, parked for approval or unavailable to this launcher's runtime, independent later work can start. Every pass reconsiders earlier work; no milestone changes state or cursor advances.

Unmilestoned issues, including issues whose milestone is outside that list, go ahead only at **strictly higher priority** than the selected milestone's next eligible issue. Milestone work wins ties. Higher-priority issues in later assigned milestones still wait. When no milestone has work eligible here, unmilestoned work runs in priority order even while milestones remain open.

`order` ranks priority first, then uses the same milestone order for equal-priority new issues, with unmilestoned issues last. Higher priority wins even in a later or no milestone. `gate` strictly holds later and unmilestoned new issues until the oldest open milestone closes or empties, even when its work is unavailable here. `ignore` is the default.

The `gate` hold applies to automatic queue selection, including planning, claiming
and approval parking. Explicit `launch N [--agent NAME]` and `status N` skip queue
priority and milestone policy at every stage, including after checkout refresh.
Later-milestone and unmilestoned targets can run; every other gate still applies.
An agent still needs a matching trigger label, including with `--agent`. Without
`--agent`, the first eligible agent in configuration order acts. Unnumbered launch,
`launch --once` and the status queue listing keep their selection policy.

`ub-agents launch --agent NAME` limits the queue to one agent named in the
configuration. `launch --once --agent NAME` observes once and runs at most one
selected assignment. Both keep queue priority and milestone policy, along with
dependencies, triggers, stop labels, input approval, ownership, elections,
attempts, backoff and runtime availability. Only the selected agent's assignments
are planned, claimed, started or recovered; other agents' leases, pending outcomes
and notices stay untouched. Supervision and process cleanup remain the same.
Unknown names fail before queue observation and list configured names; removing
the selected agent during a reload leaves the launcher claiming nothing with
an explanation.

To prepare a milestone's issues first, run `ub-agents launch --agent issue-preparer`
using your configured preparation agent name. Watch preparation finish or reach
human decisions, stop the launcher, resolve the decisions, then run
`ub-agents launch` for the normal queue. A filtered continuous launcher keeps
polling until stopped, even when its work runs out. It does not override milestones,
bypass blockers or automatically launch another agent.

The terminal view and `--no-ui` output name the selected agent. Idle output
distinguishes no open item with that agent's trigger labels, naming them, from
matching work waiting with counts by state and reason. It does not claim every
issue is prepared.

To avoid idle waiting, switch `gate` to `prefer` when milestone precedence matters, or to `order` when priority should always win. Unreadable milestones, items or dependency links stop selection visibly; they never justify fallback. Fresh claim checks and election protection still apply. `prefer` and `order` need no milestone recheck at claim time or during approval parking.

```yaml
milestones: prefer
```

**Upgrading:** older launchers reject `prefer`. Upgrade every launcher of a project to a build supporting it before that project's `ub-agents.yaml` uses the value. Existing configurations keep their behavior.

## Dependencies

With `wait`, an issue does not start while a GitHub "blocked by" issue is open, and blockers inherit the priority of what they block. In milestone `order` and `prefer` modes, open local blockers also inherit their dependents' earliest milestone, directly or transitively. Release preparation still waits for its prerequisites, as does later work blocked by earlier issues. `ignore` turns dependency waits and inheritance off.

```yaml
dependencies: wait
```

## See the order

`ub-agents status` and `status --json` list ready new issues in the order this launcher would start them, with each item's priority and where it came from. In `order` and `prefer`, issues also show their effective milestone and inherited source. No `prefer` row shows `Waiting for active milestone`.

```sh
ub-agents status
```
