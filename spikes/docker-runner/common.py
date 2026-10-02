"""Shared spike-only validation. No environment credentials are imported."""

import json


def credentials(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Credentials must be a JSON object") from None
    if not isinstance(value, dict) or set(value) not in (
            {"GH_TOKEN", "OPENAI_API_KEY"}, {"GH_TOKEN", "CODEX_AUTH_JSON"}):
        raise ValueError("Supply GH_TOKEN and exactly one of OPENAI_API_KEY or CODEX_AUTH_JSON")
    for key in ("GH_TOKEN", "OPENAI_API_KEY"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()
                             or "\x00" in value[key] or "\n" in value[key]):
            raise ValueError(f"{key} must be a nonempty single-line string")
    if "CODEX_AUTH_JSON" in value and not isinstance(value["CODEX_AUTH_JSON"], dict):
        raise ValueError("CODEX_AUTH_JSON must be the owner's auth.json object")
    return value


def render_config(template, issue):
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0 or issue == 10:
        raise ValueError("Use a dedicated positive test issue number, never issue #10")
    return template.replace("ISSUE", str(issue)).replace(
        "docker-spike-implementer:", f"docker-spike-implementer-{issue}:")


def container_command(image, name, issue, git_name, git_email):
    # No caller-supplied Docker flags, binds, devices, host namespaces or sockets.
    return ["docker", "create", "--name", name, "--label", "ub-agent.spike=issue-10",
            "--init", "--user", "1000:1000", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--read-only",
            "--network", "bridge", "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,uid=1000,gid=1000,mode=1777",
            "--tmpfs", "/run/spike-auth:rw,nosuid,nodev,uid=1000,gid=1000,mode=0700",
            "--mount", f"type=volume,source={name}-data,target=/work",
            image, "--issue", str(issue), "--git-name", git_name, "--git-email", git_email]
