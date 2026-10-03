"""Conservative, local shutdown evidence. These checks never signal a process."""

import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import uuid

from .errors import AgentError
from .execution import group_members


def _sysctl(name):
    result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                            timeout=5, check=True)
    return result.stdout.strip()


def host_identity():
    if sys.platform.startswith("linux"):
        return {"machine": Path("/etc/machine-id").read_text().strip(),
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "pid_namespace": os.readlink("/proc/self/ns/pid")}
    if sys.platform == "darwin":
        result = subprocess.run(["ioreg", "-r", "-d", "1", "-c", "IOPlatformExpertDevice", "-a"],
                                capture_output=True, timeout=5, check=True)
        hardware = plistlib.loads(result.stdout)
        return {"machine": hardware[0]["IOPlatformUUID"], "boot": _sysctl("kern.bootsessionuuid")}
    raise AgentError("This platform has no supported machine and boot identity")


def process_identity(pid):
    """None proves absence; unreadable state raises, and a reused PID stays present."""
    if type(pid) is not int or pid < 1:
        raise AgentError("Invalid supervisor PID")
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return None
        fields = stat.rsplit(")", 1)[1].split()
        return {"pid": pid, "birth": fields[19]}
    if sys.platform == "darwin":
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
            return None
        if result.returncode or not result.stdout.strip():
            raise AgentError("Cannot read supervisor process identity")
        return {"pid": pid, "birth": result.stdout.strip()}
    raise AgentError("This platform has no supported supervisor process identity")


def identity_fields():
    # Failure to capture identity must not prevent execution or expiry recovery.
    try:
        identity = host_identity()
        supervisor = process_identity(os.getpid())
        if not valid_host(identity) or not valid_supervisor(supervisor):
            return {}
        return {"host": socket.gethostname(), "host_identity": identity, "supervisor": supervisor}
    except (AgentError, OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return {}


def valid_host(value):
    return (isinstance(value, dict) and {"machine", "boot"} <= set(value)
            and set(value) <= {"machine", "boot", "pid_namespace"}
            and all(isinstance(v, str) and v.strip() for v in value.values()))


def valid_supervisor(value):
    return (isinstance(value, dict) and set(value) == {"pid", "birth"}
            and type(value["pid"]) is int and value["pid"] > 0
            and isinstance(value["birth"], str) and bool(value["birth"].strip()))


def run_directory(config, lease):
    run = lease["run"]
    if not run or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run):
        raise AgentError("Unsafe run diagnostics name")
    directory = config.root / ".ub-agent" / "runs" / run
    if directory.resolve() != directory:
        raise AgentError("Run diagnostics path redirects")
    return directory


def read_file(path):
    if path.resolve() != path:
        raise AgentError("Shutdown evidence path redirects")
    return path.read_text()


def diagnostics_confirmed(config, lease):
    directory = run_directory(config, lease)
    events = directory / "events.jsonl"
    if events.resolve() != events:
        raise AgentError("Run diagnostics file redirects")
    if events.exists():
        for line in read_file(events).splitlines():
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise AgentError("Unreadable run diagnostic")
            if event["event"] in {"cleanup-unconfirmed", "cleanup-verdict-unrecorded"}:
                raise AgentError("Local diagnostics record unconfirmed cleanup")
    if lease.get("cleanup") == "unconfirmed":
        raise AgentError("Lease records unconfirmed cleanup")


def agent_group_stopped(config, lease):
    # Read both records: a crash between Popen and the GitHub update must fail closed.
    pid_file = run_directory(config, lease) / "pid"
    if pid_file.resolve() != pid_file:
        raise AgentError("Agent process record redirects")
    groups = set()
    if pid_file.exists():
        groups.add(int(read_file(pid_file)))
    if lease.get("process_group") is not None:
        groups.add(lease["process_group"])
    for group in groups:
        if type(group) is not int or group < 1:
            raise AgentError("Invalid recorded agent process group")
        if group_members(group):
            raise AgentError(f"Agent process group {group} is still present (possibly reused)")


def hook_groups_stopped(config, lease):
    parent = run_directory(config, lease) / "cleanup"
    if parent.resolve() != parent:
        raise AgentError("Cleanup hook diagnostics redirect")
    if not parent.exists():
        return
    for directory in parent.iterdir():
        if directory.resolve() != directory or not directory.is_dir():
            raise AgentError("Unreadable cleanup hook directory")
        if read_file(directory / "stopped") != "confirmed\n":
            raise AgentError("Cleanup hook has no confirmed stop")
        pid_file = directory / "pid"
        if pid_file.resolve() != pid_file:
            raise AgentError("Hook process record redirects")
        if pid_file.exists():
            group = int(read_file(pid_file))
            if group < 1 or group_members(group):
                raise AgentError(f"Cleanup hook process group {group} is still present (possibly reused)")


def cleanup_record(lease):
    return {key: lease.get(key) for key in
            ("run", "id", "actor", "host_identity", "supervisor")} | {"cleanup": "confirmed"}


def confirm_cleanup(config, lease):
    """Persist only after supervision and workspace cleanup have actually finished."""
    directory = run_directory(config, lease)
    diagnostics_confirmed(config, lease)
    agent_group_stopped(config, lease)
    hook_groups_stopped(config, lease)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "cleanup-confirmed.json"
    if target.resolve() != target:
        raise AgentError("Cleanup confirmation path redirects")
    temporary = directory / f".cleanup-{uuid.uuid4().hex}"
    try:
        with temporary.open("x") as stream:
            json.dump(cleanup_record(lease), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def cleanup_confirmed(config, lease):
    record = json.loads(read_file(run_directory(config, lease) / "cleanup-confirmed.json"))
    if record != cleanup_record(lease):
        raise AgentError("No matching durable supervisor cleanup confirmation")
