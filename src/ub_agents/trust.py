"""Current repository roles determine which launcher comments count."""

from urllib.parse import quote

from .bots import listed_bot
from .errors import AgentError, GitHubError

WRITERS = {"write", "maintain", "admin"}


class LauncherTrust:
    def __init__(self, github, launchers=None, role=None, on_author=None, *, trusted_bots=()):
        self.github = github
        self.launchers = None if launchers is None else {login.casefold() for login in launchers}
        self.role = role or (lambda login: github.role(login))
        self.on_author = on_author
        self.trusted_bots = {login.casefold() for login in trusted_bots}

    def observed(self, login, reason):
        if self.on_author is not None:
            self.on_author(login, reason is None, reason)
        return reason

    def reason(self, login):
        if not isinstance(login, str) or not login:
            raise GitHubError("GET", "comment author", "Coordination author is unreadable")
        if self.launchers is not None and login.casefold() not in self.launchers:
            return self.observed(login, f"Launcher account @{login} is not listed in launchers")
        try:
            role = self.role(login)
        except AgentError:
            self.observed(login, f"Launcher account @{login}'s repository role could not be read")
            raise
        if role is None:
            self.observed(login, f"Launcher account @{login}'s repository role could not be read")
            raise GitHubError("GET", f"repos/{self.github.repository}/collaborators/{quote(login, safe='')}/permission",
                              f"Launcher account @{login}'s repository role could not be read")
        if role not in WRITERS:
            return self.observed(login, f"Launcher account @{login} has repository role {role}; write or higher is required")
        return self.observed(login, None)

    def __call__(self, author):
        login = author.get("login") if isinstance(author, dict) else None
        if listed_bot(author, self.trusted_bots):
            self.observed(login, f"Listed bot @{login} supplies feedback, not launcher records")
            return False
        return self.reason(login) is None

    def observation(self):
        """Share role reads only for this read, never across observations."""
        cache = {}

        def role(login):
            key = login.casefold()
            if key not in cache:
                cache[key] = self.role(login)
            return cache[key]

        return LauncherTrust(self.github, self.launchers, role, self.on_author, trusted_bots=self.trusted_bots)
