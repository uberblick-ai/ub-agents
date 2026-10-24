# Run a launcher in Docker

One launcher in a container, with the checkout, run state and CLI logins on
volumes. Agents can use everything inside the container, including the tokens in
`.env`, but nothing on the host.

```sh
cp env.example .env              # fill in the repository and tokens
docker compose up -d --build
docker attach ub-agents-launcher # terminal view; detach with Ctrl-p Ctrl-q
```

Other commands run in the same setup. Use `-u agent` with `exec`; the container starts
as root to run dockerd, and files created as root lock the agent out.

```sh
docker compose run --rm launcher doctor
docker compose exec -u agent launcher ub-agents status
```

## Projects whose checks use Docker

The image includes a Docker engine. It starts only where the container may run
one; the host's Docker socket is never mounted.

- **Linux host:** install [Sysbox](https://github.com/nestybox/sysbox), then
  `docker compose -f compose.yaml -f compose.sysbox.yaml up -d --build`.
- **Mac:** run the launcher's Docker in a Lima VM without your home folder, then use
  the privileged override there:

  ```sh
  limactl start --name ub-agents --set '.mounts=[]' template://docker
  export DOCKER_HOST=$(limactl list ub-agents --format 'unix://{{.Dir}}/sock/docker.sock')
  docker compose -f compose.yaml -f compose.privileged.yaml up -d --build
  ```

## What else is inside

ub-agents from a tag (`--build-arg UB_AGENTS_REF=v0.3.5`), git, `gh`, Python 3,
build tools, mise, Claude Code, Codex, Playwright's Chromium and WebKit with their
system libraries (`--build-arg PLAYWRIGHT_VERSION=…` to match your project). Claude
Code, Codex, mise and the browsers sit in the home volume, so `runtime-updates:
auto` keeps them current. On start, `mise install` runs in the checkout when it has
a mise config.

`docker compose stop` sends SIGTERM and kills the run after 10 seconds; the item
runs again on the next start. `docker compose down -v` also deletes the volumes.
