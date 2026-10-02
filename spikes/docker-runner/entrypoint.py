"""Container bootstrap, deliberately outside the supported package."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from common import credentials, render_config


AUTH = Path("/run/spike-auth")
ROOT = Path("/work/repo")


def wait_for(path, seconds=180):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise SystemExit(f"Timed out waiting for {path.name}")
        time.sleep(0.1)


def call(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--git-name", required=True)
    parser.add_argument("--git-email", required=True)
    args = parser.parse_args()
    if os.getuid() != 1000:
        raise SystemExit("Spike must run as UID 1000")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if any(int(status[key].strip(), 16) for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")):
        raise SystemExit("Spike requires all capabilities dropped")
    if status["NoNewPrivs"].strip() != "1":
        raise SystemExit("Spike requires no-new-privileges")
    if any(Path(socket).exists() for socket in ("/var/run/docker.sock", "/run/docker.sock")):
        raise SystemExit("Spike must not have a Docker socket")
    os.umask(0o077)
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    codex_home = home / ".codex"
    codex_home.mkdir(exist_ok=True)
    (codex_home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    sessions = Path("/work/codex-sessions")
    sessions.mkdir(exist_ok=True)
    (codex_home / "sessions").symlink_to(sessions, target_is_directory=True)
    wait_for(AUTH / "credentials.json")
    secret = credentials((AUTH / "credentials.json").read_bytes())
    (AUTH / "credentials.json").unlink()
    os.environ["GH_TOKEN"] = secret["GH_TOKEN"]
    (AUTH / "gh-token").write_text(secret["GH_TOKEN"])
    if "OPENAI_API_KEY" in secret:
        # Credentials arrive via stdin, never build arguments or Docker metadata.
        result = subprocess.run(["codex", "login", "--with-api-key"],
                                input=secret["OPENAI_API_KEY"] + "\n", text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise SystemExit("Codex API-key login failed")
    else:
        (codex_home / "auth.json").write_text(json.dumps(secret["CODEX_AUTH_JSON"]))
    del secret
    # Only this worker's clone is used. Control HEAD must remain on the default
    # branch for refresh; config and instructions are ignored, local overlay files.
    if not ROOT.exists():
        call("git", "clone", "https://github.com/uberblick-ai/ub-agents.git", str(ROOT))
    call("gh", "auth", "setup-git")
    call("git", "-C", str(ROOT), "config", "user.name", args.git_name)
    call("git", "-C", str(ROOT), "config", "user.email", args.git_email)
    role_dir = ROOT / ".ub-agent/docker-spike"
    role_dir.mkdir(parents=True, exist_ok=True)
    role = Path("/opt/spike/base-role.md").read_text() + Path("/opt/spike/role-suffix.md").read_text()
    (role_dir / "implementer.md").write_text(role)
    config = ROOT / ".docker-spike.yaml"
    config.write_text(render_config(Path("/opt/spike/ub-agent.yaml").read_text(), args.issue)
                      .replace("instructions: role-suffix.md",
                               "instructions: .ub-agent/docker-spike/implementer.md"))
    exclude = ROOT / ".git/info/exclude"
    if "/.docker-spike.yaml" not in exclude.read_text().splitlines():
        with exclude.open("a") as stream:
            stream.write("\n/.docker-spike.yaml\n/.ub-agent/\n")
    call("ub-agent", "--config", str(config), "check")
    from ub_agents.config import load_config
    from ub_agents.labels import configured_labels
    normal_labels = {label.name.casefold() for label in configured_labels(load_config(ROOT / "ub-agent.yaml"))}
    spike_labels = {label.name.casefold() for label in configured_labels(load_config(config))}
    if normal_labels & spike_labels:
        raise SystemExit("Spike labels overlap the current normal workflow")
    trigger = f"docker-spike-10-{args.issue}-ready"
    matches = json.loads(call("gh", "issue", "list", "--repo", "uberblick-ai/ub-agents",
                              "--label", trigger, "--state", "open", "--limit", "100", "--json", "number,labels"))
    if len(matches) != 1 or matches[0]["number"] != args.issue:
        raise SystemExit("Trigger must match exactly the requested open test issue")
    if normal_labels & {label["name"].casefold() for label in matches[0]["labels"]}:
        raise SystemExit("Test issue carries normal workflow labels; remove them before running")
    # Read-only prerequisite diagnostics. Failure stops before a claim.
    diagnosis = subprocess.run(["ub-agent", "--config", str(config), "doctor", "--json"],
                               capture_output=True, text=True)
    (Path("/work") / "doctor.json").write_text(diagnosis.stdout)
    if diagnosis.returncode:
        raise SystemExit("Doctor failed; inspect /work/doctor.json")
    metadata = {"issue": args.issue, "control_sha_before_refresh": call("git", "-C", str(ROOT), "rev-parse", "HEAD"),
                "ub_agent": call("ub-agent", "--version"), "codex": call("codex", "--version"),
                "gh": call("gh", "--version"), "actor": call("gh", "api", "user", "--jq", ".login")}
    with Path("/work/startups.jsonl").open("a") as stream:
        stream.write(json.dumps(metadata) + "\n")
    (AUTH / "prepared").touch()
    wait_for(AUTH / "go", seconds=1800)
    with Path("/work/launcher.log").open("a") as log:
        child = subprocess.Popen(["ub-agent", "--config", str(config), "launch", "--once"],
                                 cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        # Docker's init forwards TERM here; ub-agent performs ordinary supervision.
        signal.signal(signal.SIGTERM, lambda *_: child.send_signal(signal.SIGTERM))
        signal.signal(signal.SIGINT, lambda *_: child.send_signal(signal.SIGINT))
        raise SystemExit(child.wait())


if __name__ == "__main__":
    main()
