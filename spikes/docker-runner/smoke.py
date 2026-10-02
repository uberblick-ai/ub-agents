"""Offline image smoke: no credentials, network, clone or agent execution."""

import argparse
import json
from pathlib import Path
import subprocess
import uuid

from common import container_command


CHECK = """
import json, os, pathlib, subprocess
from ub_agents.config import load_config
from ub_agents.execution import parse_process_table
assert os.getuid() == 1000
status = pathlib.Path('/proc/self/status').read_text().splitlines()
assert int(next(line.split()[1] for line in status if line.startswith('CapEff:')), 16) == 0
assert int(next(line.split()[1] for line in status if line.startswith('CapBnd:')), 16) == 0
assert next(line.split()[1] for line in status if line.startswith('NoNewPrivs:')) == '1'
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/run/docker.sock').exists()
assert not os.environ.get('GH_TOKEN')
assert not os.environ.get('OPENAI_API_KEY')
pathlib.Path(os.environ['HOME']).mkdir(parents=True)
subprocess.run(['ub-agent', '--config', '/opt/spike/ub-agent.yaml', 'check'], check=True)
rows = parse_process_table(subprocess.check_output(['ps', '-axo', 'pid=,pgid=,stat='], text=True))
assert os.getpid() in {pid for pid, _, _ in rows}
record = {name: subprocess.check_output(command, text=True).strip() for name, command in (
    ('codex', ['codex', '--version']), ('gh', ['gh', '--version']), ('ub_agent', ['ub-agent', '--version']))}
pathlib.Path('/work/smoke.json').write_text(json.dumps(record))
print(json.dumps(record))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    worker = f"ub-spike10-smoke-{uuid.uuid4().hex[:12]}"
    image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", args.image],
                                    text=True).strip()
    command = container_command(image, worker, 101, "Synthetic", "synthetic@example.invalid")
    command[command.index("--network") + 1] = "none"
    position = command.index(image)
    command = command[:position] + ["--entrypoint", "python3", image, "-c", CHECK]
    records = Path(__file__).resolve().parents[2] / ".ub-agent/docker-spike"
    records.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        subprocess.run(command, check=True, capture_output=True)
        created = True
        subprocess.run(["docker", "start", "--attach", worker], check=True)
        code = subprocess.check_output(["docker", "inspect", "--format", "{{.State.ExitCode}}", worker], text=True).strip()
        if code != "0":
            raise SystemExit(f"Offline smoke failed with exit {code}")
        # A stopped container still exposes the Docker-managed volume artifact.
        artifact = records / "smoke.json"
        subprocess.run(["docker", "cp", f"{worker}:/work/smoke.json", str(artifact)], check=True)
        metadata = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
        record = json.loads(artifact.read_text()) | {
            "image_id": image, "commit": metadata["Config"]["Labels"]["org.opencontainers.image.revision"],
            "network": "none", "credentials": "none", "stopped_volume_read": "passed"}
        artifact.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record, indent=2))
    finally:
        if created:
            # Only the uniquely named container and volume created by this smoke.
            subprocess.run(["docker", "rm", worker], check=True, capture_output=True)
            subprocess.run(["docker", "volume", "rm", f"{worker}-data"], check=True, capture_output=True)


if __name__ == "__main__":
    main()
