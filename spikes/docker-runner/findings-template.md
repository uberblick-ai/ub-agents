# Issue #10 Docker feasibility findings

Owner to fill after live experiments. No experiment has been run by the
implementer. Keep the completed report on the spike branch for review, unmerged.

For every result: exact expanded command(s), UTC times, host platform, Docker
version/context, image content ID, registry digest or explicitly none, image
revision/ub-agents commit, actual worktree base SHA and resulting commit SHA.

| Experiment | Test issues / containers | Version evidence | Observation / artifacts | Result or failure |
| --- | --- | --- | --- | --- |
| One worker / isolation | | | | Not run |
| Credential reach | | | | Not run |
| Graceful stop / recovery | | | | Not run |
| Hard crash / recovery, if tested | | | | Not run |
| Two separate issues | | | | Not run |
| Two contenders / recovery | | | | Not run |

## Isolation

Clone and mount evidence; filesystem probes and unchanged host markers/checkout;
PID and capability evidence; network/host-service reach; Unix/TCP Docker daemon
reach. Distinguish observed ordinary access from untested container escape.

## Credentials

Each credential's type (never secret value), supplying mechanism, readable
locations, effective repository/account/project/model/spending permissions,
expiry, access beyond the target repository, and trusted coordination-comment
authority. Record API/provider shared-account and refresh/rate-limit constraints.

## Recovery and concurrency

Branches and local uncommitted contents retained/lost; pushed draft continuity;
logs retained; TERM/KILL and lease/retry timing; operator reset evidence; attempts;
claim IDs/winner/withdrawals; actual runtime starts; duplicate work; result
acceptance and labels. Separate exclusion, election, outcome recovery and fresh
execution. State what was not exercised.

## Recommendation

Case for and against a supported runner, proposed minimum contents if warranted,
remaining security/recovery limits, tested platforms and Linux/macOS VM/native
build limits. Separate observed facts, inferences and untested assumptions.
