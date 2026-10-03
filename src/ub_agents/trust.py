"""Current repository roles determine which launcher comments count."""

from urllib.parse import quote

from .errors import GitHubError

WRITERS = {"write", "maintain", "admin"}


class LauncherTrust:
    def __init__(self, github, launchers=None, role=None):
        self.github = github
        self.launchers = None if launchers is None else {login.casefold() for login in launchers}
        self.role = role or (lambda login: github.role(login))

    def reason(self, login):
        if not isinstance(login, str) or not login:
            raise GitHubError("GET", "comment author", "Coordination author is unreadable")
        if self.launchers is not None and login.casefold() not in self.launchers:
            return f"Launcher account @{login} is not listed in launchers"
        role = self.role(login)
        if role is None:
            raise GitHubError("GET", f"repos/{self.github.repository}/collaborators/{quote(login, safe='')}/permission",
                              f"Launcher account @{login}'s repository role could not be read")
        if role not in WRITERS:
            return f"Launcher account @{login} has repository role {role}; write or higher is required"
        return None

    def __call__(self, author):
        login = author.get("login") if isinstance(author, dict) else None
        return self.reason(login) is None

    def observation(self):
        """Share role reads only for this read, never across observations."""
        cache = {}

        def role(login):
            key = login.casefold()
            if key not in cache:
                cache[key] = self.role(login)
            return cache[key]

        return LauncherTrust(self.github, self.launchers, role)
