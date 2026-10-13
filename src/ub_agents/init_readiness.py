"""Advisory setup readiness uses the existing doctor and launch gates."""

from .config import instruction_text, load_config
from .doctor import Doctor
from .init_probes import PROBE_ERRORS, findings


def readiness(path, github, doctor=None):
    from .cli import launch_checks
    doctor = doctor or Doctor(github=github)
    doctor.run(path)
    checks = doctor.checks
    blocked = any((c.required or c.id == "runtime-permissions") and c.status != "ok" for c in checks)
    details = [findings(checks)]
    try:
        config = load_config(path)
        def read_git(root, *args):
            return doctor.probe(["git", "-C", str(root), *args], root).strip()
        launch_checks(config, github, read_git)
        policy = instruction_text(config.root, config.shared_instructions, "shared-instructions")
        if any(marker in policy for marker in ("<project build command>", "<project test command>",
                                               "<project lint or validation command>", "@org/maintainers",
                                               "Define those gates", "Replace this section with the project's review priorities")):
            blocked = True
            details.append("Replace project check placeholders and review merge authority and decision-makers in .agents/ub_agents.md before launch.")
    except PROBE_ERRORS as exc:
        from .doctor import public
        blocked = True
        details.append(public(exc))
    return not blocked, "\n\n".join(details)
