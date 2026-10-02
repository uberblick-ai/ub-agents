#!/usr/bin/env python3
"""Owner-operated Docker experiment commands. Not an ub-agent runner API."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time

from common import container_command, credentials


ROOT = Path(__file__).resolve().parents[2]
RECORDS = ROOT / ".ub-agent/docker-spike"
FILES = ["pyproject.toml", "README.md", "LICENSE", "src", ".agents/implementer.md", "spikes/docker-runner"]
INJECT = """import os, pathlib, sys
os.umask(0o077)
root = pathlib.Path('/run/spike-auth')
(root / 'input.tmp').write_bytes(sys.stdin.buffer.read())
(root / 'input.tmp').replace(root / 'credentials.json')
"""


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def name(value):
    if not re.fullmatch(r"ub-spike10-[a-z0-9][a-z0-9-]{0,50}", value):
        raise argparse.ArgumentTypeError("Name must start ub-spike10- and use lowercase letters, digits, hyphens")
    return value


def issue(value):
    number = int(value)
    if number <= 0 or number == 10:
        raise argparse.ArgumentTypeError("Use a dedicated test issue, never issue #10")
    return number


def check_container(worker):
    label = output("docker", "inspect", "--format", '{{index .Config.Labels "ub-agent.spike"}}', worker)
    if label != "issue-10":
        raise ValueError("Refusing to operate on a container without the issue-10 spike label")


def build(args):
    # Context is an allowlisted committed snapshot, never the operator's working
    # tree, environment, .git, home, .venv, .ub-agent or credentials file.
    sha = output("git", "-C", str(ROOT), "rev-parse", "HEAD")
    archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", sha, "--", *FILES])
    RECORDS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RECORDS) as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            # Refuse links even on Python 3.11; committed symlinks cannot smuggle
            # other host files into the build context.
            if any(not member.isfile() and not member.isdir() for member in tar.getmembers()):
                raise ValueError("Build context must contain only files and directories")
            tar.extractall(directory)
        run("docker", "build", "--tag", args.image, "--build-arg", f"UB_AGENTS_COMMIT={sha}",
            "--file", str(Path(directory) / "spikes/docker-runner/Dockerfile"), directory)
    metadata = json.loads(output("docker", "image", "inspect", args.image))[0]
    record = {"commit": sha, "image": args.image, "image_id": metadata["Id"],
              "repo_digests": metadata["RepoDigests"], "platform": metadata["Os"] + "/" + metadata["Architecture"]}
    (RECORDS / "build.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


def secret_input(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Owner credential file must have mode 0600 or stricter")
    raw = path.read_bytes()
    credentials(raw)
    return raw


def create(args, worker, number):
    # Reject any existing volume, including artifacts from a removed container.
    if subprocess.run(["docker", "volume", "inspect", f"{worker}-data"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError(f"{worker}-data already exists; use a new name or restart the original container")
    run(*container_command(args.image, worker, number, args.git_name, args.git_email),
        stdout=subprocess.DEVNULL)
    run("docker", "start", worker, stdout=subprocess.DEVNULL)


def prepare(workers, raw, release=True):
    for worker in workers:
        run("docker", "exec", "-i", worker, "python3", "-c", INJECT, input=raw)
    deadline = time.monotonic() + 180
    pending = set(workers)
    while pending:
        for worker in list(pending):
            if output("docker", "inspect", "--format", "{{.State.Running}}", worker) != "true":
                raise ValueError(f"{worker} failed preflight; collect it and inspect bootstrap.log and doctor.json")
            result = subprocess.run(["docker", "exec", worker, "test", "-f", "/run/spike-auth/prepared"])
            if result.returncode == 0:
                pending.remove(worker)
        if time.monotonic() >= deadline:
            raise ValueError("Preflight timed out; collect containers before stopping them")
        if pending:
            time.sleep(1)
    # Both clones/auth/preflights are ready before either launcher is released.
    if release:
        for worker in workers:
            run("docker", "exec", worker, "touch", "/run/spike-auth/go")
    print(("Started: " if release else "Prepared, held for up to 30 minutes: ") + ", ".join(workers))


def start(args):
    raw = secret_input(args.credentials)
    targets = [(args.name, args.issue)] if args.command == "start" else list(zip(args.names, args.issues))
    if len({worker for worker, _ in targets}) != len(targets):
        raise ValueError("Workers must have distinct names and volumes")
    # Resolve the tag once; both workers use the same immutable image ID.
    args.image = output("docker", "image", "inspect", "--format", "{{.Id}}", args.image)
    for worker, number in targets:
        create(args, worker, number)
    prepare([worker for worker, _ in targets], raw, release=not args.hold)


def restart(args):
    raw = secret_input(args.credentials)
    check_container(args.name)
    if output("docker", "inspect", "--format", "{{.State.Running}}", args.name) == "true":
        raise ValueError("Container is already running")
    run("docker", "start", args.name, stdout=subprocess.DEVNULL)
    prepare([args.name], raw, release=not args.hold)


def collect(args):
    check_container(args.name)
    destination = RECORDS / "evidence" / args.name / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination.mkdir(parents=True, exist_ok=False)
    with (destination / "bootstrap.log").open("w") as stream:
        run("docker", "logs", "--timestamps", args.name, stdout=stream, stderr=subprocess.STDOUT)
    metadata = json.loads(output("docker", "inspect", args.name))[0]
    # Do not export Docker environment arrays. Input secrets aren't in them, but
    # keep evidence narrowly scoped in case an owner changes the setup.
    record = {key: metadata[key] for key in ("Id", "Image", "State", "Mounts", "HostConfig", "NetworkSettings")}
    record["command"] = metadata["Config"]["Cmd"]
    record["image_metadata"] = json.loads(output("docker", "image", "inspect", metadata["Image"]))[0]
    (destination / "container.json").write_text(json.dumps(record, indent=2) + "\n")
    run("docker", "cp", f"{args.name}:/work/.", str(destination / "work"))
    print(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build", help="Build only an allowlisted HEAD snapshot; no credentials")
    command.add_argument("--image", required=True)
    command.set_defaults(action=build)
    for verb in ("start", "start-two"):
        command = commands.add_parser(verb, help="Owner only: preflight then launch once")
        command.add_argument("--image", required=True)
        command.add_argument("--credentials", required=True)
        command.add_argument("--hold", action="store_true", help="Prepare without claiming; use release within 30 minutes")
        command.add_argument("--git-name", required=True)
        command.add_argument("--git-email", required=True)
        if verb == "start":
            command.add_argument("--name", type=name, required=True)
            command.add_argument("--issue", type=issue, required=True)
        else:
            command.add_argument("--names", type=name, nargs=2, required=True)
            command.add_argument("--issues", type=issue, nargs=2, required=True,
                                 help="Distinct issues, or the same issue twice for contention")
        command.set_defaults(action=start)
    command = commands.add_parser("restart", help="Owner only: same volume and config, fresh auth and session")
    command.add_argument("--name", type=name, required=True)
    command.add_argument("--credentials", required=True)
    command.add_argument("--hold", action="store_true")
    command.set_defaults(action=restart)
    for verb in ("stop", "crash", "collect", "release"):
        command = commands.add_parser(verb)
        command.add_argument("--name", type=name, required=True)
        command.set_defaults(action=collect if verb == "collect" else stop)
    args = parser.parse_args()
    try:
        args.action(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Do not dump credential JSON or subprocess stdin on errors.
        parser.exit(1, f"Spike command failed: {exc}\n")


def stop(args):
    check_container(args.name)
    if args.command == "release":
        run("docker", "exec", args.name, "test", "-f", "/run/spike-auth/prepared")
        run("docker", "exec", args.name, "touch", "/run/spike-auth/go")
        return
    elif args.command == "crash":
        run("docker", "kill", "--signal", "KILL", args.name)
    else:
        run("docker", "stop", "--time", "30", args.name)
    print("Container and volume retained; run collect before any manual removal")


if __name__ == "__main__":
    main()
