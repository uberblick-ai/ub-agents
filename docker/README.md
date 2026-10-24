# Run a launcher in Docker

A trial setup: one launcher in a container, with the checkout, run state and CLI
logins on two volumes. Agents can use everything inside the container, including the
tokens below, but nothing on the host.

```sh
cp env.example .env              # fill in the repository and tokens
docker compose up -d --build
docker attach ub-agents-launcher # terminal view; detach with Ctrl-p Ctrl-q
```

Other commands run in the same setup:

```sh
docker compose run --rm launcher doctor
docker compose run --rm launcher status
```

`docker compose stop` sends SIGTERM and kills the run after 10 seconds; the item runs
again on the next start. `docker compose down -v` also deletes the volumes.

The image installs ub-agents from a tag (`--build-arg UB_AGENTS_REF=v0.3.5`). Claude
Code, Codex and mise sit in the home volume, so `runtime-updates: auto` keeps them
current. On start, `mise install` runs in the checkout when it has a mise config.

Not covered: checks that need Docker (no socket is mounted), outbound network limits.
