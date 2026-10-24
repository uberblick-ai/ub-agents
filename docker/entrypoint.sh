#!/bin/sh
# Start a private Docker daemon when the container may run one, then continue as
# the agent user. UB_AGENTS_DOCKERD: auto (default), on or off.
set -eu

mode=${UB_AGENTS_DOCKERD:-auto}

start_dockerd() {
  # cgroup v2: move this container's processes out of the root cgroup so dockerd
  # can delegate controllers, as moby's dind script does.
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :
    sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers \
      > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || :
  fi
  dockerd > /var/log/dockerd.log 2>&1 &
  pid=$!
  tries=0
  until docker info > /dev/null 2>&1; do
    kill -0 "$pid" 2> /dev/null || return 1
    tries=$((tries + 1))
    [ "$tries" -lt 60 ] || return 1
    sleep 1
  done
}

if [ "$mode" != off ]; then
  # A plain container has a read-only cgroup tree; Sysbox and privileged ones don't.
  if [ "$mode" = on ] || [ -w /sys/fs/cgroup ]; then
    if ! start_dockerd; then
      echo "ub-agents: dockerd did not start; see /var/log/dockerd.log" >&2
      tail -n 20 /var/log/dockerd.log >&2 || :
      [ "$mode" != on ] || exit 1
    fi
  fi
fi

exec setpriv --reuid=agent --regid=agent --init-groups ub-agents-setup "$@"
