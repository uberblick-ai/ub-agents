"""The first loop issue offered by fresh init, also printable for manual filing."""

from importlib.resources import files


TITLE = "Tailor `.agents/ub_agents.md` to this repository"
BODY = files("ub_agents").joinpath("templates/tailor-policy.md").read_text(encoding="utf-8")


def command(repository):
    return ["gh", "issue", "create", "--repo", repository, "--title", TITLE, "--label", "ready", "--body", BODY]
