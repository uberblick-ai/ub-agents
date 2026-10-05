#!/usr/bin/env python3
"""One bounded OpenCode attempt; OpenCode owns the entire agent/tool loop.

macOS only. Runtime inputs and raw output belong in a fresh private scratch path.
No model installation or persistent operator configuration is changed.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
TARGET = json.loads((HERE / "target.json").read_text())
LIMIT = 30 * 60
SWAP_GROWTH_MIB = 2048
MIN_FREE_PERCENT = 5


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=120, **kwargs).strip()


def api(port, path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def stop(process):
    # Only the process group created by this script, never an operator process.
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    # Also finish any child still holding our process group after its leader exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def observation(port, processes):
    import re
    swap = command(["sysctl", "vm.swapusage"])
    used = float(re.search(r"used = ([0-9.]+)M", swap).group(1))
    pressure = command(["memory_pressure", "-Q"])
    free = int(re.search(r"free percentage: (\d+)%", pressure).group(1))
    rss = {}
    for name, process in processes.items():
        if process is not None and process.poll() is None:
            rss[name] = int(command(["ps", "-o", "rss=", "-p", str(process.pid)]))
    try:
        models = api(port, "/api/ps").get("models", [])
    except (OSError, ValueError):
        models = []
    return {"swap_used_mib": used, "memory_free_percent": free,
            "leader_rss_kib": rss, "models": models}


def profile(runtime, opencode, python_root, port):
    # The agent can read system runtimes and only this attempt's writable state.
    # A port-specific outbound rule prevents GitHub and all other web access.
    read_paths = ["/System", "/usr", "/bin", "/sbin",
                  "/Library/Developer/CommandLineTools", "/Library/Apple",
                  "/Applications/Xcode.app/Contents",
                  "/opt/homebrew/Cellar", "/opt/homebrew/opt",
                  "/private/var/db/timezone", str(opencode.parent.parent),
                  str(python_root)]
    read_rules = " ".join(f"(subpath {json.dumps(p)})" for p in read_paths)
    return f'''(version 1)
(allow default)
(deny file-read-data (require-not (require-any {read_rules}
    (literal "/")
    (subpath {json.dumps(str(runtime))})
    (literal "/dev/null") (literal "/dev/tty")
    (literal "/dev/urandom") (literal "/dev/random"))))
(deny file-write* (require-not (require-any
    (subpath {json.dumps(str(runtime))})
    (literal "/dev/null") (literal "/dev/tty"))))
(deny network-outbound (require-not (remote ip "localhost:{port}")))
(deny network-inbound)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True,
                        help="New directory within the run's private scratch")
    parser.add_argument("--opencode", type=Path, required=True,
                        help="Spike-local installed opencode binary")
    parser.add_argument("--ollama-models", type=Path, required=True,
                        help="Existing model store; read only, no pulls")
    parser.add_argument("--source", type=Path, default=HERE.parent.parent)
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = args.scratch.resolve()
    root.mkdir(parents=True, exist_ok=False)
    runtime = root / "runtime"
    runtime.mkdir()
    checkout = runtime / "checkout"
    state = runtime / "state"
    state.mkdir()
    result = {"target": TARGET, "wall_limit_seconds": LIMIT,
              "guards": {"swap_growth_mib": SWAP_GROWTH_MIB,
                         "minimum_memory_free_percent": MIN_FREE_PERCENT},
              "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "stage": "prepare", "stop_reason": None}
    daemon = agent = None
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    logs = []
    try:
        clean = {"PATH": "/Applications/Xcode.app/Contents/Developer/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                 "TMPDIR": str(runtime), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": "/dev/null"}
        command(["git", "init", "-q", str(checkout)], env=clean)
        # No alternates, shared objects, remotes, or refs after the reviewed head.
        command(["git", "-C", str(checkout), "fetch", "--depth=2",
                 args.source.resolve().as_uri(), TARGET["head"]], env=clean)
        command(["git", "-C", str(checkout), "checkout", "-q", "--detach",
                 TARGET["head"]], env=clean)
        (checkout / ".git/FETCH_HEAD").unlink()
        assert command(["git", "-C", str(checkout), "rev-parse", "HEAD^"], env=clean) == TARGET["base"]
        assert command(["git", "-C", str(checkout), "rev-list", "--all"], env=clean).splitlines() == [TARGET["head"], TARGET["base"]]
        (checkout / "requirements.md").write_text((HERE / "requirements.md").read_text())
        prompt = (HERE / "prompt.md").read_text().replace("BASE", TARGET["base"]).replace("HEAD", TARGET["head"])
        (runtime / "prompt.md").write_text(prompt)
        python_root = Path(sys.base_prefix).resolve()
        python_bin = python_root / "bin/python3"
        command([str(python_bin), "-m", "venv", str(checkout / ".venv")], env=clean)
        # Preparation may use the network. The agent cannot; dependencies are ready.
        command([str(checkout / ".venv/bin/pip"), "install", "-q", "-e", str(checkout)], env={**clean, "PATH": str(python_bin.parent) + ":" + clean["PATH"]})
        opencode = args.opencode.resolve()
        env = {**clean, "PATH": str(checkout / ".venv/bin") + ":" + clean["PATH"],
               "SHELL": "/bin/bash",
               "XDG_CONFIG_HOME": str(state / "config"),
               "XDG_DATA_HOME": str(state / "data"),
               "XDG_CACHE_HOME": str(state / "cache"),
               "XDG_STATE_HOME": str(state / "local-state"),
               "OPENCODE_DISABLE_AUTOUPDATE": "true",
               "OPENCODE_DISABLE_MODELS_FETCH": "true",
               "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
               "OPENCODE_DISABLE_CLAUDE_CODE": "true",
               "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
               "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "true"}
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "ollama/" + TARGET["model"],
            "small_model": "ollama/" + TARGET["model"],
            "enabled_providers": ["ollama"], "share": "disabled",
            "autoupdate": False, "snapshot": False, "lsp": False,
            "provider": {"ollama": {
                "npm": "@ai-sdk/openai-compatible", "name": "Ollama",
                "options": {"baseURL": f"http://127.0.0.1:{args.port}/v1", "timeout": LIMIT * 1000},
                "models": {TARGET["model"]: {"name": TARGET["model"],
                    "tool_call": True, "reasoning": True,
                    "limit": {"context": TARGET["context"], "output": 8192}}}}},
            "permission": {"*": "deny", "read": "allow", "glob": "allow",
                           "grep": "allow", "bash": "allow"},
            "agent": {"reviewer": {"description": "Unattended evidence-based review",
                "mode": "primary", "prompt": "You are a code reviewer. Read source and run focused checks before writing your final review."}},
        }
        config_path = runtime / "opencode.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        env["OPENCODE_CONFIG"] = str(config_path)
        result["opencode_version"] = command([str(opencode), "--version"], env=env)
        if result["opencode_version"] != TARGET["opencode_version"]:
            raise RuntimeError("OpenCode version differs from pinned target")
        result["ollama_version"] = command(["/usr/local/bin/ollama", "--version"])
        result["hardware"] = {"model": command(["sysctl", "-n", "hw.model"]),
                              "cpu": command(["sysctl", "-n", "machdep.cpu.brand_string"]),
                              "memory_bytes": int(command(["sysctl", "-n", "hw.memsize"])),
                              "os": command(["sw_vers", "-productVersion"])}
        sb = runtime / "agent.sb"
        sb.write_text(profile(runtime, opencode, python_root, args.port))
        sandbox = ["/usr/bin/sandbox-exec", "-f", str(sb)]
        result["stage"] = "isolation-preflight"
        sentinel = root / "outside-runtime.txt"
        sentinel.write_text("isolation sentinel\n")
        # Positive tool checks and negative credential/source/web-access checks.
        probe = '''import pathlib, socket, subprocess
assert pathlib.Path("AGENTS.md").read_text()
assert subprocess.run(["git", "diff", "HEAD^", "HEAD", "--stat"], capture_output=True).returncode == 0
for p in [__SOURCE__, __SENTINEL__, str(pathlib.Path.home()/".config/gh/hosts.yml"), str(pathlib.Path.home()/".local/share/opencode/auth.json")]:
    try: pathlib.Path(p).read_text()
    except (PermissionError, FileNotFoundError): pass
    else: raise RuntimeError("external file was readable: " + p)
try: socket.create_connection(("1.1.1.1", 443), timeout=2)
except PermissionError: pass
else: raise RuntimeError("external network was allowed")
print("checkout/git/python allowed; source/credentials/external network denied")
'''.replace("__SOURCE__", repr(str(args.source.resolve() / "AGENTS.md"))).replace("__SENTINEL__", repr(str(sentinel)))
        result["isolation_preflight"] = command(sandbox + [str(checkout / ".venv/bin/python"), "-c", probe], env=env, cwd=checkout)
        result["resolved_config"] = json.loads(command(sandbox + [str(opencode), "--pure", "debug", "config"], env=env, cwd=checkout))
        if args.prepare_only:
            result["stop_reason"] = "prepared-only; no model request"
            return
        result["stage"] = "daemon-start"
        # A separate daemon makes the documented server context setting ephemeral.
        # Refuse to connect to any pre-existing service on our port.
        with socket.socket() as check:
            check.bind(("127.0.0.1", args.port))
        daemon_log = (root / "ollama.log").open("wb")
        logs.append(daemon_log)
        daemon_env = {**clean, "OLLAMA_HOST": f"127.0.0.1:{args.port}",
                      # Preserve the existing home location; never redirect it or
                      # copy its credentials into the agent's environment/state.
                      "HOME": os.environ["HOME"],
                      "OLLAMA_MODELS": str(args.ollama_models.resolve()),
                      "OLLAMA_TMPDIR": str(runtime),
                      "OLLAMA_CONTEXT_LENGTH": str(TARGET["context"]),
                      "OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_NUM_PARALLEL": "1",
                      "OLLAMA_NO_CLOUD": "1"}
        daemon = subprocess.Popen(["/usr/local/bin/ollama", "serve"], env=daemon_env,
                                  stdout=daemon_log, stderr=daemon_log, start_new_session=True)
        while True:
            if daemon.poll() is not None:
                raise RuntimeError(f"private Ollama daemon exited {daemon.returncode}; see ollama.log")
            if time.monotonic() - started >= LIMIT:
                raise RuntimeError("private Ollama daemon exceeded attempt wall-clock bound")
            try:
                result["server_version"] = api(args.port, "/api/version")
                break
            except OSError:
                time.sleep(0.2)
        models = api(args.port, "/api/tags")["models"]
        model = next(m for m in models if m["name"] == TARGET["model"])
        if model["digest"] != TARGET["digest"]:
            raise RuntimeError("installed model digest differs from pinned target")
        result["installed_model"] = model
        info = api(args.port, "/api/show", {"model": TARGET["model"]})
        result["model_metadata"] = {k: info.get(k) for k in ["thinking", "parameters", "details", "model_info", "capabilities"]}
        result["stage"] = "opencode-review"
        baseline = observation(args.port, {"ollama": daemon})
        result["baseline"] = baseline
        invocation = sandbox + [str(opencode), "--pure", "run", "--format", "json",
                               "--thinking", "--agent", "reviewer", "--title",
                               "Blind PR review", prompt]
        result["invocation"] = invocation
        agent = subprocess.Popen(invocation, env=env, cwd=checkout,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        for pipe, name in [(agent.stdout, "events.jsonl"), (agent.stderr, "opencode.log")]:
            os.set_blocking(pipe.fileno(), False)
            output = (root / name).open("wb")
            logs.append(output)
            selector.register(pipe, selectors.EVENT_READ, output)
        next_sample = 0
        verified = False
        with (root / "observations.jsonl").open("w") as observations:
            while selector.get_map() or agent.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed >= LIMIT:
                    result["stop_reason"] = "wall-clock limit (1800 seconds)"
                    break
                if elapsed >= next_sample:
                    sample = observation(args.port, {"ollama": daemon, "opencode": agent})
                    sample["elapsed_seconds"] = round(elapsed, 3)
                    observations.write(json.dumps(sample) + "\n")
                    observations.flush()
                    next_sample = elapsed + 5
                    if sample["swap_used_mib"] - baseline["swap_used_mib"] >= SWAP_GROWTH_MIB or sample["memory_free_percent"] <= MIN_FREE_PERCENT:
                        result["stop_reason"] = "resource guard: swap growth or low free memory"
                        break
                    for loaded in sample["models"]:
                        if loaded["name"] == TARGET["model"]:
                            result["effective_context"] = loaded.get("context_length")
                            if loaded.get("context_length") != TARGET["context"]:
                                result["stop_reason"] = "effective context differs from configured 65536"
                                break
                            verified = True
                    if result["stop_reason"]:
                        break
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if chunk:
                        key.data.write(chunk)
                        key.data.flush()
                    else:
                        selector.unregister(key.fileobj)
                if daemon.poll() is not None:
                    result["stop_reason"] = "private Ollama daemon exited"
                    break
        if result["stop_reason"] is None:
            result["stop_reason"] = f"OpenCode exited {agent.wait()}"
        result["context_verified_while_loaded"] = verified
    except Exception as error:
        result["stop_reason"] = f"{type(error).__name__}: {error}"
    finally:
        stop(agent)
        stop(daemon)
        selector.close()
        for log in logs:
            log.close()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["opencode_exit_code"] = None if agent is None else agent.returncode
        result["ollama_exit_code"] = None if daemon is None else daemon.returncode
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: result[k] for k in ["stage", "stop_reason", "elapsed_seconds"]}))


if __name__ == "__main__":
    main()
